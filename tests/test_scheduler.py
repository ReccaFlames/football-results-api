from datetime import UTC, datetime, timedelta

from app.config import load_config
from app.models import Competition, SeasonSource
from app.repository import ScheduledRefresh, SeasonCheckResult
from app.scheduler import check_seasons, describe_idle
from app.scraper import ProviderCapabilities, ScraperRegistry

CONFIG = load_config()
COMPETITIONS = CONFIG.to_competitions()
NEXT_SEASON = SeasonSource(
    season="2027-2028",
    source_url="https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2027",
    provider="espn",
)


class FakeProvider:
    def __init__(self, name: str, discovery: bool, error: Exception | None = None) -> None:
        self.name = name
        self.capabilities = ProviderCapabilities(
            statistics=frozenset(), season_discovery=discovery, requires_api_token=False
        )
        self.error = error
        self.calls: list[str] = []

    async def scrape(self, competition, source):
        raise AssertionError("season checks must not scrape")

    async def discover_seasons(self, competition: Competition) -> list[SeasonSource]:
        self.calls.append(competition.slug)
        if self.error is not None:
            raise self.error
        return [NEXT_SEASON]


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[SeasonSource], datetime]] = []

    async def apply_season_check(self, slug, discovered, next_check_at, policy):
        self.calls.append((slug, discovered, next_check_at))
        return SeasonCheckResult()


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
