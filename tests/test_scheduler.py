from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.scheduler as scheduler_module
from app.config import load_config
from app.models import Competition, ScrapedMatch, SeasonSource
from app.repository import RefreshTarget, ScheduledRefresh, SeasonCheckResult
from app.scheduler import check_seasons, describe_idle, refresh_target
from app.scheduling import SchedulePolicy
from app.scraper import ProviderCapabilities, ScraperRegistry

CONFIG = load_config()
COMPETITIONS = CONFIG.to_competitions()
NEXT_SEASON = SeasonSource(
    season="2027-2028",
    source_url="https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2027",
    provider="espn",
)


NEXT_FALLBACK = SeasonSource(
    season="2027-2028",
    source_url="https://api.football-data.org/v4/competitions/PL/matches?season=2027",
    provider="football-data",
)


class FakeProvider:
    def __init__(
        self,
        name: str,
        discovery: bool,
        error: Exception | None = None,
        discovered: SeasonSource = NEXT_SEASON,
        windowed: bool = False,
    ) -> None:
        self.name = name
        self.capabilities = ProviderCapabilities(
            statistics=frozenset(),
            season_discovery=discovery,
            requires_api_token=False,
            windowed=windowed,
        )
        self.error = error
        self.discovered = discovered
        self.calls: list[str] = []
        self.scraped: list[SeasonSource] = []
        self.available = True

    async def scrape(self, competition, source):
        self.scraped.append(source)
        if self.error is not None:
            raise self.error
        return [MATCH]

    async def discover_seasons(self, competition: Competition) -> list[SeasonSource]:
        self.calls.append(competition.slug)
        if self.error is not None:
            raise self.error
        return [self.discovered]


class FakeRepository:
    def __init__(self, primary_failures: int = 0) -> None:
        self.calls: list[tuple[str, list[SeasonSource], datetime]] = []
        self.fallbacks: list[list[SeasonSource]] = []
        self.primary_failures = primary_failures
        self.stored: list[SeasonSource | None] = []
        self.windowed: list[bool] = []
        self.failed: list[SeasonSource | None] = []

    async def apply_season_check(self, slug, discovered, next_check_at, policy, fallbacks):
        self.calls.append((slug, discovered, next_check_at))
        self.fallbacks.append(fallbacks)
        return SeasonCheckResult()

    async def replace_competition(
        self, competition, source, matches, run_id=None, policy=None, fallback=None, windowed=False
    ):
        self.stored.append(fallback)
        self.windowed.append(windowed)

    async def record_scrape_failure(
        self, competition, source, error, run_id=None, policy=None, fallback=None
    ):
        self.failed.append(fallback)
        if fallback is None:
            self.primary_failures += 1
        return self.primary_failures


def registry(*providers: FakeProvider) -> ScraperRegistry:
    return ScraperRegistry({provider.name: provider for provider in providers})


async def test_season_check_passes_discovered_seasons_to_the_repository() -> None:
    provider = FakeProvider("espn", discovery=True)
    repository = FakeRepository()
    before = datetime.now(UTC)

    await check_seasons(repository, registry(provider), CONFIG, COMPETITIONS["premier-league"])

    [(slug, discovered, next_check_at)] = repository.calls
    assert provider.calls == ["premier-league"]
    assert (slug, discovered) == ("premier-league", [NEXT_SEASON])
    assert next_check_at >= before + timedelta(days=1)


async def test_season_check_without_discovery_still_evaluates_rollover() -> None:
    provider = FakeProvider("90minut", discovery=False)
    repository = FakeRepository()

    await check_seasons(repository, registry(provider), CONFIG, COMPETITIONS["ekstraklasa"])

    assert provider.calls == []
    assert [(slug, discovered) for slug, discovered, _ in repository.calls] == [("ekstraklasa", [])]


async def test_failed_discovery_retries_after_the_longest_failure_backoff() -> None:
    provider = FakeProvider("espn", discovery=True, error=RuntimeError("HTTP 429"))
    repository = FakeRepository()
    before = datetime.now(UTC)

    await check_seasons(repository, registry(provider), CONFIG, COMPETITIONS["premier-league"])

    [(_, discovered, next_check_at)] = repository.calls
    assert discovered == []
    assert before + timedelta(minutes=60) <= next_check_at < before + timedelta(minutes=61)


NOW = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)


def test_idle_message_names_the_next_refresh_or_what_blocks_an_overdue_one() -> None:
    upcoming = ScheduledRefresh("ekstraklasa", "2026-2027", NOW + timedelta(hours=1), None)
    blocked = ScheduledRefresh("ekstraklasa", "2026-2027", NOW - timedelta(days=3), 7)

    assert describe_idle(upcoming, NOW) == (
        "idle; next refresh ekstraklasa 2026-2027 at 2026-11-18T21:00:00+00:00"
    )
    assert describe_idle(blocked, NOW) == (
        "idle; ekstraklasa 2026-2027 was due at 2026-11-15T20:00:00+00:00 "
        "and waits for scrape job 7"
    )
    assert "no current season" in describe_idle(None, NOW)


