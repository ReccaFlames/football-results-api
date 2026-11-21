import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db_models import (
    CompetitionRow,
    CompetitionSeasonRow,
    MatchRow,
    SchedulerHeartbeatRow,
    ScrapeRunRow,
    TeamAliasRow,
)
from app.models import Competition, MatchQuery, ScrapedMatch, SeasonSource
from app.repository import (
    AbandonedScrapeRun,
    ActiveScrapeJobConflict,
    EmptySeasonError,
    PostgresResultRepository,
    ScheduledRefresh,
)
from app.scheduling import SchedulePolicy
from app.standings import compute_standings


def source(season: str, league_id: int) -> SeasonSource:
    return SeasonSource(
        season=season,
        source_url=f"http://www.90minut.pl/liga/1/liga{league_id}.html",
        provider="90minut",
    )


def league(current: str = "2026-2027", current_league_id: int = 2) -> Competition:
    return Competition(
        slug="test-league",
        name="Test League",
        country="Poland",
        format="league",
        current_season=current,
        seasons={
            "2025-2026": source("2025-2026", 1),
            "2026-2027": source("2026-2027", current_league_id),
        },
    )


def cup() -> Competition:
    return Competition(
        slug="test-cup",
        name="Test Cup",
        country=None,
        format="cup",
        current_season="2026-2027",
        seasons={"2026-2027": source("2026-2027", 3)},
    )


def match(
    home: str,
    away: str,
    played_at: datetime,
    score: tuple[int, int] | None = None,
    round_number: int = 1,
    season: str = "2026-2027",
) -> ScrapedMatch:
    home_score, away_score = score if score else (None, None)
    return ScrapedMatch(
        competition="test-league",
        season=season,
        stage=f"Kolejka {round_number}",
        round=round_number,
        home_team=home,
        away_team=away,
        status="finished" if score else "scheduled",
        home_score=home_score,
        away_score=away_score,
        home_goals=home_score,
        away_goals=away_score,
        home_cards=3 if score else None,
        played_at=played_at,
        source_url="http://www.90minut.pl/mecz.php?id_mecz=1",
    )


async def season_row(
    sessions: async_sessionmaker[AsyncSession], slug: str, season: str
) -> CompetitionSeasonRow:
    async with sessions() as session:
        return await session.scalar(
            select(CompetitionSeasonRow)
            .join(CompetitionRow)
            .where(CompetitionRow.slug == slug, CompetitionSeasonRow.season == season)
        )


async def scrape_runs(sessions: async_sessionmaker[AsyncSession]) -> list[ScrapeRunRow]:
    async with sessions() as session:
        return list((await session.scalars(select(ScrapeRunRow).order_by(ScrapeRunRow.id))).all())


async def set_next_check(
    sessions: async_sessionmaker[AsyncSession], slug: str, value: datetime
) -> None:
    async with sessions.begin() as session:
        competition_id = select(CompetitionRow.id).where(CompetitionRow.slug == slug)
        await session.execute(
            update(CompetitionSeasonRow)
            .where(
                CompetitionSeasonRow.competition_id == competition_id.scalar_subquery(),
                CompetitionSeasonRow.is_current,
            )
            .values(next_check_at=value)
        )


def close_to(actual: datetime | None, expected: datetime) -> bool:
    return actual is not None and abs(actual - expected) < timedelta(minutes=1)


async def test_seed_schedules_only_current_season_and_is_idempotent(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league()})
    await repository.seed_competitions({"test-league": league()})

    competitions = await repository.list_competitions()
    assert [item.slug for item in competitions] == ["test-league"]
    assert competitions[0].available_seasons == ["2026-2027", "2025-2026"]
    assert competitions[0].fetched_seasons == []

    current = await season_row(sessions, "test-league", "2026-2027")
    assert current.is_current and current.automatic_refresh_enabled
    assert current.scheduler_state == "waiting_for_schedule"
    assert current.next_check_at is not None
    historical = await season_row(sessions, "test-league", "2025-2026")
    assert not historical.is_current and not historical.automatic_refresh_enabled
    assert historical.scheduler_state == "disabled"
    assert historical.next_check_at is None


async def test_removed_competition_is_disabled_without_deleting_data(
    repository: PostgresResultRepository,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    removed = cup()
    await repository.seed_competitions({"test-league": league(), "test-cup": removed})
    source_info = removed.seasons[removed.current_season]
    played = datetime.now(UTC) - timedelta(days=1)
    await repository.replace_competition(
        removed, source_info, [match("A", "B", played, score=(1, 0))]
    )
    run_id, _ = await repository.enqueue_scrape(removed, source_info)

    await repository.seed_competitions({"test-league": league()})

    async with sessions() as session:
        competition_row = await session.scalar(
            select(CompetitionRow).where(CompetitionRow.slug == "test-cup")
        )
        assert competition_row is not None
        season = await session.scalar(
            select(CompetitionSeasonRow).where(
                CompetitionSeasonRow.competition_id == competition_row.id,
                CompetitionSeasonRow.season == source_info.season,
            )
        )
        matches = await session.scalars(
            select(MatchRow)
            .join(CompetitionSeasonRow)
            .where(CompetitionSeasonRow.competition_id == competition_row.id)
        )
        queued_job = await session.get(ScrapeRunRow, run_id)
        assert not competition_row.is_enabled
        assert season is not None
        assert not season.is_current and not season.automatic_refresh_enabled
        assert season.scheduler_state == "disabled" and season.next_check_at is None
        assert len(matches.all()) == 1
        assert queued_job is not None and queued_job.status == "cancelled"

    assert await repository.get_competition("test-cup") is None
    assert [item.slug for item in await repository.list_competitions()] == ["test-league"]
    results, refreshed_at = await repository.get_results(competition="test-cup")
    assert results == [] and refreshed_at is None
    assert await repository.claim_queued_scrape_runs(10) == []


async def test_list_competitions_without_current_season(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league()})
    async with sessions.begin() as session:
        await session.execute(update(CompetitionSeasonRow).values(is_current=False))

    competitions = await repository.list_competitions()
    assert len(competitions) == 1
    assert competitions[0].current_season is None
    assert competitions[0].available_seasons == ["2026-2027", "2025-2026"]
    assert not competitions[0].automatic_refresh_enabled
    assert competitions[0].scheduler_state is None
    assert competitions[0].next_check_at is None


