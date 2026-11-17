from datetime import UTC, datetime, timedelta

from app.config import load_config
from app.models import Competition, SeasonSource
from app.repository import SeasonCheckResult
from app.scheduler import check_seasons
from app.scraper import ProviderCapabilities, ScraperRegistry

CONFIG = load_config()
COMPETITIONS = CONFIG.to_competitions()
NEXT_SEASON = SeasonSource(
    season="2027-2028",
    source_url="https://api.football-data.org/v4/competitions/PL/matches?season=2027",
    provider="football-data",
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
    provider = FakeProvider("football-data", discovery=True)
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
    provider = FakeProvider("football-data", discovery=True, error=RuntimeError("HTTP 429"))
    repository = FakeRepository()
    before = datetime.now(UTC)

    await check_seasons(repository, registry(provider), CONFIG, COMPETITIONS["premier-league"])

    [(_, discovered, next_check_at)] = repository.calls
    assert discovered == []
    assert before + timedelta(minutes=60) <= next_check_at < before + timedelta(minutes=61)
