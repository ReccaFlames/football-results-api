import logging
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from app.competitions import source_from_url
from app.config import Settings, load_config
from app.models import ScrapedMatch
from app.scraper import (
    MATCH_STATISTICS,
    TheSportsDbScraper,
    create_scraper_registry,
    parse_thesportsdb_events,
    parse_thesportsdb_seasons,
)

DEFAULT_CONFIG = Path(__file__).parents[1] / "config/competitions.yaml"
EKSTRAKLASA = load_config(DEFAULT_CONFIG).to_competitions()["ekstraklasa"]
SOURCE_URL = "https://www.thesportsdb.com/league/4422?season=2026-2027"
SOURCE = source_from_url("2026-2027", SOURCE_URL)
NOW = datetime(2026, 11, 21, 23, 30, tzinfo=UTC)


def event(
    event_id: str = "2490306",
    status: str | None = "FT",
    score: tuple[str | None, str | None] = ("1", "0"),
    season: str = "2026-2027",
    timestamp: str = "2026-11-08T13:45:00",
    postponed: str = "no",
    league: str = "4422",
    round_number: str = "14",
) -> dict[str, object]:
    """An ``eventsday.php`` event, trimmed to the fields that matter, as TheSportsDB sends it."""
    return {
        "idEvent": event_id,
        "idAPIfootball": "1553219",
        "strTimestamp": timestamp,
        "strEvent": "Jagiellonia Białystok vs GKS Katowice",
        "strSport": "Soccer",
        "idLeague": league,
        "strLeague": "Polish Ekstraklasa",
        "strSeason": season,
        "strHomeTeam": "Jagiellonia Białystok",
        "strAwayTeam": "GKS Katowice",
        "intHomeScore": score[0],
        "intHomeScoreExtra": None,
        "intAwayScoreExtra": None,
        "intRound": round_number,
        "intAwayScore": score[1],
        "intSpectators": None,
        "dateEvent": timestamp[:10],
        "dateEventLocal": timestamp[:10],
        "strTime": timestamp[11:],
        "strTimeLocal": "",
        "strGroup": "",
        "idHomeTeam": "135297",
        "idAwayTeam": "142467",
        "strResult": "",
        "strVenue": "Chorten Arena",
        "strCountry": "Poland",
        "strStatus": status,
        "strPostponed": postponed,
        "strLocked": "unlocked",
    }


def parse(*events: dict[str, object]) -> list[ScrapedMatch]:
    return parse_thesportsdb_events([{"events": list(events)}], EKSTRAKLASA, SOURCE)


def test_finished_event_maps_to_a_finished_match() -> None:
    [match] = parse(event())

    assert match.status == "finished"
    assert (match.home_team, match.away_team) == ("Jagiellonia Białystok", "GKS Katowice")
    assert (match.home_score, match.away_score) == (1, 0)
    assert (match.home_goals, match.away_goals) == (1, 0)
    assert match.played_at == datetime(2026, 11, 8, 13, 45, tzinfo=UTC)
    assert match.round == 14
    assert match.season == "2026-2027"
    assert match.provider_match_id == "2490306"
    assert match.source_url == "https://www.thesportsdb.com/event/2490306"
    assert match.home_cards is None and match.first_goal_minute is None


@pytest.mark.parametrize(
    ("status", "score", "postponed", "expected"),
    [
        ("FT", ("2", "4"), "no", "finished"),
        ("AET", ("1", "1"), "no", "finished"),
        ("PEN", ("0", "0"), "no", "finished"),
        ("FT", (None, None), "no", "scheduled"),
        ("NS", (None, None), "no", "scheduled"),
        ("TBD", (None, None), "no", "scheduled"),
        ("1H", ("0", "0"), "no", "scheduled"),
        ("HT", ("1", "0"), "no", "scheduled"),
        ("2H", ("1", "1"), "no", "scheduled"),
        ("PST", (None, None), "no", "inactive"),
        ("NS", (None, None), "yes", "inactive"),
        ("CANC", (None, None), "no", "inactive"),
        ("ABD", ("1", "0"), "no", "inactive"),
        ("SUSP", ("0", "0"), "no", "inactive"),
    ],
)
def test_status_mapping(
    status: str, score: tuple[str | None, str | None], postponed: str, expected: str
) -> None:
    [match] = parse(event(status=status, score=score, postponed=postponed))

    assert match.status == expected
    if expected != "finished":
        assert match.home_score is None and match.home_goals is None