async def test_seed_rollover_changes_which_season_is_claimed(
    repository: PostgresResultRepository,
) -> None:
    await repository.seed_competitions({"test-league": league(current="2025-2026")})
    await repository.seed_competitions({"test-league": league(current="2026-2027")})

    competition = await repository.get_competition("test-league")
    assert competition is not None and competition.current_season == "2026-2027"
    targets = await repository.claim_due_seasons(10)
    assert [target.source.season for target in targets] == ["2026-2027"]


async def test_seed_persists_calendar_season_settings_for_claims(
    repository: PostgresResultRepository,
) -> None:
    calendar = Competition(
        slug="calendar-league",
        name="Calendar League",
        format="league",
        season_format="calendar",
        season_start_month=1,
        current_season="2026",
        seasons={"2026": source("2026", 4)},
    )
    await repository.seed_competitions(
        {"calendar-league": calendar.model_copy(update={"season_start_month": 8})}
    )
    await repository.seed_competitions({"calendar-league": calendar})

    competition = await repository.get_competition("calendar-league")
    summary = (await repository.list_competitions())[0]
    targets = await repository.claim_due_seasons(10)
    assert competition is not None
    assert (competition.season_format, competition.season_start_month) == ("calendar", 1)
    assert (summary.season_format, summary.available_seasons) == ("calendar", ["2026"])
    assert [(target.source.season, target.competition.season_format) for target in targets] == [
        ("2026", "calendar")
    ]


async def test_seed_resets_schedule_only_when_source_changes(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league()})
    competition = league()
    await repository.record_scrape_failure(
        competition, competition.seasons["2026-2027"], RuntimeError("boom")
    )

    await repository.seed_competitions({"test-league": league()})
    unchanged = await season_row(sessions, "test-league", "2026-2027")
    assert unchanged.scheduler_state == "retrying"
    assert unchanged.consecutive_failures == 1

    await repository.seed_competitions({"test-league": league(current_league_id=99)})
    changed = await season_row(sessions, "test-league", "2026-2027")
    assert changed.source_url.endswith("liga99.html")
    assert changed.scheduler_state == "waiting_for_schedule"
    assert changed.consecutive_failures == 0
    assert changed.next_check_at <= datetime.now(UTC)


async def test_replace_competition_stores_matches_and_schedules_next_check(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition})
    now = datetime.now(UTC)
    fixtures = [
        match("Legia", "Lech", now - timedelta(days=1), score=(2, 1)),
        match("Wisła", "Cracovia", now + timedelta(days=3), round_number=2),
    ]

    refreshed_at = await repository.replace_competition(
        competition, competition.seasons["2026-2027"], fixtures
    )

    results, results_refreshed_at = await repository.get_results(competition="test-league")
    assert results_refreshed_at == refreshed_at
    assert [(item.home_team, item.home_score, item.away_score) for item in results] == [
        ("Legia", 2, 1)
    ]
    assert results[0].home_cards == 3 and results[0].home_corners is None
    season = await season_row(sessions, "test-league", "2026-2027")
    assert season.fetched_at == refreshed_at
    assert season.scheduler_state == "waiting_for_match"
    assert close_to(season.next_check_at, refreshed_at + timedelta(days=1))
    runs = await scrape_runs(sessions)
    assert [(run.status, run.matches_found) for run in runs] == [("completed", 2)]


async def test_replace_competition_replaces_previous_snapshot(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    season = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})
    played = datetime.now(UTC) - timedelta(days=2)

    await repository.replace_competition(
        competition,
        season,
        [match("A", "B", played, (1, 0)), match("C", "D", played, (0, 0))],
    )
    await repository.replace_competition(competition, season, [match("E", "F", played, (3, 3))])

    results, _ = await repository.get_results(competition="test-league")
    assert [(item.home_team, item.away_team) for item in results] == [("E", "F")]
    assert [run.status for run in await scrape_runs(sessions)] == ["completed", "completed"]


async def test_match_ids_survive_provider_id_upserts(
    repository: PostgresResultRepository,
) -> None:
    competition = league()
    season = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})
    original = match("Legia", "Lech", datetime.now(UTC) - timedelta(days=1), (2, 1))
    original.provider_match_id = "provider-123"
    await repository.replace_competition(competition, season, [original])
    first, _ = await repository.get_results(competition="test-league")

    updated = original.model_copy(
        update={
            "stage": "Rescheduled fixture",
            "round": 2,
            "played_at": original.played_at + timedelta(days=2),
        }
    )
    await repository.replace_competition(competition, season, [updated])
    second, _ = await repository.get_results(competition="test-league")

    assert len(first) == len(second) == 1
    assert second[0].id == first[0].id