PREMIER_LEAGUE = COMPETITIONS["premier-league"]
SOURCE = PREMIER_LEAGUE.seasons["2026-2027"]
FALLBACK = SOURCE.fallback()
MATCH = ScrapedMatch(
    competition="premier-league",
    season="2026-2027",
    stage="Premier League",
    home_team="Arsenal FC",
    away_team="Chelsea FC",
    status="scheduled",
    played_at=NOW,
    source_url=SOURCE.source_url,
)


def target(scheduled: bool = True) -> RefreshTarget:
    return RefreshTarget(PREMIER_LEAGUE, SOURCE, run_id=1, scheduled=scheduled)


async def test_fallback_is_used_once_the_primary_failed_often_enough() -> None:
    espn = FakeProvider("espn", discovery=False, error=RuntimeError("HTTP 503"))
    football_data = FakeProvider("football-data", discovery=False)
    repository = FakeRepository(primary_failures=1)
    policy = SchedulePolicy(fallback_after_failures=3)
    scraper = registry(espn, football_data)

    await refresh_target(repository, scraper, target(), policy, "football-data")
    assert (football_data.scraped, repository.failed) == ([], [None])

    await refresh_target(repository, scraper, target(), policy, "football-data")
    assert football_data.scraped == [FALLBACK]
    assert repository.stored == [FALLBACK]
    assert repository.primary_failures == 3


@pytest.mark.parametrize("windowed", [True, False])
async def test_fallback_refresh_tells_the_repository_whether_the_source_is_windowed(
    windowed: bool,
) -> None:
    espn = FakeProvider("espn", discovery=False, error=RuntimeError("HTTP 503"))
    football_data = FakeProvider("football-data", discovery=False, windowed=windowed)
    repository = FakeRepository(primary_failures=10)

    await refresh_target(
        repository, registry(espn, football_data), target(), SchedulePolicy(), "football-data"
    )

    assert (repository.stored, repository.windowed) == ([FALLBACK], [windowed])


async def test_primary_success_does_not_try_the_fallback() -> None:
    espn = FakeProvider("espn", discovery=False)
    football_data = FakeProvider("football-data", discovery=False)
    repository = FakeRepository(primary_failures=5)

    await refresh_target(
        repository, registry(espn, football_data), target(), SchedulePolicy(), "football-data"
    )

    assert (espn.scraped, football_data.scraped, repository.stored) == ([SOURCE], [], [None])


@pytest.mark.parametrize(
    ("scheduled", "configured", "available"),
    [(False, "football-data", True), (True, None, True), (True, "football-data", False)],
    ids=["queued-job", "fallback-removed-from-config", "no-api-token"],
)
async def test_fallback_is_skipped(
    scheduled: bool, configured: str | None, available: bool
) -> None:
    espn = FakeProvider("espn", discovery=False, error=RuntimeError("HTTP 503"))
    football_data = FakeProvider("football-data", discovery=False)
    football_data.available = available
    repository = FakeRepository(primary_failures=10)

    await refresh_target(
        repository, registry(espn, football_data), target(scheduled), SchedulePolicy(), configured
    )

    assert (football_data.scraped, repository.stored, repository.failed) == ([], [], [None])


async def test_failed_fallback_is_recorded_as_a_fallback_run() -> None:
    espn = FakeProvider("espn", discovery=False, error=RuntimeError("HTTP 503"))
    football_data = FakeProvider("football-data", discovery=False, error=RuntimeError("HTTP 429"))
    repository = FakeRepository(primary_failures=10)

    await refresh_target(
        repository, registry(espn, football_data), target(), SchedulePolicy(), "football-data"
    )

    assert repository.failed == [None, FALLBACK]
    assert repository.primary_failures == 11


async def test_season_check_discovers_fallback_sources() -> None:
    espn = FakeProvider("espn", discovery=True)
    football_data = FakeProvider("football-data", discovery=True, discovered=NEXT_FALLBACK)
    repository = FakeRepository()

    await check_seasons(repository, registry(espn, football_data), CONFIG, PREMIER_LEAGUE)

    assert repository.fallbacks == [[NEXT_FALLBACK]]
    assert football_data.calls == ["premier-league"]


def test_scheduler_enforces_demo_mode_before_seeding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module.settings, "demo_mode", True)
    monkeypatch.setattr(scheduler_module.settings, "operator_contact", "demo@example.com")
    monkeypatch.setattr(scheduler_module.settings, "api_keys", "")
    monkeypatch.setattr(scheduler_module.settings, "api_keys_file", None)
    root = Path(__file__).parents[1]

    scheduler_module.check_demo(load_config(root / "config/demo-competitions.yaml"))
    with pytest.raises(ValueError, match="'premier-league' uses provider 'espn'"):
        scheduler_module.check_demo(load_config(root / "config/competitions.yaml"))
    monkeypatch.setattr(scheduler_module.settings, "api_keys", "env-admin-key")
    with pytest.raises(ValueError, match="FOOTBALL_RESULTS_API_KEYS is set"):
        scheduler_module.check_demo(load_config(root / "config/demo-competitions.yaml"))