@pytest.mark.parametrize("status", ["Match Finished", "XYZ", "", None])
def test_unknown_status_is_logged_and_inactive(
    status: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.scraper"):
        [match] = parse(event(status=status))

    assert match.status == "inactive"
    assert match.home_score is None
    assert "unknown TheSportsDB status" in caplog.text


def test_null_events_is_an_empty_day() -> None:
    payloads = [{"events": None}, {"events": [event()]}, {"events": None}]

    assert len(parse_thesportsdb_events(payloads, EKSTRAKLASA, SOURCE)) == 1
    assert parse_thesportsdb_events([{"events": None}], EKSTRAKLASA, SOURCE) == []


@pytest.mark.parametrize("payload", [{}, {"events": "none"}, {"error": "x"}])
def test_malformed_payload_raises(payload: dict[str, object]) -> None:
    with pytest.raises(TypeError):
        parse_thesportsdb_events([payload], EKSTRAKLASA, SOURCE)


def test_other_seasons_and_leagues_are_dropped_and_events_deduplicated() -> None:
    matches = parse_thesportsdb_events(
        [
            {"events": [event("1"), event("2", season="2025-2026")]},
            {"events": [event("1", status="NS", score=(None, None)), event("3", league="4661")]},
            {
                "events": [
                    event("4", timestamp="2026-11-22T11:15:00", status="NS", score=(None, None))
                ]
            },
        ],
        EKSTRAKLASA,
        SOURCE,
    )

    assert [match.provider_match_id for match in matches] == ["1", "4"]


def test_incomplete_events_are_skipped() -> None:
    missing_team = {**event("2"), "strHomeTeam": ""}
    missing_kickoff = {**event("3"), "strTimestamp": None, "dateEvent": None}

    assert [
        match.provider_match_id for match in parse(event("1"), missing_team, missing_kickoff)
    ] == ["1"]


def test_special_rounds_become_stages() -> None:
    [final] = parse(event(round_number="200"))
    [league_round] = parse(event(round_number="0"))

    assert (final.stage, final.round) == ("Final", None)
    assert (league_round.stage, league_round.round) == (EKSTRAKLASA.name, None)


def test_parsed_statistics_stay_within_the_declared_capabilities() -> None:
    capabilities = TheSportsDbScraper.capabilities
    matches = parse(event("1"), event("2", status="NS", score=(None, None)))

    assert capabilities.windowed and capabilities.season_discovery
    assert not capabilities.requires_api_token
    for match in matches:
        filled = {name for name in MATCH_STATISTICS if getattr(match, name) is not None}
        assert filled <= capabilities.statistics


def test_window_dates_come_from_the_injected_clock() -> None:
    scraper = TheSportsDbScraper(
        1, "test", window_days_back=2, window_days_ahead=1, clock=lambda: NOW
    )

    assert scraper.window() == [date(2026, 11, d) for d in (19, 20, 21, 22)]
    # TheSportsDB timestamps are UTC, so the window is centred on the UTC date.
    warsaw = datetime(2026, 11, 22, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    scraper = TheSportsDbScraper(
        1, "test", window_days_back=0, window_days_ahead=0, clock=lambda: warsaw
    )
    assert scraper.window() == [date(2026, 11, 21)]


def test_default_window_is_seven_days_back_and_fourteen_ahead() -> None:
    window = TheSportsDbScraper(1, "test", clock=lambda: NOW).window()

    assert (window[0], window[-1], len(window)) == (date(2026, 11, 14), date(2026, 12, 5), 22)


@pytest.mark.parametrize(("back", "ahead"), [(-1, 14), (7, -1)])
def test_negative_window_is_rejected(back: int, ahead: int) -> None:
    with pytest.raises(ValueError, match="negative"):
        TheSportsDbScraper(1, "test", window_days_back=back, window_days_ahead=ahead)


def mock_scraper(
    handler, api_key: str = "123", back: int = 1, ahead: int = 1
) -> TheSportsDbScraper:
    return TheSportsDbScraper(
        1,
        "test",
        api_key,
        0,
        back,
        ahead,
        clock=lambda: NOW,
        transport=httpx.MockTransport(handler),
    )


async def test_scrape_requests_eventsday_once_per_day_with_the_key() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.params["d"] == "2026-11-22":
            return httpx.Response(200, json={"events": [event(status="NS", score=(None, None))]})
        return httpx.Response(200, json={"events": None})

    matches = await mock_scraper(handler, api_key="98765").scrape(EKSTRAKLASA, SOURCE)

    base = "https://www.thesportsdb.com/api/v1/json/98765/eventsday.php"
    assert requested == [f"{base}?d=2026-11-{day}&l=4422" for day in (20, 21, 22)]
    assert [match.status for match in matches] == ["scheduled"]
    assert "98765" not in matches[0].source_url


async def test_http_errors_and_logs_never_contain_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scraper = mock_scraper(lambda request: httpx.Response(429), api_key="98765")

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="HTTP 429") as error:
        await scraper.scrape(EKSTRAKLASA, SOURCE)

    assert "98765" not in str(error.value) and error.value.__cause__ is None
    assert "thesportsdb.com/api/v1/json/***/eventsday.php" in caplog.text
    assert "98765" not in caplog.text


def test_api_key_defaults_to_the_free_key_and_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOOTBALL_RESULTS_THESPORTSDB_API_KEY", raising=False)
    assert Settings(_env_file=None).thesportsdb_api_key.get_secret_value() == "123"

    monkeypatch.setenv("FOOTBALL_RESULTS_THESPORTSDB_API_KEY", "98765")
    settings = Settings(_env_file=None)
    assert settings.thesportsdb_api_key.get_secret_value() == "98765"
    assert "98765" not in repr(settings)

    registry = create_scraper_registry(1, "test", None, 0, thesportsdb_api_key="98765")
    assert registry.available("thesportsdb")
    assert registry.provider("thesportsdb")._url("x.php").endswith("/json/98765/x.php")


def test_key_free_source_urls_are_accepted() -> None:
    assert SOURCE.provider == "thesportsdb"
    assert (
        source_from_url("2026", "https://www.thesportsdb.com/league/4422?season=2026").season
        == "2026"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.thesportsdb.com/league/4422?season=2026-2027",
        "https://thesportsdb.com/league/4422?season=2026-2027",
        "https://www.thesportsdb.com/league/4422?season=2025-2026",
        "https://www.thesportsdb.com/league/4422?season=2026",
        "https://www.thesportsdb.com/league/4422",
        "https://www.thesportsdb.com/league/4422?season=2026-2027&key=123",
        "https://www.thesportsdb.com/league/abc?season=2026-2027",
        "https://www.thesportsdb.com/league/0422?season=2026-2027",
        "https://www.thesportsdb.com/api/v1/json/123/eventsday.php?d=2026-11-21&l=4422",
        "https://www.thesportsdb.com/league/4422/x?season=2026-2027",
        "https://www.thesportsdb.com:8443/league/4422?season=2026-2027",
    ],
)
def test_invalid_thesportsdb_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        source_from_url("2026-2027", url)