async def test_match_ids_and_rounds_survive_a_switch_to_a_provider_without_rounds(
    repository: PostgresResultRepository,
) -> None:
    competition = league()
    season = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})
    kickoff = datetime.now(UTC) - timedelta(days=10)
    old = [
        match("Legia", "Lech", kickoff, (2, 1), round_number=1),
        match("Lech", "Legia", kickoff + timedelta(days=7), (0, 0), round_number=2),
    ]
    for index, item in enumerate(old):
        item.provider_match_id = f"old-{index}"
    await repository.replace_competition(competition, season, old)
    before, _ = await repository.get_results(competition="test-league")

    switched = [
        item.model_copy(
            update={"provider_match_id": f"new-{index}", "round": None, "stage": "Test League"}
        )
        for index, item in enumerate(old)
    ]
    added = match("Legia", "Wisła", kickoff + timedelta(days=8), (1, 1)).model_copy(
        update={"provider_match_id": "new-2", "round": None, "stage": "Test League"}
    )
    await repository.replace_competition(competition, season, [*switched, added])
    after, _ = await repository.get_results(competition="test-league")

    kept = {(item.home_team, item.away_team): item for item in after}
    assert {item.id for item in before} < {item.id for item in after}
    for item in before:
        assert kept[(item.home_team, item.away_team)].id == item.id
        assert kept[(item.home_team, item.away_team)].round == item.round
        assert kept[(item.home_team, item.away_team)].stage == item.stage
    assert (kept[("Legia", "Wisła")].round, kept[("Legia", "Wisła")].stage) == (
        None,
        "Test League",
    )


async def test_fallback_match_identity_aliases_and_escaped_search(
    repository: PostgresResultRepository,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    competition = league()
    season = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})
    kickoff = datetime.now(UTC) - timedelta(days=2)
    first = match("River % United", "Harbor_United", kickoff, (1, 0), round_number=1)
    first.stage = "Cup_A"
    second = match(
        "River X United", "Southside", kickoff + timedelta(days=1), (2, 0), round_number=2
    )
    second.stage = "Cup A"
    await repository.replace_competition(competition, season, [first, second])
    before, _ = await repository.get_results(competition="test-league")
    first_result = next(item for item in before if item.home_team == "River % United")

    async with sessions.begin() as session:
        home_id = await session.scalar(
            select(MatchRow.home_team_id).where(MatchRow.id == first_result.id)
        )
        session.add(
            TeamAliasRow(
                normalized_alias="alternate river",
                alias="Alternate River",
                team_id=home_id,
            )
        )

    moved = first.model_copy(update={"played_at": first.played_at + timedelta(days=5)})
    await repository.replace_competition(competition, season, [moved, second])
    after, _ = await repository.get_results(competition="test-league")
    moved_result = next(item for item in after if item.home_team == "River % United")

    assert moved_result.id == first_result.id
    assert moved_result.home_team_slug == "river-united"
    assert (await repository.get_results(team="%"))[0] == [moved_result]
    assert (await repository.get_results(team="_"))[0] == [moved_result]
    assert (await repository.get_results(team="Alternate"))[0] == [moved_result]
    assert (await repository.get_results(stage="Cup_"))[0] == [moved_result]


async def test_unregistered_historical_season_is_persisted_without_scheduling(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition})
    historical = source("2024-2025", 7)
    played = datetime(2025, 3, 1, 18, tzinfo=UTC)

    await repository.replace_competition(
        competition, historical, [match("A", "B", played, (1, 1), season="2024-2025")]
    )

    assert await repository.is_cached("test-league", "2024-2025")
    assert not await repository.is_cached("test-league", "2025-2026")
    row = await season_row(sessions, "test-league", "2024-2025")
    assert not row.is_current and row.scheduler_state == "disabled"
    assert row.next_check_at is None
    summary = (await repository.list_competitions())[0]
    assert "2024-2025" in summary.available_seasons
    assert summary.fetched_seasons == ["2024-2025"]


async def test_failures_back_off_and_success_resets_counter(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    season = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})

    await repository.record_scrape_failure(competition, season, RuntimeError("first"))
    before_second = datetime.now(UTC)
    await repository.record_scrape_failure(competition, season, RuntimeError("second"))

    row = await season_row(sessions, "test-league", "2026-2027")
    assert row.consecutive_failures == 2
    assert row.scheduler_state == "retrying"
    assert close_to(row.next_check_at, before_second + timedelta(minutes=15))
    runs = await scrape_runs(sessions)
    assert [(run.status, run.error_message) for run in runs] == [
        ("failed", "first"),
        ("failed", "second"),
    ]

    await repository.replace_competition(competition, season, [])
    row = await season_row(sessions, "test-league", "2026-2027")
    assert row.consecutive_failures == 0
    assert row.scheduler_state == "waiting_for_schedule"


async def test_claim_due_seasons_orders_limits_and_leases(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league(), "test-cup": cup()})
    now = datetime.now(UTC)
    await set_next_check(sessions, "test-league", now - timedelta(hours=1))
    await set_next_check(sessions, "test-cup", now - timedelta(hours=2))

    first = await repository.claim_due_seasons(1)
    assert [target.competition.slug for target in first] == ["test-cup"]
    assert first[0].competition.format == "cup"
    assert first[0].source == cup().seasons["2026-2027"]

    second = await repository.claim_due_seasons(10)
    assert [target.competition.slug for target in second] == ["test-league"]
    assert await repository.claim_due_seasons(10) == []
    leased = await season_row(sessions, "test-league", "2026-2027")
    assert close_to(leased.next_check_at, now + timedelta(minutes=10))


async def test_claim_ignores_future_and_disabled_seasons(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league(), "test-cup": cup()})
    await set_next_check(sessions, "test-cup", datetime.now(UTC) + timedelta(hours=1))
    async with sessions.begin() as session:
        await session.execute(
            update(CompetitionSeasonRow)
            .where(CompetitionSeasonRow.season == "2026-2027")
            .where(
                CompetitionSeasonRow.competition_id
                == select(CompetitionRow.id)
                .where(CompetitionRow.slug == "test-league")
                .scalar_subquery()
            )
            .values(automatic_refresh_enabled=False)
        )

    assert await repository.claim_due_seasons(10) == []


