import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.main as main_module
from app import access
from app.competitions import competition_summary, source_from_url
from app.config import load_config
from app.main import app
from app.models import (
    PROBLEM_TYPES,
    Freshness,
    FreshnessSource,
    Match,
    MatchResult,
    Problem,
    ScrapedMatch,
    ScrapeJobStatus,
    ScrapeRun,
    Team,
)
from app.observability import ScrapeRunCount, SeasonState, WorkerHeartbeat
from app.ratelimit import FixedWindowRateLimiter

AUTH_HEADERS = {"X-API-Key": "test-api-key"}
COMPETITIONS = load_config().to_competitions()


class FakeRepository:
    def __init__(self) -> None:
        self.competitions = {
            slug: competition.model_copy(deep=True) for slug, competition in COMPETITIONS.items()
        }
        self.results: dict[tuple[str, str], list[MatchResult]] = {}
        self.fetched: dict[str, set[str]] = {}
        self.refreshed_at: datetime | None = None
        self.jobs: dict[int, ScrapeJobStatus] = {}
        self.active_jobs: dict[tuple[str, str], int] = {}
        self.matches: list[Match] = []
        self.freshness: list[Freshness] = []
        self.heartbeat_at: datetime | None = None
        self.workers: list[WorkerHeartbeat] = []
        self.season_states: list[SeasonState] = []
        self.scrape_runs: list[ScrapeRun] = []
        self.run_counts: list[ScrapeRunCount] = []

    async def health(self) -> None:
        return None

    async def latest_scheduler_heartbeat(self):
        return self.heartbeat_at

    async def scheduler_workers(self, limit):
        return self.workers[:limit]

    async def scheduled_seasons(self):
        return self.season_states

    async def scrape_run_counts(self):
        return self.run_counts

    async def list_scrape_runs(
        self, competition=None, season=None, statuses=(), limit=None, before_id=None
    ):
        runs = [
            run
            for run in sorted(self.scrape_runs, key=lambda item: item.job_id, reverse=True)
            if (not competition or run.competition == competition)
            and (not season or run.season == season)
            and (not statuses or run.status in statuses)
            and (before_id is None or run.job_id < before_id)
        ]
        return runs if limit is None else runs[:limit]

    async def seed_competitions(self, _seeds, _auto_rollover=frozenset()) -> None:
        return None

    async def get_competition(self, slug: str):
        return self.competitions.get(slug.casefold())

    async def list_competitions(self):
        return [
            competition_summary(item, self.fetched.get(item.slug, set()))
            for item in self.competitions.values()
        ]

    async def get_results(
        self,
        competition=None,
        season=None,
        round_number=None,
        stage=None,
        team=None,
    ):
        results = [item for values in self.results.values() for item in values]
        if competition:
            results = [item for item in results if item.competition == competition]
        if season:
            results = [item for item in results if item.season == season]
        if round_number:
            results = [item for item in results if item.round == round_number]
        if stage:
            results = [item for item in results if stage.casefold() in item.stage.casefold()]
        if team:
            query = team.casefold()
            results = [
                item
                for item in results
                if query in item.home_team.casefold() or query in item.away_team.casefold()
            ]
        return results, self.refreshed_at

    async def find_matches(self, query, limit=None):
        def key(item: Match) -> tuple[datetime, int]:
            return item.kickoff_at or datetime.max.replace(tzinfo=UTC), item.id

        def plays(item: Match) -> bool:
            needle = query.team.casefold()
            slugs = {item.home_team_slug, item.away_team_slug}
            if any(needle == match.home_team_slug for match in self.matches) or any(
                needle == match.away_team_slug for match in self.matches
            ):
                return needle in slugs
            names = (item.home_team, item.away_team, *slugs)
            return any(needle in name.casefold() for name in names)

        items = [
            item
            for item in self.matches
            if (not query.competition or item.competition == query.competition)
            and (not query.season or item.season == query.season)
            and (not query.statuses or item.status in query.statuses)
            and (query.kickoff_from is None or item.kickoff_at >= query.kickoff_from)
            and (query.kickoff_until is None or item.kickoff_at < query.kickoff_until)
            and (query.round is None or item.round == query.round)
            and (not query.stage or query.stage.casefold() in item.stage.casefold())
            and (not query.team or plays(item))
        ]
        items.sort(key=key, reverse=query.descending)
        if query.after is not None:
            after = (query.after[0] or datetime.max.replace(tzinfo=UTC), query.after[1])
            items = [
                item
                for item in items
                if (key(item) < after if query.descending else key(item) > after)
            ]
        return items if limit is None else items[:limit]

    async def list_stages(self, competition, season):
        return list(
            dict.fromkeys(
                item.stage
                for item in self.matches
                if item.competition == competition and item.season == season
            )
        )

    async def get_freshness(self, competition=None, season=None):
        return [
            item
            for item in self.freshness
            if (not competition or item.competition == competition)
            and (not season or item.season == season)
        ]

    async def list_teams(self, competition=None, season=None, q=None, limit=None, after_slug=None):
        teams: dict[str, set[str]] = {}
        for item in self.matches:
            if (competition and item.competition != competition) or (
                season and item.season != season
            ):
                continue
            teams.setdefault(item.home_team_slug, set()).add(item.home_team)
            teams.setdefault(item.away_team_slug, set()).add(item.away_team)
        result = [
            Team(slug=slug, name=min(aliases), aliases=sorted(aliases))
            for slug, aliases in sorted(teams.items())
            if (after_slug is None or slug > after_slug)
            and (not q or q == slug or any(q.casefold() in a.casefold() for a in aliases))
        ]
        return result if limit is None else result[:limit]

    async def is_cached(self, competition: str, season: str) -> bool:
        return season in self.fetched.get(competition, set())

    async def enqueue_scrape(self, competition, source):
        key = competition.slug, source.season
        active_id = self.active_jobs.get(key)
        if active_id is not None:
            return active_id, self.jobs[active_id].status
        job_id = max(self.jobs, default=0) + 1
        self.jobs[job_id] = ScrapeJobStatus(
            job_id=job_id,
            competition=competition.slug,
            season=source.season,
            status="queued",
            queued_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            matches_found=None,
            error_message=None,
        )
        self.active_jobs[key] = job_id
        return job_id, "queued"

    async def get_scrape_job(self, job_id):
        return self.jobs.get(job_id)

    async def replace_competition(self, competition, source, fresh, run_id=None):
        competition.seasons[source.season] = source
        self.results[(competition.slug, source.season)] = [
            result for item in fresh if (result := item.as_result()) is not None
        ]
        self.fetched.setdefault(competition.slug, set()).add(source.season)
        self.refreshed_at = datetime.now(UTC)
        if run_id is not None:
            job = self.jobs[run_id]
            self.jobs[run_id] = job.model_copy(
                update={
                    "status": "completed",
                    "started_at": job.queued_at,
                    "completed_at": self.refreshed_at,
                    "matches_found": len(fresh),
                }
            )
            self.active_jobs.pop((competition.slug, source.season), None)
        return self.refreshed_at