SEASONS = {
    "seasons": [
        {"strSeason": "2016-2017"},
        {"strSeason": "2026-2027"},
        {"strSeason": "2025-2026"},
        {"strSeason": "2020"},
    ]
}


def test_seasons_are_parsed_newest_first_in_the_competition_format() -> None:
    sources = parse_thesportsdb_seasons(SEASONS, EKSTRAKLASA, "4422")

    assert [source.season for source in sources] == ["2026-2027", "2025-2026", "2016-2017"]
    for source in sources:
        assert source_from_url(source.season, source.source_url) == source
    assert parse_thesportsdb_seasons({"seasons": None}, EKSTRAKLASA, "4422") == []


async def test_discovery_uses_the_league_of_the_fallback_sources() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=SEASONS)

    competition = EKSTRAKLASA.model_copy(update={"seasons": {"2026-2027": SOURCE}})
    discovered = await mock_scraper(handler).discover_seasons(competition)

    assert requested == [
        "https://www.thesportsdb.com/api/v1/json/123/search_all_seasons.php?id=4422"
    ]
    assert discovered[0].source_url == SOURCE_URL


async def test_discovery_needs_a_thesportsdb_source() -> None:
    with pytest.raises(ValueError, match="TheSportsDB league"):
        await mock_scraper(lambda request: httpx.Response(500)).discover_seasons(EKSTRAKLASA)


def write_config(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_windowed_provider_is_allowed_only_as_fallback(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    ekstraklasa = data["competitions"][0]
    ekstraklasa["fallback_provider"] = "thesportsdb"
    ekstraklasa["seasons"]["2026-2027"]["fallback_url"] = SOURCE_URL
    config = load_config(write_config(tmp_path, data))
    assert config.to_competitions()["ekstraklasa"].seasons["2026-2027"].fallback() is not None

    ekstraklasa["provider"] = "thesportsdb"
    ekstraklasa["fallback_provider"] = None
    ekstraklasa["seasons"] = {"2026-2027": {"source_url": SOURCE_URL}}
    with pytest.raises(ValueError, match="windowed provider 'thesportsdb'"):
        load_config(write_config(tmp_path, data))


def test_window_settings_are_validated(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    assert load_config(DEFAULT_CONFIG).providers["thesportsdb"].window() == (7, 14)

    data["providers"]["thesportsdb"] = {"min_request_interval_seconds": 3}
    assert load_config(write_config(tmp_path, data)).providers["thesportsdb"].window() == (7, 14)

    data["providers"]["thesportsdb"]["window_days_back"] = -1
    with pytest.raises(ValueError, match="window_days_back"):
        load_config(write_config(tmp_path, data))

    data["providers"]["thesportsdb"]["window_days_back"] = 7
    data["providers"]["espn"]["window_days_ahead"] = 3
    with pytest.raises(ValueError, match="not windowed"):
        load_config(write_config(tmp_path, data))