async def test_queued_scrape_job_is_claimed_and_completed(
    repository: PostgresResultRepository,
) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition})
    source_info = competition.seasons["2026-2027"]

    run_id, status = await repository.enqueue_scrape(competition, source_info)
    queued = await repository.get_scrape_job(run_id)
    assert status == "queued"
    assert queued is not None and queued.status == "queued"
    assert queued.queued_at is not None and queued.started_at is None

    targets = await repository.claim_queued_scrape_runs(10)
    assert len(targets) == 1
    target = targets[0]
    assert target.run_id == run_id
    assert target.competition.slug == competition.slug
    assert target.source == source_info
    assert await repository.claim_queued_scrape_runs(10) == []
    assert await repository.claim_due_seasons(10) == []

    fixtures = [match("Legia", "Lech", datetime.now(UTC), score=(2, 1))]
    await repository.replace_competition(
        target.competition,
        target.source,
        fixtures,
        run_id=target.run_id,
    )
    completed = await repository.get_scrape_job(run_id)
    assert completed is not None and completed.status == "completed"
    assert completed.started_at is not None and completed.completed_at is not None
    assert completed.matches_found == 1


async def test_active_scrape_job_is_deduplicated_and_source_conflicts(
    repository: PostgresResultRepository,
) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition})
    source_info = competition.seasons["2026-2027"]

    first = await repository.enqueue_scrape(competition, source_info)
    second = await repository.enqueue_scrape(competition, source_info)
    assert second == first

    changed_source = source("2026-2027", 99)
    with pytest.raises(ActiveScrapeJobConflict):
        await repository.enqueue_scrape(competition, changed_source)


async def test_queued_scrape_failure_updates_the_existing_job(
    repository: PostgresResultRepository,
) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition})
    source_info = competition.seasons["2026-2027"]
    run_id, _ = await repository.enqueue_scrape(competition, source_info)
    target = (await repository.claim_queued_scrape_runs(1))[0]

    await repository.record_scrape_failure(
        target.competition,
        target.source,
        RuntimeError("upstream unavailable"),
        run_id=target.run_id,
    )

    failed = await repository.get_scrape_job(run_id)
    assert failed is not None and failed.status == "failed"
    assert failed.error_message == "upstream unavailable"
    assert len(await scrape_runs(sessions=repository.sessions)) == 1


async def test_claim_skips_seasons_locked_by_another_worker(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league(), "test-cup": cup()})
    locked = await season_row(sessions, "test-league", "2026-2027")

    async with sessions.begin() as other_worker:
        await other_worker.execute(
            select(CompetitionSeasonRow)
            .where(CompetitionSeasonRow.id == locked.id)
            .with_for_update()
        )
        targets = await asyncio.wait_for(repository.claim_due_seasons(10), timeout=5)

    assert [target.competition.slug for target in targets] == ["test-cup"]


async def test_get_results_filters_in_sql(repository: PostgresResultRepository) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition})
    played = datetime.now(UTC) - timedelta(days=7)
    await repository.replace_competition(
        competition,
        competition.seasons["2026-2027"],
        [
            match("Wisła Kraków", "Lech", played, (1, 0), round_number=1),
            match("Legia", "Wisła Płock", played + timedelta(days=7), (2, 2), round_number=2),
            match("Legia", "Lech", played + timedelta(days=8), (0, 1), round_number=3),
        ],
    )

    by_team, _ = await repository.get_results(team="wisła")
    assert [item.round for item in by_team] == [1, 2]
    by_round, _ = await repository.get_results(competition="TEST-LEAGUE", round_number=3)
    assert [(item.home_team, item.away_team) for item in by_round] == [("Legia", "Lech")]
    by_stage, _ = await repository.get_results(stage="kolejka 2")
    assert [item.round for item in by_stage] == [2]
    other_season, refreshed_at = await repository.get_results(season="2025-2026")
    assert other_season == [] and refreshed_at is None


async def test_scheduler_heartbeat(repository: PostgresResultRepository) -> None:
    assert await repository.latest_scheduler_heartbeat() is None
    started = datetime.now(UTC)
    await repository.heartbeat("worker-1", started)
    first = await repository.latest_scheduler_heartbeat()
    await repository.heartbeat("worker-1", started)
    second = await repository.latest_scheduler_heartbeat()
    assert first is not None and second is not None and second >= first


async def test_observability_queries(repository: PostgresResultRepository) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition, "test-cup": cup()})
    current = competition.seasons["2026-2027"]
    await repository.record_scrape_failure(competition, current, RuntimeError("first"))
    await repository.record_scrape_failure(competition, current, RuntimeError("second"))
    queued_id, _ = await repository.enqueue_scrape(competition, current)
    await repository.enqueue_scrape(cup(), cup().seasons["2026-2027"])
    started = datetime.now(UTC)
    await repository.heartbeat("old-worker", started - timedelta(days=1))
    await repository.heartbeat("new-worker", started)

    runs = await repository.list_scrape_runs(competition="TEST-LEAGUE")
    assert [(run.status, run.error_message) for run in runs] == [
        ("queued", None),
        ("failed", "second"),
        ("failed", "first"),
    ]
    assert runs[0].source_url == current.source_url
    page = await repository.list_scrape_runs(
        statuses=("failed",), limit=1, before_id=runs[1].job_id
    )
    assert [run.error_message for run in page] == ["first"]
    assert await repository.list_scrape_runs(season="2025-2026") == []

    [latest] = await repository.scheduler_workers(1)
    assert latest.worker_id == "new-worker"

    seasons = {state.competition: state for state in await repository.scheduled_seasons()}
    assert set(seasons) == {"test-league", "test-cup"}
    assert seasons["test-league"].season == "2026-2027"
    assert seasons["test-league"].consecutive_failures == 2
    assert seasons["test-league"].active_job_id == queued_id
    assert seasons["test-league"].fetched_at is None

    counts = {
        (item.competition, item.status): item.count for item in await repository.scrape_run_counts()
    }
    assert counts == {
        ("test-cup", "queued"): 1,
        ("test-league", "failed"): 2,
        ("test-league", "queued"): 1,
    }


