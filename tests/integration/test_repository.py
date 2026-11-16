import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db_models import (
    CompetitionRow,
    CompetitionSeasonRow,
    MatchRow,
    ScrapeRunRow,
    TeamAliasRow,
)
from app.models import Competition, ScrapedMatch, SeasonSource
from app.repository import ActiveScrapeJobConflict, PostgresResultRepository


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