@pytest.fixture(autouse=True)
def configured_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module.settings, "api_keys", "test-api-key")


@pytest.fixture(autouse=True)
def fresh_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "rate_limiter", FixedWindowRateLimiter())


def client_with_empty_store(_tmp_path: Path) -> tuple[TestClient, FakeRepository]:
    repository = FakeRepository()
    main_module.store = repository
    return TestClient(app), repository


def test_empty_database(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.get("/api/v1/results")
    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["stale"] is True


def test_health_checks_database(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    assert client.get("/health").json() == {
        "status": "ok",
        "database": "ok",
        "scheduler": "not_started",
    }


def test_lists_configured_competitions(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.get("/api/v1/competitions")
    assert response.status_code == 200
    assert {item["slug"] for item in response.json()} == {
        "ekstraklasa",
        "pierwsza-liga",
        "europa-league",
        "premier-league",
        "3-liga",
    }
    ekstraklasa = next(item for item in response.json() if item["slug"] == "ekstraklasa")
    assert ekstraklasa["current_season"] == "2026-2027"
    assert ekstraklasa["available_seasons"] == ["2026-2027", "2025-2026"]
    assert ekstraklasa["fetched_seasons"] == []


def test_unknown_competition_is_rejected(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.get("/api/v1/results?competition=la-liga")
    assert response.status_code == 404


def test_refresh_rejects_removed_round_response_filters(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/refresh",
        json={
            "competition": "europa-league",
            "first_round": 1,
            "last_round": 2,
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert response.json()["errors"][0]["type"] == "extra_forbidden"


def test_refresh_requires_competition(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post("/api/v1/results/refresh", json={}, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert response.json()["errors"][0]["loc"] == ["body", "competition"]


def test_refresh_rejects_historical_season(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/refresh",
        json={"competition": "ekstraklasa", "season": "2025-2026"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert "history/fetch" in response.json()["detail"]


def test_write_endpoints_require_a_configured_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = client_with_empty_store(tmp_path)
    monkeypatch.setattr(main_module.settings, "api_keys", "")

    disabled = client.post(
        "/api/v1/results/refresh",
        json={"competition": "ekstraklasa"},
    )
    assert disabled.status_code == 503

    monkeypatch.setattr(main_module.settings, "api_keys", "test-api-key")
    missing_key = client.post(
        "/api/v1/results/refresh",
        json={"competition": "ekstraklasa"},
    )
    invalid_key = client.post(
        "/api/v1/results/history/fetch",
        json={"competition": "ekstraklasa", "season": "2025-2026"},
        headers={"X-API-Key": "wrong"},
    )
    assert missing_key.status_code == invalid_key.status_code == 401


def test_refresh_is_queued_and_status_is_protected(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)

    response = client.post(
        "/api/v1/results/refresh",
        json={"competition": "ekstraklasa"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 202
    assert response.json() == {
        "job_id": 1,
        "status": "queued",
        "status_url": "/api/v1/jobs/1",
    }
    assert response.headers["location"] == "/api/v1/jobs/1"
    job = client.get("/api/v1/jobs/1", headers=AUTH_HEADERS).json()
    assert (job["status"], job["matches_skipped"]) == ("queued", 0)
    assert client.get("/api/v1/jobs/1").status_code == 401
    assert client.get("/api/v1/jobs/999", headers=AUTH_HEADERS).status_code == 404


def match_for(competition, source) -> ScrapedMatch:
    return ScrapedMatch(
        competition=competition.slug,
        season=source.season,
        stage="Kolejka 1",
        round=1,
        home_team="Team A",
        away_team="Team B",
        status="finished",
        home_score=2,
        away_score=1,
        home_goals=2,
        away_goals=1,
        played_at=datetime(2026, 7, 25, 17, 30, tzinfo=UTC),
        source_url=source.source_url,
    )


async def test_historical_fetch_is_queued_and_cached_requests_return_results(
    tmp_path: Path,
) -> None:
    class FakeScraper:
        calls = 0

        async def scrape(self, competition, source):
            self.calls += 1
            return [match_for(competition, source)]

    fake = FakeScraper()
    main_module.scraper = fake
    client, repository = client_with_empty_store(tmp_path)
    payload = {"competition": "ekstraklasa", "season": "2025-2026"}

    first = client.post("/api/v1/results/history/fetch", json=payload, headers=AUTH_HEADERS)

    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert fake.calls == 0
    competition = repository.competitions["ekstraklasa"]
    source = competition.seasons["2025-2026"]
    await repository.replace_competition(
        competition, source, [match_for(competition, source)], run_id=first.json()["job_id"]
    )
    cached = client.post("/api/v1/results/history/fetch", json=payload, headers=AUTH_HEADERS)
    assert cached.status_code == 200
    assert cached.json()["fetched"] is False
    assert cached.json()["results"][0]["home_goals"] == 2
    assert cached.json()["results"][0]["home_corners"] is None

    forced = client.post(
        "/api/v1/results/history/fetch",
        json={**payload, "force_refresh": True},
        headers=AUTH_HEADERS,
    )
    assert forced.status_code == 202
    assert forced.json()["status"] == "queued"
    assert fake.calls == 0


def test_unknown_historical_season_is_queued(tmp_path: Path) -> None:
    class FakeScraper:
        async def scrape(self, competition, source):
            return [match_for(competition, source)]

    main_module.scraper = FakeScraper()
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={
            "competition": "ekstraklasa",
            "season": "2024-2025",
            "source_url": "http://www.90minut.pl/liga/1/liga13482.html",
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 202
    job = client.get(response.json()["status_url"], headers=AUTH_HEADERS)
    assert job.json()["season"] == "2024-2025"


def test_unknown_historical_season_requires_source(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={"competition": "ekstraklasa", "season": "2024-2025"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert "provide source_url" in response.json()["detail"]


def test_historical_source_rejects_arbitrary_url(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={
            "competition": "ekstraklasa",
            "season": "2024-2025",
            "source_url": "http://127.0.0.1/private",
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert "supported 90minut page" in response.json()["detail"]


def test_historical_source_rejects_provider_from_another_competition(
    tmp_path: Path,
) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={
            "competition": "ekstraklasa",
            "season": "2024-2025",
            "source_url": ("https://api.football-data.org/v4/competitions/PL/matches?season=2024"),
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert "not configured for ekstraklasa" in response.json()["detail"]


def test_historical_season_must_match_the_competition_season_format(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={"competition": "ekstraklasa", "season": "2024"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert "split format" in response.json()["detail"]


def test_calendar_historical_season_is_queued(tmp_path: Path) -> None:
    client, repository = client_with_empty_store(tmp_path)
    repository.competitions["premier-league"] = repository.competitions[
        "premier-league"
    ].model_copy(
        update={
            "season_format": "calendar",
            "season_start_month": 1,
            "current_season": "2025",
            "seasons": {
                "2025": source_from_url(
                    "2025",
                    "https://api.football-data.org/v4/competitions/PL/matches?season=2025",
                )
            },
        }
    )
    response = client.post(
        "/api/v1/results/history/fetch",
        json={
            "competition": "premier-league",
            "season": "2024",
            "source_url": "https://api.football-data.org/v4/competitions/PL/matches?season=2024",
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 202
    summary = next(
        item
        for item in client.get("/api/v1/competitions").json()
        if item["slug"] == "premier-league"
    )
    assert (summary["season_format"], summary["available_seasons"]) == ("calendar", ["2025"])


def test_historical_source_rejects_another_provider_competition(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={
            "competition": "premier-league",
            "season": "2023-2024",
            "source_url": (
                "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard?season=2023"
            ),
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert "code ger.1 is not configured" in response.json()["detail"]


KICKOFF = datetime(2026, 8, 1, 18, 0, tzinfo=UTC)


def slug(name: str) -> str:
    return name.casefold().translate(str.maketrans("łó ", "lo-"))


def fixture(
    match_id: int,
    home: str,
    away: str,
    goals: tuple[int, int] | None = None,
    *,
    days: int = 0,
    status: str | None = None,
    competition: str = "ekstraklasa",
    season: str = "2026-2027",
    stage: str | None = None,
    round_number: int = 1,
) -> Match:
    home_goals, away_goals = goals if goals else (None, None)
    return Match(
        id=match_id,
        competition=competition,
        season=season,
        stage=stage or f"Kolejka {round_number}",
        round=round_number,
        status=status or ("finished" if goals else "scheduled"),
        kickoff_at=KICKOFF + timedelta(days=days),
        home_team=home,
        away_team=away,
        home_team_slug=slug(home),
        away_team_slug=slug(away),
        home_goals=home_goals,
        away_goals=away_goals,
        source_url="http://www.90minut.pl/mecz.php?id_mecz=1",
    )


def client_with_matches(tmp_path: Path) -> tuple[TestClient, FakeRepository]:
    client, repository = client_with_empty_store(tmp_path)
    repository.matches = [
        fixture(1, "Wisła Płock", "Legia", (2, 0), days=0),
        fixture(2, "Lech", "Wisła Kraków", (1, 1), days=0),
        fixture(3, "Legia", "Lech", (0, 1), days=7, round_number=2),
        fixture(4, "Wisła Kraków", "Wisła Płock", days=14, round_number=3),
        fixture(5, "Lech", "Wisła Płock", days=14, round_number=3, status="inactive"),
    ]
    return client, repository


def ids(response) -> list[int]:
    assert response.status_code == 200, response.json()
    return [item["id"] for item in response.json()["data"]]


def test_matches_include_every_status_with_null_scores_for_fixtures(tmp_path: Path) -> None:
    client, _ = client_with_matches(tmp_path)
    data = client.get("/api/v1/matches").json()["data"]
    assert [item["id"] for item in data] == [1, 2, 3, 4, 5]
    scheduled, inactive = data[3], data[4]
    assert (scheduled["status"], inactive["status"]) == ("scheduled", "inactive")
    assert scheduled["home_goals"] is None and scheduled["home_cards"] is None
    assert data[0]["kickoff_at"] == "2026-08-01T18:00:00Z"
    assert "home_score" not in data[0] and "played_at" not in data[0]


def test_matches_filters(tmp_path: Path) -> None:
    client, _ = client_with_matches(tmp_path)
    assert ids(client.get("/api/v1/matches?status=scheduled,inactive")) == [4, 5]
    assert ids(client.get("/api/v1/matches?date_from=2026-08-08&date_to=2026-08-08")) == [3]
    assert ids(client.get("/api/v1/matches?date_to=2026-08-01T18:00:00Z")) == [1, 2]
    assert ids(client.get("/api/v1/matches?round=3&order=desc")) == [5, 4]
    assert ids(client.get("/api/v1/matches?stage=kolejka%202")) == [3]
    # An exact slug selects one team; anything else is a name substring.
    assert ids(client.get("/api/v1/matches?team=wisla-plock")) == [1, 4, 5]
    assert ids(client.get("/api/v1/matches?team=Wisła")) == [1, 2, 4, 5]
    assert ids(client.get("/api/v1/matches?competition=premier-league")) == []


def test_matches_reject_invalid_filters_with_valid_values(tmp_path: Path) -> None:
    client, _ = client_with_matches(tmp_path)
    status = client.get("/api/v1/matches?status=finished,live")
    assert status.status_code == 422
    assert status.json()["invalid"] == ["live"]
    assert status.json()["valid_values"] == ["finished", "scheduled", "inactive"]
    assert client.get("/api/v1/matches?date_from=yesterday").status_code == 422
    assert client.get("/api/v1/matches?competition=la-liga").status_code == 404
    season = client.get("/api/v1/matches?competition=ekstraklasa&season=2019-2020")
    assert season.status_code == 404
    assert season.json()["valid_values"] == ["2026-2027", "2025-2026"]
    assert client.get("/api/v1/matches?limit=201").status_code == 422


def test_matches_cursor_pagination(tmp_path: Path) -> None:
    client, _ = client_with_matches(tmp_path)
    seen: list[int] = []
    url = "/api/v1/matches?order=desc&limit=2"
    cursor = None
    for _ in range(5):
        response = client.get(url + (f"&cursor={cursor}" if cursor else ""))
        assert response.json()["pagination"]["limit"] == 2
        seen += ids(response)
        cursor = response.json()["pagination"]["next_cursor"]
        if cursor is None:
            break
    assert seen == [5, 4, 3, 2, 1]

    first = client.get("/api/v1/matches?limit=2").json()["pagination"]["next_cursor"]
    # The page size may change; the filters may not.
    assert ids(client.get(f"/api/v1/matches?limit=3&cursor={first}")) == [3, 4, 5]
    other = client.get(f"/api/v1/matches?limit=2&status=finished&cursor={first}")
    assert other.status_code == 400
    assert "different filters" in other.json()["detail"]
    assert client.get("/api/v1/matches?cursor=not-a-cursor").status_code == 400


def test_matches_fields_select_compact_items(tmp_path: Path) -> None:
    client, _ = client_with_matches(tmp_path)
    response = client.get("/api/v1/matches?fields=home_team_slug,away_goals&limit=1")
    assert response.json()["data"] == [{"id": 1, "home_team_slug": "wisla-plock", "away_goals": 0}]
    invalid = client.get("/api/v1/matches?fields=id,score")
    assert invalid.status_code == 422
    assert invalid.json()["invalid"] == ["score"]
    assert "kickoff_at" in invalid.json()["valid_values"]


def test_freshness_is_reported_per_competition_season(tmp_path: Path) -> None:
    client, repository = client_with_matches(tmp_path)
    fetched = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)
    repository.freshness = [
        Freshness(
            competition="ekstraklasa",
            season="2026-2027",
            data_as_of=fetched,
            next_update_at=fetched + timedelta(days=1),
            automatic_refresh=True,
            scheduler_state="idle",
            source=FreshnessSource(provider="90minut", url="http://www.90minut.pl/liga/1/l.html"),
        ),
        Freshness(
            competition="premier-league",
            season="2026-2027",
            data_as_of=fetched + timedelta(hours=5),
            next_update_at=None,
            automatic_refresh=False,
            scheduler_state="disabled",
            source=FreshnessSource(provider="football-data", url="https://example.invalid"),
        ),
    ]
    freshness = client.get("/api/v1/matches?competition=ekstraklasa").json()["freshness"]
    assert freshness == [
        {
            "competition": "ekstraklasa",
            "season": "2026-2027",
            "data_as_of": "2026-08-01T20:00:00Z",
            "next_update_at": "2026-08-02T20:00:00Z",
            "automatic_refresh": True,
            "scheduler_state": "idle",
            "source": {"provider": "90minut", "url": "http://www.90minut.pl/liga/1/l.html"},
        }
    ]
    everything = client.get("/api/v1/matches").json()["freshness"]
    assert [item["competition"] for item in everything] == ["ekstraklasa", "premier-league"]


def test_standings_rank_by_points_goal_difference_goals_and_name(tmp_path: Path) -> None:
    client, repository = client_with_empty_store(tmp_path)
    repository.matches = [
        fixture(1, "Alpha", "Delta", (3, 0)),
        fixture(2, "Bravo", "Echo", (1, 0)),
        fixture(3, "Charlie", "Foxtrot", (2, 1)),
        fixture(4, "Delta", "Bravo", (0, 0), days=7),
        fixture(5, "Echo", "Charlie", (0, 0), days=7),
        fixture(6, "Foxtrot", "Alpha", (1, 1), days=7),
        fixture(7, "Alpha", "Bravo", days=14),
        fixture(8, "Hotel", "Golf", (0, 0), days=21),
    ]
    response = client.get("/api/v1/standings?competition=ekstraklasa")
    assert response.status_code == 200
    body = response.json()
    assert (body["season"], body["stage"]) == ("2026-2027", None)
    table = [(row["team_slug"], row["points"], row["goal_difference"]) for row in body["data"]]
    # Charlie beats Bravo on goals for; Golf beats Hotel on name after a full tie.
    assert table == [
        ("alpha", 4, 3),
        ("charlie", 4, 1),
        ("bravo", 4, 1),
        ("golf", 1, 0),
        ("hotel", 1, 0),
        ("foxtrot", 1, -1),
        ("echo", 1, -1),
        ("delta", 1, -3),
    ]
    assert [row["position"] for row in body["data"]] == list(range(1, 9))
    alpha = body["data"][0]
    assert (alpha["played"], alpha["won"], alpha["drawn"], alpha["form"]) == (2, 1, 1, "DW")
    compact_rows = client.get("/api/v1/standings?competition=ekstraklasa&fields=points").json()
    assert compact_rows["data"][0] == {"team_slug": "alpha", "points": 4}


def test_cup_standings_require_a_stage(tmp_path: Path) -> None:
    client, repository = client_with_empty_store(tmp_path)
    repository.matches = [
        fixture(1, "A", "B", (1, 0), competition="europa-league", stage="Grupa A"),
        fixture(2, "C", "D", (1, 0), competition="europa-league", stage="Grupa B"),
    ]
    missing = client.get("/api/v1/standings?competition=europa-league")
    assert missing.status_code == 422
    assert missing.json()["valid_values"] == ["Grupa A", "Grupa B"]
    group = client.get("/api/v1/standings?competition=europa-league&stage=grupa%20b").json()
    assert [row["team_slug"] for row in group["data"]] == ["c", "d"]
    assert group["stage"] == "grupa b"


def test_standings_validate_competition_and_season(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    assert client.get("/api/v1/standings").status_code == 422
    assert client.get("/api/v1/standings?competition=la-liga").status_code == 404
    assert (
        client.get("/api/v1/standings?competition=ekstraklasa&season=2019-2020").status_code == 404
    )


def test_teams_list_aliases_with_cursor_pagination(tmp_path: Path) -> None:
    client, repository = client_with_matches(tmp_path)
    repository.matches.append(
        fixture(6, "Legia Warszawa", "Lech", (1, 0), days=21).model_copy(
            update={"home_team_slug": "legia"}
        )
    )
    first = client.get("/api/v1/teams?limit=2").json()
    assert [team["slug"] for team in first["data"]] == ["lech", "legia"]
    assert first["data"][1]["aliases"] == ["Legia", "Legia Warszawa"]
    cursor = first["pagination"]["next_cursor"]
    rest = client.get(f"/api/v1/teams?limit=10&cursor={cursor}").json()
    assert [team["slug"] for team in rest["data"]] == ["wisla-krakow", "wisla-plock"]
    assert rest["pagination"]["next_cursor"] is None
    assert client.get(f"/api/v1/teams?q=lech&cursor={cursor}").status_code == 400
    assert [t["slug"] for t in client.get("/api/v1/teams?q=Warszawa").json()["data"]] == ["legia"]


PROBLEM_MEDIA_TYPE = "application/problem+json"


def assert_problem(response, status_code: int, problem_type: str) -> dict:
    assert response.status_code == status_code, response.text
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    Problem.model_validate(body)
    assert body["status"] == status_code
    assert body["type"] == f"/problems/{problem_type}"
    return body


def test_every_operation_has_a_unique_snake_case_operation_id_and_tag() -> None:
    routes = [
        route for route in app.routes if isinstance(route, APIRoute) and route.include_in_schema
    ]
    assert routes
    operation_ids = [route.operation_id for route in routes]
    assert all(operation_ids), [route.path for route in routes if not route.operation_id]
    assert len(set(operation_ids)) == len(operation_ids)
    assert all(re.fullmatch(r"[a-z]+(_[a-z]+)*", item) for item in operation_ids)
    declared_tags = {tag["name"] for tag in app.openapi()["tags"]}
    for path in app.openapi()["paths"].values():
        for operation in path.values():
            assert operation["operationId"] in operation_ids
            assert operation["summary"] and operation["description"]
            assert len(operation["tags"]) == 1 and operation["tags"][0] in declared_tags


def test_openapi_documents_response_schemas_and_error_examples() -> None:
    paths = app.openapi()["paths"]
    for path, model in [
        ("/api/v1/matches", "MatchesResponse"),
        ("/api/v1/standings", "StandingsResponse"),
        ("/api/v1/teams", "TeamsResponse"),
    ]:
        schema = paths[path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{model}"}
        assert all(p.get("description") for p in paths[path]["get"]["parameters"])
    history = paths["/api/v1/results/history/fetch"]["post"]["responses"]
    assert history["202"]["content"]["application/json"]["schema"]["$ref"].endswith("JobAccepted")
    assert paths["/api/v1/results"]["get"]["deprecated"] is True

    problem_schema = app.openapi()["components"]["schemas"]["Problem"]
    problem_types = set(problem_schema["properties"]["type"]["enum"])
    for path_name, path in paths.items():
        for operation in path.values():
            responses = operation["responses"]
            if path_name != "/health":
                assert "429" in responses, operation["operationId"]
                assert "Retry-After" in responses["429"]["headers"]
            for code, response in responses.items():
                if int(code) < 400:
                    continue
                content = response["content"]
                assert list(content) == [PROBLEM_MEDIA_TYPE], (operation["operationId"], code)
                assert content[PROBLEM_MEDIA_TYPE]["schema"] == {
                    "$ref": "#/components/schemas/Problem"
                }
                for name, example in content[PROBLEM_MEDIA_TYPE]["examples"].items():
                    Problem.model_validate(example["value"])
                    assert example["value"]["status"] == int(code), (operation["operationId"], name)
                    assert example["value"]["type"] in problem_types
    for path in ["/api/v1/matches", "/api/v1/standings", "/api/v1/teams", "/api/v1/competitions"]:
        responses = paths[path]["get"]["responses"]
        assert {"ETag", "Cache-Control", "RateLimit", "RateLimit-Policy"} <= set(
            responses["200"]["headers"]
        )
        assert "content" not in responses["304"]


def test_error_bodies_match_the_documented_error_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = client_with_matches(tmp_path)
    cursor = client.get("/api/v1/matches?limit=1").json()["pagination"]["next_cursor"]
    requests = [
        ("get", "/api/v1/matches?cursor=nope", None, {}),
        ("get", f"/api/v1/matches?status=finished&cursor={cursor}", None, {}),
        ("get", "/api/v1/matches?competition=la-liga", None, {}),
        ("get", "/api/v1/matches?competition=ekstraklasa&season=2019-2020", None, {}),
        ("get", "/api/v1/matches?fields=score", None, {}),
        ("get", "/api/v1/matches?status=live", None, {}),
        ("get", "/api/v1/matches?date_to=soon", None, {}),
        ("get", "/api/v1/matches?limit=0", None, {}),
        ("get", "/api/v1/standings?competition=europa-league", None, {}),
        ("get", "/api/v1/standings", None, {}),
        ("get", f"/api/v1/teams?q=x&cursor={cursor}", None, {}),
        ("get", "/api/v1/results?competition=la-liga", None, {}),
        ("get", "/api/v1/jobs/999", None, AUTH_HEADERS),
        ("get", "/api/v1/jobs/1", None, {}),
        ("post", "/api/v1/results/refresh", {"competition": "la-liga"}, AUTH_HEADERS),
        ("post", "/api/v1/results/refresh", {}, AUTH_HEADERS),
        (
            "post",
            "/api/v1/results/history/fetch",
            {"competition": "ekstraklasa", "season": "2019-2020"},
            AUTH_HEADERS,
        ),
    ]
    paths = app.openapi()["paths"]
    for method, url, body, headers in requests:
        response = client.request(method, url, json=body, headers=headers)
        route = next(
            route
            for route in app.routes
            if isinstance(route, APIRoute)
            and method.upper() in route.methods
            and route.path_regex.match(url.split("?")[0])
        )
        operation = paths[route.path_format][method]
        assert str(response.status_code) in operation["responses"], (url, response.status_code)
        problem = assert_problem(response, response.status_code, response.json()["type"][10:])
        assert problem["instance"] == url.split("?")[0]
        assert PROBLEM_MEDIA_TYPE in operation["responses"][str(response.status_code)]["content"]

    monkeypatch.setattr(main_module.settings, "api_keys", "")
    disabled = client.post("/api/v1/results/refresh", json={"competition": "ekstraklasa"})
    assert_problem(disabled, 503, "writes-disabled")


def test_errors_are_problem_details_with_hints(tmp_path: Path) -> None:
    client, _ = client_with_matches(tmp_path)
    unknown = assert_problem(
        client.get("/api/v1/matches?competition=la-liga"), 404, "unknown-competition"
    )
    assert unknown["title"] == "Unknown competition"
    assert unknown["detail"] == "Unknown competition: la-liga"
    assert unknown["instance"] == "/api/v1/matches"
    assert unknown["parameter"] == "competition"
    assert "ekstraklasa" in unknown["valid_values"]
    assert unknown["hint"]

    validation = assert_problem(client.get("/api/v1/matches?limit=0"), 422, "validation-error")
    assert validation["parameter"] == "limit"
    assert validation["detail"].startswith("limit: ")
    assert validation["errors"] == [
        {
            "loc": ["query", "limit"],
            "msg": "Input should be greater than or equal to 1",
            "type": "greater_than_equal",
        }
    ]
    assert "detail" not in validation["errors"][0]

    unauthorized = client.get("/api/v1/jobs/1")
    assert_problem(unauthorized, 401, "unauthorized")
    assert unauthorized.headers["www-authenticate"] == "ApiKey"
    assert_problem(client.get("/api/v1/nope"), 404, "not-found")
    assert_problem(client.delete("/api/v1/matches"), 405, "method-not-allowed")


def test_unexpected_errors_are_problem_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, repository = client_with_matches(tmp_path)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(repository, "list_competitions", broken)
    response = TestClient(app, raise_server_exceptions=False).get("/api/v1/competitions")
    body = assert_problem(response, 500, "internal-error")
    assert "boom" not in body["detail"]
    assert client.get("/health").status_code == 200


def test_rate_limit_headers_and_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_with_matches(tmp_path)
    monkeypatch.setattr(main_module.settings, "rate_limit_read_per_minute", 2)
    first = client.get("/api/v1/competitions")
    assert first.headers["ratelimit-policy"] == '"read";q=2;w=60'
    assert first.headers["ratelimit"] == '"read";r=1;t=60'
    assert client.get("/api/v1/matches?competition=la-liga").status_code == 404

    limited = client.get("/api/v1/competitions")
    body = assert_problem(limited, 429, "rate-limited")
    assert limited.headers["retry-after"] == "60"
    assert limited.headers["ratelimit"] == '"read";r=0;t=60'
    assert body["hint"] == "Retry after 60 seconds."

    health = client.get("/health")
    assert health.status_code == 200 and "ratelimit" not in health.headers
    assert client.get("/openapi.json").status_code == 200
    # Writes have their own policy, and a valid API key its own bucket.
    write = client.post("/api/v1/results/refresh", json={"competition": "ekstraklasa"})
    assert write.headers["ratelimit-policy"] == '"write";q=10;w=60'
    keyed = client.get("/api/v1/competitions", headers=AUTH_HEADERS)
    assert keyed.status_code == 200
    invalid_key = client.get("/api/v1/competitions", headers={"X-API-Key": "made-up"})
    assert invalid_key.status_code == 429

    monkeypatch.setattr(main_module.settings, "rate_limit_enabled", False)
    unlimited = client.get("/api/v1/competitions")
    assert unlimited.status_code == 200 and "ratelimit" not in unlimited.headers


def test_forwarded_for_is_used_only_behind_trusted_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = client_with_matches(tmp_path)
    monkeypatch.setattr(main_module.settings, "rate_limit_read_per_minute", 1)
    spoofed = {"X-Forwarded-For": "203.0.113.7"}
    assert client.get("/api/v1/competitions", headers=spoofed).status_code == 200
    assert client.get("/api/v1/competitions", headers=spoofed).status_code == 429

    trusted = TestClient(app, client=("10.0.0.2", 50000))
    monkeypatch.setattr(main_module.settings, "trusted_proxies", "10.0.0.0/8")
    for address in ["203.0.113.7", "203.0.113.8"]:
        response = trusted.get("/api/v1/competitions", headers={"X-Forwarded-For": address})
        assert response.status_code == 200, address


def test_read_endpoints_return_etag_and_304(tmp_path: Path) -> None:
    client, _ = client_with_matches(tmp_path)
    for url in [
        "/api/v1/competitions",
        "/api/v1/matches?limit=2",
        "/api/v1/standings?competition=ekstraklasa",
        "/api/v1/teams",
        "/api/v1/results",
    ]:
        response = client.get(url)
        etag = response.headers["etag"]
        assert re.fullmatch(r'W/"[0-9a-f]{32}"', etag), url
        assert response.headers["cache-control"] == "public, max-age=60", url
        assert client.get(url).headers["etag"] == etag

        not_modified = client.get(url, headers={"If-None-Match": f'"other", {etag[2:]}'})
        assert not_modified.status_code == 304, url
        assert not_modified.content == b""
        assert not_modified.headers["etag"] == etag
        assert not_modified.headers["cache-control"] == "public, max-age=60"
        assert "ratelimit" in not_modified.headers
        assert client.get(url, headers={"If-None-Match": 'W/"other"'}).status_code == 200

    compact_etag = client.get("/api/v1/matches?limit=2&fields=id").headers["etag"]
    assert compact_etag != client.get("/api/v1/matches?limit=2").headers["etag"]


def test_cache_control_follows_freshness_and_never_caches_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, repository = client_with_matches(tmp_path)

    def freshness(next_update_at: datetime | None) -> Freshness:
        return Freshness(
            competition="ekstraklasa",
            season="2026-2027",
            data_as_of=datetime.now(UTC),
            next_update_at=next_update_at,
            automatic_refresh=next_update_at is not None,
            scheduler_state="idle",
            source=FreshnessSource(provider="90minut", url="http://www.90minut.pl/liga/1/l.html"),
        )

    url = "/api/v1/matches?competition=ekstraklasa"
    repository.freshness = [freshness(datetime.now(UTC) + timedelta(seconds=30))]
    max_age = int(client.get(url).headers["cache-control"].removeprefix("public, max-age="))
    assert 25 <= max_age <= 30
    repository.freshness = [freshness(datetime.now(UTC) - timedelta(minutes=5))]
    assert client.get(url).headers["cache-control"] == "public, no-cache"
    repository.freshness = [freshness(None)]
    standings = client.get("/api/v1/standings?competition=ekstraklasa")
    assert standings.headers["cache-control"] == "public, max-age=60"
    monkeypatch.setattr(main_module.settings, "cache_max_age_seconds", 0)
    assert client.get("/api/v1/teams").headers["cache-control"] == "public, no-cache"

    queued = client.post(
        "/api/v1/results/refresh", json={"competition": "ekstraklasa"}, headers=AUTH_HEADERS
    )
    assert queued.status_code == 202
    for response in [
        queued,
        client.get(queued.json()["status_url"], headers=AUTH_HEADERS),
        client.get("/health"),
    ]:
        assert response.headers["cache-control"] == "no-store"
        assert "etag" not in response.headers


AGENT_PAGES = {
    "/llms.txt": "text/markdown; charset=utf-8",
    "/llms-full.txt": "text/markdown; charset=utf-8",
    "/agents.md": "text/markdown; charset=utf-8",
    "/agents": "text/html; charset=utf-8",
    "/problems": "text/markdown; charset=utf-8",
    "/problems/unknown-season": "text/markdown; charset=utf-8",
}


def test_agent_pages_are_typed_linked_cached_and_rate_limited(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)

    for path, content_type in AGENT_PAGES.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"] == content_type
        assert response.headers["cache-control"] == "public, max-age=60"
        assert response.headers["vary"] == "Host, X-Forwarded-Host, X-Forwarded-Proto"
        assert response.headers["ratelimit-policy"] == '"read";q=120;w=60'
        assert '<http://testserver/llms.txt>; rel="describedby"' in response.headers["link"]
        revalidated = client.get(path, headers={"If-None-Match": response.headers["etag"]})
        assert revalidated.status_code == 304
    assert (
        client.get("/agents")
        .headers["link"]
        .startswith('<http://testserver/agents.md>; rel="alternate"; type="text/markdown"')
    )
    assert not set(AGENT_PAGES) & set(app.openapi()["paths"])


def test_agent_pages_use_the_configured_public_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.settings, "public_base_url", "https://football.example")
    client, _ = client_with_empty_store(tmp_path)

    response = client.get("/llms.txt", headers={"Host": "attacker.example"})

    assert "vary" not in response.headers
    assert "attacker.example" not in response.text
    assert 'curl -s "https://football.example/api/v1/competitions"' in response.text
    assert "- [OpenAPI spec](https://football.example/openapi.json)" in response.text


def test_agent_pages_list_live_competitions_and_survive_a_database_outage(
    tmp_path: Path,
) -> None:
    client, repository = client_with_empty_store(tmp_path)
    repository.competitions["ekstraklasa"].current_season = "2027-2028"
    assert "current season 2027-2028" in client.get("/llms.txt").text

    async def unavailable():
        raise OSError("database down")

    repository.list_competitions = unavailable
    response = client.get("/agents.md")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "**The database is unavailable**" in response.text
    assert "| `ekstraklasa` | PKO BP Ekstraklasa |" in response.text


def test_problem_type_uris_resolve_to_pages(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    problem = Problem.model_validate(client.get("/api/v1/matches?competition=x").json())

    page = client.get(problem.type)
    assert page.status_code == 200
    assert page.text.startswith("# Unknown competition (unknown-competition)")
    body = assert_problem(client.get("/problems/nope"), 404, "not-found")
    assert body["valid_values"] == list(PROBLEM_TYPES)


# --- Admin and metrics ---------------------------------------------------------------------


def scrape_run(job_id: int, status: str, competition: str = "ekstraklasa") -> ScrapeRun:
    queued = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(hours=job_id)
    return ScrapeRun(
        job_id=job_id,
        competition=competition,
        season="2026-2027",
        status=status,
        queued_at=queued,
        started_at=queued,
        completed_at=queued if status in ("completed", "failed") else None,
        matches_found=9 if status == "completed" else None,
        error_message="HTTP 503" if status == "failed" else None,
        source_url="http://www.90minut.pl/liga/1/liga14000.html",
        provider="90minut",
        fallback=False,
        worker_id="host:1:abc",
    )


def season_state(
    competition: str = "ekstraklasa",
    fetched_at: datetime | None = None,
    next_check_at: datetime | None = None,
    failures: int = 0,
) -> SeasonState:
    return SeasonState(
        competition=competition,
        season="2026-2027",
        scheduler_state="retrying" if failures else "waiting_for_schedule",
        fetched_at=fetched_at,
        next_check_at=next_check_at,
        last_checked_at=fetched_at,
        consecutive_failures=failures,
        active_job_id=None,
    )


@pytest.mark.parametrize("path", ["/api/v1/admin/scrape-runs", "/api/v1/admin/scheduler"])
def test_admin_operations_need_an_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    client, _ = client_with_empty_store(tmp_path)
    assert client.get(path).json()["type"].endswith("/problems/unauthorized")
    response = client.get(path, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    monkeypatch.setattr(main_module.settings, "api_keys", "")
    assert client.get(path, headers=AUTH_HEADERS).status_code == 503


def test_scrape_runs_are_newest_first_filtered_and_paginated(tmp_path: Path) -> None:
    client, repository = client_with_empty_store(tmp_path)
    repository.scrape_runs = [
        scrape_run(1, "completed"),
        scrape_run(2, "failed"),
        scrape_run(3, "failed", "premier-league"),
        scrape_run(4, "failed"),
    ]
    repository.scrape_runs[0].matches_skipped = 2
    url = "/api/v1/admin/scrape-runs"

    first = client.get(url, params={"status": "failed", "limit": 2}, headers=AUTH_HEADERS).json()
    assert [item["job_id"] for item in first["data"]] == [4, 3]
    assert first["data"][0]["error_message"] == "HTTP 503"
    cursor = first["pagination"]["next_cursor"]
    second = client.get(
        url, params={"status": "failed", "limit": 2, "cursor": cursor}, headers=AUTH_HEADERS
    ).json()
    assert [item["job_id"] for item in second["data"]] == [2]
    assert second["pagination"]["next_cursor"] is None

    assert client.get(url, params={"cursor": cursor}, headers=AUTH_HEADERS).status_code == 400
    only_league = client.get(url, params={"competition": "ekstraklasa"}, headers=AUTH_HEADERS)
    assert [item["job_id"] for item in only_league.json()["data"]] == [4, 2, 1]
    assert [item["matches_skipped"] for item in only_league.json()["data"]] == [0, 0, 2]
    unknown = client.get(url, params={"status": "done"}, headers=AUTH_HEADERS)
    assert unknown.status_code == 422
    assert unknown.json()["valid_values"] == [
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
    ]


def test_scheduler_status_reports_stalled_workers_and_stale_seasons(tmp_path: Path) -> None:
    client, repository = client_with_empty_store(tmp_path)
    now = datetime.now(UTC)
    repository.heartbeat_at = now - timedelta(hours=1)
    repository.workers = [
        WorkerHeartbeat("host:1:abc", now - timedelta(days=1), now - timedelta(hours=1))
    ]
    repository.season_states = [
        season_state("ekstraklasa", now - timedelta(hours=2), now - timedelta(minutes=10)),
        season_state("premier-league", now - timedelta(days=1), now - timedelta(minutes=20), 3),
        season_state("europa-league"),
    ]

    body = client.get("/api/v1/admin/scheduler", headers=AUTH_HEADERS).json()

    assert body["status"] == "stalled"
    assert body["stale_grace_seconds"] == 900
    assert [worker["alive"] for worker in body["workers"]] == [False]
    seasons = {item["competition"]: item for item in body["seasons"]}
    assert not seasons["ekstraklasa"]["stale"]
    assert 590 < seasons["ekstraklasa"]["overdue_seconds"] < 700
    assert seasons["premier-league"]["stale"]
    assert seasons["premier-league"]["consecutive_failures"] == 3
    assert seasons["europa-league"]["stale"] and seasons["europa-league"]["overdue_seconds"] == 0
    assert client.get("/health").json()["scheduler"] == "stale"


def metric(body: str, name: str, **labels: str) -> float:
    selector = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
    series = f"{name}{{{selector}}}" if labels else name
    for line in body.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{series} not in metrics")


def test_metrics_need_a_bearer_or_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = client_with_empty_store(tmp_path)

    missing = client.get("/metrics")
    assert missing.status_code == 401 and missing.headers["www-authenticate"] == "Bearer"
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    bearer = client.get("/metrics", headers={"Authorization": "Bearer test-api-key"})
    assert bearer.status_code == 200
    assert bearer.headers["content-type"].startswith("text/plain")
    assert bearer.headers["cache-control"] == "no-store"
    assert client.get("/metrics", headers=AUTH_HEADERS).status_code == 200
    assert "/metrics" not in app.openapi()["paths"]
    monkeypatch.setattr(main_module.settings, "api_keys", "")
    assert client.get("/metrics", headers=AUTH_HEADERS).status_code == 503


def test_metrics_report_scheduler_seasons_jobs_and_requests(tmp_path: Path) -> None:
    client, repository = client_with_empty_store(tmp_path)
    now = datetime.now(UTC)
    repository.heartbeat_at = now - timedelta(seconds=5)
    repository.season_states = [
        season_state("ekstraklasa", now - timedelta(hours=2), now + timedelta(hours=1)),
        season_state("premier-league", now - timedelta(days=1), now - timedelta(hours=1), 4),
        season_state("europa-league"),
    ]
    repository.run_counts = [ScrapeRunCount("ekstraklasa", "failed", 2)]
    client.get("/api/v1/teams")
    client.get("/api/v1/matches/does-not-exist")

    body = client.get("/metrics", headers=AUTH_HEADERS).text

    assert metric(body, "football_results_scheduler_stalled") == 0
    assert metric(body, "football_results_scheduler_heartbeat_age_seconds") < 60
    league = {"competition": "ekstraklasa", "season": "2026-2027"}
    failing = {"competition": "premier-league", "season": "2026-2027"}
    never = {"competition": "europa-league", "season": "2026-2027"}
    assert metric(body, "football_results_season_stale", **league) == 0
    assert metric(body, "football_results_season_overdue_seconds", **league) == 0
    assert metric(body, "football_results_season_stale", **failing) == 1
    assert metric(body, "football_results_season_consecutive_failures", **failing) == 4
    assert metric(body, "football_results_season_stale", **never) == 1
    assert 'football_results_season_data_age_seconds{competition="europa-league"' not in body
    runs = {"competition": "ekstraklasa", "status": "failed"}
    assert metric(body, "football_results_scrape_runs", **runs) == 2
    teams = {"operation": "list_teams", "method": "GET", "status": "200"}
    assert metric(body, "football_results_http_requests_total", **teams) >= 1
    assert "/api/v1/matches/does-not-exist" not in body


CLIENT_HEADERS = {"X-API-Key": "client-key"}
ADMIN_FILE_HEADERS = {"X-API-Key": "admin-file-key"}


def use_file_keys(monkeypatch: pytest.MonkeyPatch, *entries: access.FileKey) -> None:
    monkeypatch.setattr(main_module, "file_keys", entries)


def file_key(key: str, name: str, role: access.Role = "client", **quotas: int) -> access.FileKey:
    digest = bytes.fromhex(access.hash_key(key).removeprefix(access.HASH_PREFIX))
    return access.FileKey(
        name, digest, role, quotas.get("read_per_minute"), quotas.get("write_per_minute")
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/results/refresh"),
        ("POST", "/api/v1/results/history/fetch"),
        ("GET", "/api/v1/jobs/1"),
        ("GET", "/api/v1/admin/scrape-runs"),
        ("GET", "/api/v1/admin/scheduler"),
        ("GET", "/metrics"),
    ],
)
def test_client_keys_are_forbidden_on_admin_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    client, _ = client_with_empty_store(tmp_path)
    use_file_keys(
        monkeypatch,
        file_key("client-key", "partner-app"),
        file_key("admin-file-key", "ops", "admin"),
    )
    body = {"competition": "ekstraklasa", "season": "2024-2025"}

    forbidden = client.request(method, path, json=body, headers=CLIENT_HEADERS)
    problem = assert_problem(forbidden, 403, "forbidden")
    assert "'partner-app' has role client" in problem["detail"]
    assert forbidden.headers["cache-control"] == "no-store"
    unauthorized = client.request(method, path, json=body, headers={"X-API-Key": "wrong"})
    assert unauthorized.status_code == 401
    assert client.request(method, path, json=body, headers=ADMIN_FILE_HEADERS).status_code != 403
    assert client.request(method, path, json=body, headers=AUTH_HEADERS).status_code != 403
    if path != "/metrics":
        template = path.replace("/1", "/{job_id}")
        responses = app.openapi()["paths"][template][method.lower()]["responses"]
        assert {"401", "403", "503"} <= set(responses)


def test_writes_are_disabled_when_only_client_keys_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = client_with_empty_store(tmp_path)
    monkeypatch.setattr(main_module.settings, "api_keys", "")
    use_file_keys(monkeypatch, file_key("client-key", "partner-app"))
    refresh = client.post(
        "/api/v1/results/refresh", json={"competition": "ekstraklasa"}, headers=CLIENT_HEADERS
    )
    assert_problem(refresh, 503, "writes-disabled")
    assert client.get("/api/v1/competitions", headers=CLIENT_HEADERS).status_code == 200

    use_file_keys(monkeypatch, file_key("admin-file-key", "ops", "admin"))
    queued = client.post(
        "/api/v1/results/refresh", json={"competition": "ekstraklasa"}, headers=ADMIN_FILE_HEADERS
    )
    assert queued.status_code == 202


def test_keyed_clients_have_their_own_quotas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = client_with_matches(tmp_path)
    monkeypatch.setattr(main_module.settings, "rate_limit_read_per_minute", 2)
    use_file_keys(
        monkeypatch,
        file_key("client-key", "partner-app", read_per_minute=3),
        file_key("small-key", "small", read_per_minute=1),
        file_key("admin-file-key", "ops", "admin", write_per_minute=1),
    )

    before = client.get("/metrics", headers={"Authorization": "Bearer test-api-key"}).text
    keyed = client.get("/api/v1/competitions", headers=CLIENT_HEADERS)
    assert keyed.headers["ratelimit-policy"] == '"read";q=3;w=60'
    assert keyed.headers["ratelimit"] == '"read";r=2;t=60'
    assert client.get("/api/v1/competitions", headers=CLIENT_HEADERS).status_code == 200
    assert client.get("/api/v1/competitions", headers=CLIENT_HEADERS).status_code == 200
    limited = client.get("/api/v1/competitions", headers=CLIENT_HEADERS)
    body = assert_problem(limited, 429, "rate-limited")
    assert body["detail"] == "Rate limit exceeded: 3 read requests per 60 seconds"

    # Anonymous clients keep the default quota per IP; a key may also have a lower quota.
    assert client.get("/api/v1/competitions").headers["ratelimit-policy"] == '"read";q=2;w=60'
    assert client.get("/api/v1/competitions", headers={"X-API-Key": "small-key"}).status_code == 200
    assert client.get("/api/v1/competitions", headers={"X-API-Key": "small-key"}).status_code == 429

    refresh = {"competition": "ekstraklasa"}
    first = client.post("/api/v1/results/refresh", json=refresh, headers=ADMIN_FILE_HEADERS)
    assert first.headers["ratelimit-policy"] == '"write";q=1;w=60'
    second = client.post("/api/v1/results/refresh", json=refresh, headers=ADMIN_FILE_HEADERS)
    assert second.status_code == 429
    # Environment keys keep the default quotas.
    env = client.post("/api/v1/results/refresh", json=refresh, headers=AUTH_HEADERS)
    assert env.headers["ratelimit-policy"] == '"write";q=10;w=60'

    after = client.get("/metrics", headers=AUTH_HEADERS).text
    assert used(before, after, "keyed_requests", "partner-app", "read") == 4
    assert used(before, after, "rate_limited", "partner-app", "read") == 1
    assert used(before, after, "rate_limited", "ops", "write") == 1
    assert used(before, after, "keyed_requests", "env", "write") == 1
    assert used(before, after, "keyed_requests", "anonymous", "read") == 1


def used(before: str, after: str, name: str, key: str, policy: str) -> float:
    series = f"football_results_{name}_total"

    def value(body: str) -> float:
        try:
            return metric(body, series, key=key, policy=policy)
        except AssertionError:
            return 0.0

    return value(after) - value(before)


def test_startup_loads_the_api_keys_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client_with_empty_store(tmp_path)
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        f"keys:\n  - {{name: ops, hash: '{access.hash_key('file-admin')}', role: admin}}\n"
    )
    monkeypatch.setattr(main_module.settings, "api_keys", "")
    monkeypatch.setattr(main_module.settings, "api_keys_file", keys)
    monkeypatch.setattr(main_module, "file_keys", ())
    with TestClient(app) as client:
        response = client.get("/api/v1/admin/scheduler", headers={"X-API-Key": "file-admin"})
        assert response.status_code == 200

    keys.write_text("keys:\n  - {name: ops, hash: plain}\n")
    with pytest.raises(ValueError, match="invalid API keys file"), TestClient(app):
        pass