async def test_runs_of_a_dead_worker_are_failed_and_the_season_is_claimed_again(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition, "test-cup": cup()})
    await set_next_check(sessions, "test-league", datetime.now(UTC) - timedelta(hours=1))
    await set_next_check(sessions, "test-cup", datetime.now(UTC) + timedelta(days=1))
    await repository.enqueue_scrape(competition, competition.seasons["2025-2026"])
    started = datetime.now(UTC) - timedelta(days=3)
    await repository.heartbeat("dead-worker", started)
    await repository.heartbeat("live-worker", started)
    [scheduled] = await repository.claim_due_seasons(10, worker_id="dead-worker")
    [queued] = await repository.claim_queued_scrape_runs(10, worker_id="dead-worker")
    async with sessions.begin() as session:
        await session.execute(
            update(SchedulerHeartbeatRow)
            .where(SchedulerHeartbeatRow.worker_id == "dead-worker")
            .values(heartbeat_at=started)
        )
    await set_next_check(sessions, "test-league", datetime.now(UTC) - timedelta(hours=1))
    assert await repository.claim_due_seasons(10, worker_id="live-worker") == []
    assert await repository.next_scheduled_refresh() == ScheduledRefresh(
        "test-league", "2026-2027", ANY, scheduled.run_id
    )

    abandoned = await repository.fail_abandoned_scrape_runs(timedelta(minutes=5))

    assert sorted(abandoned) == sorted(
        [
            AbandonedScrapeRun(scheduled.run_id, "test-league", "2026-2027", "dead-worker"),
            AbandonedScrapeRun(queued.run_id, "test-league", "2025-2026", "dead-worker"),
        ]
    )
    for run_id in (scheduled.run_id, queued.run_id):
        job = await repository.get_scrape_job(run_id)
        assert job is not None and job.status == "failed" and job.completed_at is not None
        assert "dead-worker" in (job.error_message or "")
    [reclaimed] = await repository.claim_due_seasons(10, worker_id="live-worker")
    assert reclaimed.competition.slug == "test-league"
    assert await repository.next_scheduled_refresh() == ScheduledRefresh(
        "test-league", "2026-2027", ANY, reclaimed.run_id
    )


async def test_runs_of_live_workers_are_not_failed(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league()})
    await set_next_check(sessions, "test-league", datetime.now(UTC) - timedelta(hours=1))
    await repository.heartbeat("live-worker", datetime.now(UTC))
    [target] = await repository.claim_due_seasons(10, worker_id="live-worker")
    async with sessions.begin() as session:
        await session.execute(
            update(ScrapeRunRow)
            .where(ScrapeRunRow.id == target.run_id)
            .values(started_at=datetime.now(UTC) - timedelta(hours=2))
        )

    assert await repository.fail_abandoned_scrape_runs(timedelta(minutes=5)) == []
    job = await repository.get_scrape_job(target.run_id)
    assert job is not None and job.status == "running"


async def test_running_runs_without_a_worker_fail_after_the_timeout(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league(), "test-cup": cup()})
    await set_next_check(sessions, "test-league", datetime.now(UTC) - timedelta(hours=1))
    await set_next_check(sessions, "test-cup", datetime.now(UTC) - timedelta(hours=2))
    old, recent = await repository.claim_due_seasons(10)
    async with sessions.begin() as session:
        await session.execute(
            update(ScrapeRunRow)
            .where(ScrapeRunRow.id == old.run_id)
            .values(started_at=datetime.now(UTC) - timedelta(hours=2))
        )

    abandoned = await repository.fail_abandoned_scrape_runs(timedelta(minutes=5))

    assert [run.run_id for run in abandoned] == [old.run_id]
    job = await repository.get_scrape_job(recent.run_id)
    assert job is not None and job.status == "running"


async def test_next_scheduled_refresh_is_none_without_automatic_refresh(
    repository: PostgresResultRepository,
) -> None:
    assert await repository.next_scheduled_refresh() is None


ROLLOVER_DAY = datetime(2027, 7, 5, 12, tzinfo=UTC)


async def test_claim_season_checks_leases_enabled_competitions(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league(), "test-cup": cup()})
    await repository.seed_competitions({"test-league": league()})
    now = datetime.now(UTC)

    claimed = await repository.claim_season_checks(10)

    assert [competition.slug for competition in claimed] == ["test-league"]
    assert set(claimed[0].seasons) == {"2025-2026", "2026-2027"}
    assert await repository.claim_season_checks(10) == []
    async with sessions() as session:
        row = await session.scalar(
            select(CompetitionRow).where(CompetitionRow.slug == "test-league")
        )
    assert close_to(row.next_season_check_at, now + timedelta(minutes=10))


async def test_season_check_adds_only_newer_valid_seasons(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league()})
    next_check = datetime.now(UTC) + timedelta(days=1)
    discovered = [
        source("2027-2028", 5),
        source("2026-2027", 77),
        source("2024-2025", 6),
        SeasonSource(season="2028", source_url=source("2028-2029", 8).source_url),
    ]

    result = await repository.apply_season_check(
        "test-league", discovered, next_check, now=datetime(2026, 11, 17, tzinfo=UTC)
    )

    assert result.added_seasons == ["2027-2028"]
    assert result.rejected_sources == ["2028"]
    assert result.rolled_over_to is None and result.missing_season is None
    current = await season_row(sessions, "test-league", "2026-2027")
    assert current.is_current and current.source_url.endswith("liga2.html")
    added = await season_row(sessions, "test-league", "2027-2028")
    assert not added.is_current and added.scheduler_state == "disabled"
    assert await season_row(sessions, "test-league", "2024-2025") is None
    async with sessions() as session:
        row = await session.scalar(
            select(CompetitionRow).where(CompetitionRow.slug == "test-league")
        )
    assert row.next_season_check_at == next_check


