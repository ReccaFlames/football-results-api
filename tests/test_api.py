from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.competitions import COMPETITIONS, competition_summary
from app.main import app
from app.models import MatchResult, ScrapedMatch


class FakeRepository:
    def __init__(self) -> None:
        self.competitions = {
            slug: competition.model_copy(deep=True) for slug, competition in COMPETITIONS.items()
        }
        self.results: dict[tuple[str, str], list[MatchResult]] = {}
        self.fetched: dict[str, set[str]] = {}
        self.refreshed_at: datetime | None = None

    async def health(self) -> None:
        return None

    async def latest_scheduler_heartbeat(self):
        return None

    async def seed_competitions(self, _seeds) -> None:
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

    async def replace_competition(self, competition, source, fresh):
        competition.seasons[source.season] = source
        self.results[(competition.slug, source.season)] = [
            result for item in fresh if (result := item.as_result()) is not None
        ]
        self.fetched.setdefault(competition.slug, set()).add(source.season)
        self.refreshed_at = datetime.now(UTC)
        return self.refreshed_at


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


def test_round_range_is_rejected_for_cup(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/refresh",
        json={
            "competition": "europa-league",
            "first_round": 1,
            "last_round": 2,
        },
    )
    assert response.status_code == 422
    assert "only for league competitions" in response.json()["detail"]


def test_refresh_requires_competition(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post("/api/v1/results/refresh", json={})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "competition"]


def test_refresh_rejects_historical_season(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/refresh",
        json={"competition": "ekstraklasa", "season": "2025-2026"},
    )
    assert response.status_code == 422
    assert "history/fetch" in response.json()["detail"]


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


def test_historical_fetch_downloads_only_once(tmp_path: Path) -> None:
    class FakeScraper:
        calls = 0

        async def scrape(self, competition, source):
            self.calls += 1
            return [match_for(competition, source)]

    fake = FakeScraper()
    main_module.scraper = fake
    client, _ = client_with_empty_store(tmp_path)
    payload = {"competition": "ekstraklasa", "season": "2025-2026"}

    first = client.post("/api/v1/results/history/fetch", json=payload)
    second = client.post("/api/v1/results/history/fetch", json=payload)

    assert first.status_code == 200
    assert first.json()["fetched"] is True
    assert first.json()["results"][0]["home_goals"] == 2
    assert first.json()["results"][0]["home_corners"] is None
    assert second.status_code == 200
    assert second.json()["fetched"] is False
    assert fake.calls == 1

    forced = client.post(
        "/api/v1/results/history/fetch",
        json={**payload, "force_refresh": True},
    )
    assert forced.status_code == 200
    assert forced.json()["fetched"] is True
    assert fake.calls == 2


def test_unknown_historical_season_is_persisted_and_listed(tmp_path: Path) -> None:
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
    )
    assert response.status_code == 200
    assert response.json()["fetched"] is True

    competitions = client.get("/api/v1/competitions").json()
    ekstraklasa = next(item for item in competitions if item["slug"] == "ekstraklasa")
    assert ekstraklasa["available_seasons"] == [
        "2026-2027",
        "2025-2026",
        "2024-2025",
    ]
    assert ekstraklasa["fetched_seasons"] == ["2024-2025"]


def test_unknown_historical_season_requires_source(tmp_path: Path) -> None:
    client, _ = client_with_empty_store(tmp_path)
    response = client.post(
        "/api/v1/results/history/fetch",
        json={"competition": "ekstraklasa", "season": "2024-2025"},
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
    )
    assert response.status_code == 422
    assert "not configured for ekstraklasa" in response.json()["detail"]
