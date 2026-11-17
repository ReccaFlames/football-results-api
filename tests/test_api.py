from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.competitions import competition_summary, source_from_url
from app.config import load_config
from app.main import app
from app.models import MatchResult, ScrapedMatch, ScrapeJobStatus

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

    async def health(self) -> None:
        return None

    async def latest_scheduler_heartbeat(self):
        return None

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
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_refresh_requires_competition(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post("/api/v1/results/refresh", json={}, headers=AUTH_HEADERS)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "competition"]


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
    assert client.get("/api/v1/jobs/1", headers=AUTH_HEADERS).json()["status"] == "queued"
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
    assert "90minut page or football-data.org" in response.json()["detail"]


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


def test_historical_source_rejects_another_football_data_competition(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={
            "competition": "premier-league",
            "season": "2023-2024",
            "source_url": "https://api.football-data.org/v4/competitions/BL1/matches?season=2023",
        },
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 422
    assert "code BL1 is not configured" in response.json()["detail"]