async def test_rollover_waits_until_the_current_season_is_finished(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    current_source = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})
    later = ROLLOVER_DAY + timedelta(days=1)
    await repository.replace_competition(
        competition, current_source, [match("A", "B", ROLLOVER_DAY + timedelta(days=3))]
    )

    waiting = await repository.apply_season_check(
        "test-league", [source("2027-2028", 5)], later, now=ROLLOVER_DAY
    )
    assert waiting.added_seasons == ["2027-2028"] and waiting.rolled_over_to is None

    await repository.replace_competition(
        competition, current_source, [match("A", "B", ROLLOVER_DAY - timedelta(days=3), (1, 0))]
    )
    rolled = await repository.apply_season_check("test-league", [], later, now=ROLLOVER_DAY)

    assert (rolled.rolled_over_from, rolled.rolled_over_to) == ("2026-2027", "2027-2028")
    previous = await season_row(sessions, "test-league", "2026-2027")
    assert not previous.is_current and not previous.automatic_refresh_enabled
    assert previous.scheduler_state == "disabled" and previous.next_check_at is None
    new_current = await season_row(sessions, "test-league", "2027-2028")
    assert new_current.is_current and new_current.automatic_refresh_enabled
    assert new_current.scheduler_state == "waiting_for_schedule"
    await set_next_check(sessions, "test-league", datetime.now(UTC) - timedelta(minutes=1))
    targets = await repository.claim_due_seasons(10)
    assert [target.source for target in targets] == [source("2027-2028", 5)]


async def test_manual_rollover_only_reports_a_missing_season(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league()})
    later = ROLLOVER_DAY + timedelta(days=1)

    missing = await repository.apply_season_check("test-league", [], later, now=ROLLOVER_DAY)
    await repository.apply_season_check("test-league", [source("2027-2028", 5)], later)
    manual = await repository.apply_season_check(
        "test-league", [], later, SchedulePolicy(season_rollover="manual"), now=ROLLOVER_DAY
    )

    assert missing.missing_season == "2027-2028" and missing.rolled_over_to is None
    assert manual.rolled_over_to is None and manual.missing_season is None
    assert (await season_row(sessions, "test-league", "2026-2027")).is_current


async def test_reseed_keeps_an_automatically_rolled_over_season(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league()})
    later = ROLLOVER_DAY + timedelta(days=1)
    await repository.apply_season_check(
        "test-league", [source("2027-2028", 5)], later, now=ROLLOVER_DAY
    )
    auto = frozenset({"test-league"})

    await repository.seed_competitions({"test-league": league()}, auto)
    competition = await repository.get_competition("test-league")
    assert competition is not None and competition.current_season == "2027-2028"
    assert competition.seasons["2027-2028"] == source("2027-2028", 5)

    newer = league()
    newer.seasons["2028-2029"] = source("2028-2029", 9)
    newer.current_season = "2028-2029"
    await repository.seed_competitions({"test-league": newer}, auto)
    assert (await season_row(sessions, "test-league", "2028-2029")).is_current

    await repository.seed_competitions({"test-league": league()})
    competition = await repository.get_competition("test-league")
    assert competition is not None and competition.current_season == "2026-2027"
    assert set(competition.seasons) == {"2025-2026", "2026-2027", "2027-2028", "2028-2029"}


async def test_failure_backoff_uses_the_competition_policy(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    await repository.seed_competitions({"test-league": competition})
    policy = SchedulePolicy(failure_backoff=(timedelta(minutes=2),))
    before = datetime.now(UTC)

    await repository.record_scrape_failure(
        competition, competition.seasons["2026-2027"], RuntimeError("boom"), policy=policy
    )

    row = await season_row(sessions, "test-league", "2026-2027")
    assert close_to(row.next_check_at, before + timedelta(minutes=2))


KICKOFF = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)


def v11_fixtures() -> list[ScrapedMatch]:
    return [
        match("Wisła Kraków", "Legia", KICKOFF, (2, 1)),
        match("Lech", "Wisła Płock", KICKOFF, (0, 0)),
        match("Legia", "Lech", KICKOFF + timedelta(days=7), round_number=2),
        match(
            "Wisła Płock", "Wisła Kraków", KICKOFF + timedelta(days=7), round_number=2
        ).model_copy(update={"status": "inactive"}),
    ]


async def stored_v11_fixtures(repository: PostgresResultRepository) -> dict[str, int]:
    competition = league()
    await repository.seed_competitions({competition.slug: competition})
    await repository.replace_competition(
        competition, competition.seasons["2026-2027"], v11_fixtures()
    )
    matches = await repository.find_matches(MatchQuery())
    return {f"{item.home_team_slug}:{item.away_team_slug}": item.id for item in matches}


async def match_ids(repository: PostgresResultRepository, **filters) -> list[int]:
    return [item.id for item in await repository.find_matches(MatchQuery(**filters))]


async def test_find_matches_filters_every_status(
    repository: PostgresResultRepository,
) -> None:
    by_teams = await stored_v11_fixtures(repository)
    krakow_legia, lech_plock, legia_lech, plock_krakow = by_teams.values()
    assert list(by_teams) == [
        "wisla-krakow:legia",
        "lech:wisla-plock",
        "legia:lech",
        "wisla-plock:wisla-krakow",
    ]
    matches = await repository.find_matches(MatchQuery(statuses=("scheduled", "inactive")))
    assert [item.status for item in matches] == ["scheduled", "inactive"]
    assert matches[0].home_goals is None and matches[0].home_cards is None
    assert matches[0].kickoff_at == KICKOFF + timedelta(days=7)

    assert await match_ids(repository, statuses=("finished",)) == [krakow_legia, lech_plock]
    assert await match_ids(
        repository, kickoff_from=KICKOFF + timedelta(days=1), descending=True
    ) == [plock_krakow, legia_lech]
    assert await match_ids(repository, kickoff_until=KICKOFF + timedelta(seconds=1)) == [
        krakow_legia,
        lech_plock,
    ]
    assert await match_ids(repository, round=2, stage="KOLEJKA 2") == [legia_lech, plock_krakow]
    assert await match_ids(repository, competition="TEST-LEAGUE", season="2025-2026") == []


async def test_find_matches_team_filter_uses_slugs_and_aliases(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    by_teams = await stored_v11_fixtures(repository)
    krakow_legia, lech_plock, legia_lech, plock_krakow = by_teams.values()
    # An exact canonical slug selects that team only, not every slug containing it.
    assert await match_ids(repository, team="wisla-krakow") == [krakow_legia, plock_krakow]
    # Otherwise the filter is a substring of an alias or of the slugified query.
    assert await match_ids(repository, team="wisla") == [krakow_legia, lech_plock, plock_krakow]
    assert await match_ids(repository, team="WISŁA P") == [lech_plock, plock_krakow]
    assert await match_ids(repository, team="%") == []
    async with sessions.begin() as session:
        legia = await session.get(TeamAliasRow, "legia")
        assert legia is not None
        session.add(
            TeamAliasRow(
                normalized_alias="legia warszawa", alias="Legia Warszawa", team_id=legia.team_id
            )
        )
    assert await match_ids(repository, team="Warszawa") == [krakow_legia, legia_lech]

    teams = await repository.list_teams(q="warszawa")
    assert [(team.slug, team.name, team.aliases) for team in teams] == [
        ("legia", "Legia", ["Legia", "Legia Warszawa"])
    ]


async def test_match_cursor_keyset_survives_a_refresh(
    repository: PostgresResultRepository,
) -> None:
    by_teams = await stored_v11_fixtures(repository)
    all_ids = list(by_teams.values())
    # Equal kickoff times are ordered by id, in both directions.
    assert await match_ids(repository) == all_ids
    assert await match_ids(repository, descending=True) == all_ids[::-1]

    first_page = await repository.find_matches(MatchQuery(), limit=2)
    last = first_page[-1]
    competition = league()
    await repository.replace_competition(
        competition, competition.seasons["2026-2027"], v11_fixtures()
    )
    after = (last.kickoff_at, last.id)
    assert await match_ids(repository, after=after) == all_ids[2:]
    tail = await repository.find_matches(MatchQuery(descending=True, after=after))
    assert [item.id for item in tail] == all_ids[:1]
    # Matches without a kickoff sort last; a null-kickoff keyset must still be accepted.
    assert await match_ids(repository, after=(None, 0)) == []
    assert await match_ids(repository, descending=True, after=(None, 0)) == all_ids[::-1]


async def test_standings_from_stored_finished_results(
    repository: PostgresResultRepository,
) -> None:
    competition = league()
    await repository.seed_competitions({competition.slug: competition})
    await repository.replace_competition(
        competition,
        competition.seasons["2026-2027"],
        [
            *v11_fixtures(),
            match("Legia", "Lech", KICKOFF + timedelta(days=3), (3, 0), round_number=3),
        ],
    )
    await repository.replace_competition(
        competition,
        competition.seasons["2025-2026"],
        [match("Lech", "Legia", KICKOFF - timedelta(days=365), (5, 0), season="2025-2026")],
    )
    finished = await repository.find_matches(
        MatchQuery(competition="test-league", season="2026-2027", statuses=("finished",))
    )
    table = [
        (row.team_slug, row.played, row.points, row.goals_for, row.goals_against, row.form)
        for row in compute_standings(finished)
    ]
    # Legia and Wisła Kraków both have 3 points; Legia has the better goal difference.
    assert table == [
        ("legia", 2, 3, 4, 2, "WL"),
        ("wisla-krakow", 1, 3, 2, 1, "W"),
        ("wisla-plock", 1, 1, 0, 0, "D"),
        ("lech", 2, 1, 0, 3, "LD"),
    ]
    assert await repository.list_stages("test-league", "2026-2027") == [
        "Kolejka 1",
        "Kolejka 3",
        "Kolejka 2",
    ]


async def test_freshness_and_teams_are_scoped_per_competition_season(
    repository: PostgresResultRepository,
) -> None:
    competitions = {"test-league": league(), "test-cup": cup()}
    await repository.seed_competitions(competitions)
    competition = competitions["test-league"]
    await repository.replace_competition(
        competition, competition.seasons["2026-2027"], v11_fixtures()
    )
    freshness = await repository.get_freshness("test-league")
    assert [(item.competition, item.season) for item in freshness] == [
        ("test-league", "2026-2027"),
        ("test-league", "2025-2026"),
    ]
    current, historical = freshness
    assert current.data_as_of is not None and current.next_update_at is not None
    assert current.automatic_refresh and current.scheduler_state != "disabled"
    assert current.source.provider == "90minut"
    assert current.source.url == "http://www.90minut.pl/liga/1/liga2.html"
    assert historical.data_as_of is None and historical.next_update_at is None
    assert not historical.automatic_refresh
    cup_freshness = await repository.get_freshness("test-cup", "2026-2027")
    assert [(item.competition, item.data_as_of) for item in cup_freshness] == [("test-cup", None)]
    assert len(await repository.get_freshness()) == 3

    assert [team.slug for team in await repository.list_teams("test-league")] == [
        "lech",
        "legia",
        "wisla-krakow",
        "wisla-plock",
    ]
    assert await repository.list_teams("test-league", "2025-2026") == []
    assert await repository.list_teams("test-cup") == []
    page = await repository.list_teams(limit=2, after_slug="legia")
    assert [team.slug for team in page] == ["wisla-krakow", "wisla-plock"]


FALLBACK_URL = "https://api.football-data.org/v4/competitions/PL/matches?season=2026"


def league_with_fallback() -> Competition:
    competition = league()
    competition.seasons["2026-2027"] = competition.seasons["2026-2027"].model_copy(
        update={"fallback_provider": "football-data", "fallback_url": FALLBACK_URL}
    )
    return competition


async def stored_matches(sessions: async_sessionmaker[AsyncSession]) -> dict[str, MatchRow]:
    async with sessions() as session:
        rows = (await session.scalars(select(MatchRow))).all()
    return {f"{row.home_team}-{row.away_team}": row for row in rows}


async def test_fallback_refresh_updates_fixtures_and_keeps_everything_else(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league_with_fallback()
    primary = competition.seasons["2026-2027"]
    fallback = primary.fallback()
    await repository.seed_competitions({"test-league": competition})
    kickoff = datetime.now(UTC) - timedelta(days=3)
    finished = match("Legia", "Lech", kickoff, (2, 1)).model_copy(
        update={"provider_match_id": "espn-1", "round": None}
    )
    upcoming = match("Lech", "Wisła", kickoff + timedelta(days=2)).model_copy(
        update={"provider_match_id": "espn-2", "round": None}
    )
    omitted = match("Wisła", "Legia", kickoff + timedelta(days=9)).model_copy(
        update={"provider_match_id": "espn-3", "round": None}
    )
    await repository.replace_competition(competition, primary, [finished, upcoming, omitted])
    before = await stored_matches(sessions)
    await repository.record_scrape_failure(competition, primary, RuntimeError("HTTP 503"))

    fallback_matches = [
        finished.model_copy(
            update={"provider_match_id": "fd-1", "home_cards": None, "round": 4, "stage": "RS"}
        ),
        match("Lech", "Wisła", kickoff + timedelta(days=2), (0, 3), round_number=5).model_copy(
            update={"provider_match_id": "fd-2", "home_cards": None}
        ),
        match("Legia", "Wisła", kickoff + timedelta(days=20), round_number=6).model_copy(
            update={"provider_match_id": "fd-4"}
        ),
    ]
    await repository.replace_competition(competition, primary, fallback_matches, fallback=fallback)

    after = await stored_matches(sessions)
    assert {key: row.id for key, row in before.items()} == {key: after[key].id for key in before}
    assert after["Legia-Lech"].home_cards == 3
    assert (after["Legia-Lech"].provider_match_id, after["Legia-Lech"].round) == ("espn-1", None)
    lech = after["Lech-Wisła"]
    assert (lech.status, lech.home_score, lech.away_score) == ("finished", 0, 3)
    assert (lech.provider_match_id, lech.source_url) == ("espn-2", upcoming.source_url)
    assert after["Wisła-Legia"].status == "scheduled"
    assert after["Legia-Wisła"].provider_match_id is None
    row = await season_row(sessions, "test-league", "2026-2027")
    assert (row.consecutive_failures, row.primary_consecutive_failures) == (0, 1)
    assert (row.source_url, row.provider) == (primary.source_url, "90minut")
    [freshness] = await repository.get_freshness("test-league", "2026-2027")
    assert (freshness.source.provider, freshness.source.url) == ("football-data", FALLBACK_URL)
    run = (await scrape_runs(sessions))[-1]
    assert (run.provider, run.fallback, run.source_url) == ("football-data", True, FALLBACK_URL)
    [state] = await repository.scheduled_seasons()
    assert state.fallback_active and state.primary_consecutive_failures == 1

    await repository.replace_competition(competition, primary, [finished, upcoming])

    recovered = await stored_matches(sessions)
    assert set(recovered) == {"Legia-Lech", "Lech-Wisła"}
    assert recovered["Lech-Wisła"].id == before["Lech-Wisła"].id
    [state] = await repository.scheduled_seasons()
    assert not state.fallback_active and state.primary_consecutive_failures == 0
    [freshness] = await repository.get_freshness("test-league", "2026-2027")
    assert freshness.source.provider == "90minut"


async def test_failed_fallback_refresh_does_not_count_as_another_primary_failure(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league_with_fallback()
    primary = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})

    assert await repository.record_scrape_failure(competition, primary, RuntimeError("a")) == 1
    retry_at = (await season_row(sessions, "test-league", "2026-2027")).next_check_at
    assert (
        await repository.record_scrape_failure(
            competition, primary, RuntimeError("b"), fallback=primary.fallback()
        )
        == 1
    )

    row = await season_row(sessions, "test-league", "2026-2027")
    assert (row.consecutive_failures, row.next_check_at) == (1, retry_at)
    assert [(run.provider, run.fallback) for run in await scrape_runs(sessions)] == [
        ("90minut", False),
        ("football-data", True),
    ]


async def test_an_empty_result_for_a_stored_season_fails_without_deleting(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    competition = league()
    season = competition.seasons["2026-2027"]
    await repository.seed_competitions({"test-league": competition})
    await repository.replace_competition(
        competition, season, [match("Legia", "Lech", datetime.now(UTC), (1, 0))]
    )

    with pytest.raises(EmptySeasonError, match="no matches"):
        await repository.replace_competition(competition, season, [])

    assert set(await stored_matches(sessions)) == {"Legia-Lech"}


async def test_season_check_fills_only_missing_fallback_sources(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.seed_competitions({"test-league": league_with_fallback()})
    fallbacks = [
        SeasonSource(
            season=season,
            source_url=f"https://api.football-data.org/v4/competitions/PL/matches?season={year}",
            provider="football-data",
        )
        for season, year in (("2025-2026", 2025), ("2026-2027", 2099), ("2030-2031", 2030))
    ]

    result = await repository.apply_season_check(
        "test-league",
        [],
        datetime.now(UTC) + timedelta(days=1),
        now=datetime(2026, 11, 17, tzinfo=UTC),
        fallbacks=fallbacks,
    )

    assert result.fallback_seasons == ["2025-2026"]
    assert result.rejected_sources == []
    previous = await season_row(sessions, "test-league", "2025-2026")
    assert (previous.fallback_provider, previous.fallback_url) == (
        "football-data",
        fallbacks[0].source_url,
    )
    current = await season_row(sessions, "test-league", "2026-2027")
    assert current.fallback_url == FALLBACK_URL
