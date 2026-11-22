import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app import agent_docs
from app.competitions import competition_summary, source_from_url
from app.config import load_config
from app.main import app
from app.models import ScrapedMatch
from app.scraper import (
    MATCH_STATISTICS,
    OpenLigaDbScraper,
    create_scraper_registry,
    parse_openligadb_matches,
    parse_openligadb_seasons,
)

DEFAULT_CONFIG = Path(__file__).parents[1] / "config/competitions.yaml"
LIGA3 = load_config(DEFAULT_CONFIG).to_competitions()["3-liga"]
SOURCE_URL = "https://api.openligadb.de/getmatchdata/bl3/2026"
SOURCE = source_from_url("2026-2027", SOURCE_URL)
KICKOFF = "2026-08-07T18:30:00Z"
NOW = datetime(2026, 11, 22, 14, 0, tzinfo=UTC)


def goal(home: int, away: int, minute: int | None, team_id: int = 54, own: bool = False):
    return {
        "goalID": 145384 + home * 10 + away,
        "scoreTeam1": home,
        "scoreTeam2": away,
        "matchMinute": minute,
        "goalGetterID": 23532,
        "goalGetterName": "Sebastian Grönning",
        "scoringTeamId": team_id,
        "isPenalty": False,
        "isOwnGoal": own,
        "isOvertime": False,
        "comment": None,
    }


def result(type_id: int, home: int, away: int) -> dict[str, object]:
    names = {1: ("Halbzeit", "HalfTime"), 2: ("Endergebnis", "After90Minutes")}
    return {
        "resultID": 127032 + type_id,
        "resultName": names[type_id][0],
        "pointsTeam1": home,
        "pointsTeam2": away,
        "resultOrderID": type_id,
        "resultTypeID": type_id,
        "resultTypeKind": names[type_id][1],
    }


def match(
    match_id: int = 83509,
    kickoff: str = KICKOFF,
    finished: bool = True,
    results: list[dict[str, object]] | None = None,
    goals: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """A ``getmatchdata`` match, shaped like an archived 2. Bundesliga 2026-27 response."""
    return {
        "matchID": match_id,
        "matchDateTime": "2026-08-07T20:30:00",
        "timeZoneID": "W. Europe Standard Time",
        "leagueId": 4939,
        "leagueName": "3. Liga 2026/2027",
        "leagueSeason": 2026,
        "leagueShortcut": "bl3",
        "matchDateTimeUTC": kickoff,
        "group": {"groupName": "1. Spieltag", "groupOrderID": 1, "groupID": 50668},
        "team1": {"teamId": 129, "teamName": "VfL Bochum", "shortName": "Bochum"},
        "team2": {"teamId": 54, "teamName": "Hertha BSC", "shortName": "Hertha"},
        "lastUpdateDateTime": "2026-08-07T22:26:29.493",
        "matchIsFinished": finished,
        # Full time listed first: scores must be chosen by resultTypeID, not by position.
        "matchResults": [result(2, 0, 1), result(1, 0, 1)] if results is None else results,
        "goals": [goal(0, 1, 39)] if goals is None else goals,
        "location": None,
        "numberOfViewers": None,
    }


def unplayed(kickoff: str, match_id: int = 83510) -> dict[str, object]:
    return match(match_id, kickoff, finished=False, results=[], goals=[])


def parse(*matches: dict[str, object], now: datetime = NOW) -> list[ScrapedMatch]:
    return parse_openligadb_matches(list(matches), LIGA3, SOURCE, now)


def test_finished_match_with_goals() -> None:
    [parsed] = parse(match())

    assert parsed.status == "finished"
    assert parsed.played_at == datetime(2026, 8, 7, 18, 30, tzinfo=UTC)
    assert (parsed.round, parsed.stage) == (1, "1. Spieltag")
    assert parsed.provider_match_id == "83509"
    assert (parsed.home_team, parsed.away_team) == ("VfL Bochum", "Hertha BSC")
    assert (parsed.home_score, parsed.away_score) == (0, 1)
    assert (parsed.home_goals, parsed.away_goals) == (0, 1)
    assert (parsed.home_goals_first_half, parsed.away_goals_first_half) == (0, 1)
    assert (parsed.home_goals_second_half, parsed.away_goals_second_half) == (0, 0)
    assert (parsed.first_team_to_score, parsed.first_goal_minute) == ("Hertha BSC", "39")
    assert parsed.home_cards is parsed.away_cards is None
    assert parsed.home_corners is parsed.away_corners is None
    assert parsed.home_fouls is parsed.away_fouls is None


def test_scheduled_match() -> None:
    [parsed] = parse(unplayed("2026-11-29T12:00:00Z"))

    assert parsed.status == "scheduled"
    assert parsed.home_score is parsed.away_score is None
    assert all(getattr(parsed, name) is None for name in MATCH_STATISTICS)


@pytest.mark.parametrize(
    ("hours_past_kickoff", "status"), [(23, "scheduled"), (24, "scheduled"), (25, "inactive")]
)
def test_unfinished_match_is_inactive_after_the_postponed_threshold(
    hours_past_kickoff: int, status: str
) -> None:
    kickoff = NOW - timedelta(hours=hours_past_kickoff)
    [parsed] = parse(unplayed(kickoff.isoformat()))

    assert parsed.status == status


def test_postponed_threshold_is_configurable() -> None:
    kickoff = NOW - timedelta(hours=5)
    matches = [unplayed(kickoff.isoformat())]

    assert parse_openligadb_matches(matches, LIGA3, SOURCE, NOW, timedelta(hours=4))[0].status == (
        "inactive"
    )
    assert parse_openligadb_matches(matches, LIGA3, SOURCE, NOW, timedelta(hours=6))[0].status == (
        "scheduled"
    )


def test_finished_match_without_a_full_time_result_stays_unfinished(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        [parsed] = parse(match(kickoff=NOW.isoformat(), results=[result(1, 0, 1)]))

    assert parsed.status == "scheduled"
    assert parsed.home_score is None and parsed.home_goals is None
    assert "83509" in caplog.text and "no full-time result" in caplog.text


def test_goals_that_do_not_add_up_leave_the_first_goal_unknown() -> None:
    results = [result(1, 1, 0), result(2, 2, 1)]
    missing_goal = match(results=results, goals=[goal(1, 0, 12, 129), goal(2, 0, 50, 129)])
    duplicate = match(results=results, goals=[goal(1, 0, 12, 129), goal(1, 0, 13, 129)])

    for parsed in parse(missing_goal) + parse(duplicate):
        assert (parsed.home_goals, parsed.away_goals) == (2, 1)
        assert (parsed.home_goals_first_half, parsed.away_goals_second_half) == (1, 1)
        assert parsed.first_team_to_score is None and parsed.first_goal_minute is None


def test_own_goal_counts_for_the_team_whose_score_it_raised() -> None:
    # A Hertha (team 54) player scores into his own net: Bochum's score goes up.
    own_goal = goal(1, 0, 7, team_id=54, own=True)
    [parsed] = parse(
        match(results=[result(2, 1, 1), result(1, 1, 0)], goals=[goal(1, 1, 80), own_goal])
    )

    assert (parsed.first_team_to_score, parsed.first_goal_minute) == ("VfL Bochum", "7")


def test_missing_half_time_result_stays_null() -> None:
    [parsed] = parse(match(results=[result(2, 0, 1)]))

    assert (parsed.home_goals, parsed.away_goals) == (0, 1)
    assert parsed.home_goals_first_half is parsed.away_goals_first_half is None
    assert parsed.home_goals_second_half is parsed.away_goals_second_half is None
    assert parsed.first_team_to_score == "Hertha BSC"


def test_goalless_match_has_no_first_goal() -> None:
    [parsed] = parse(match(results=[result(2, 0, 0), result(1, 0, 0)], goals=[]))

    assert (parsed.home_goals, parsed.away_goals, parsed.home_goals_second_half) == (0, 0, 0)
    assert parsed.first_team_to_score is None and parsed.first_goal_minute is None


@pytest.mark.parametrize(
    "payload",
    [
        {"matches": []},
        ["not a match"],
        [{**match(), "matchID": None}],
        [{**match(), "matchDateTimeUTC": "2026-08-07T18:30:00"}],
        [{**match(), "matchDateTimeUTC": "tomorrow"}],
        [{**match(), "team2": None}],
    ],
)
def test_malformed_payload_raises(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_openligadb_matches(payload, LIGA3, SOURCE, NOW)


def test_parsed_statistics_stay_within_declared_capabilities() -> None:
    capabilities = OpenLigaDbScraper.capabilities
    matches = parse(match(), unplayed("2026-11-29T12:00:00Z"))

    assert capabilities.season_discovery and not capabilities.windowed
    assert not capabilities.requires_api_token
    for parsed in matches:
        filled = {name for name in MATCH_STATISTICS if getattr(parsed, name) is not None}
        assert filled <= capabilities.statistics


LEAGUES = [
    {"leagueId": 4938, "leagueShortcut": "bl2", "leagueSeason": 2026, "leagueName": "2. Liga"},
    {"leagueId": 4500, "leagueShortcut": "bl3", "leagueSeason": "2025", "leagueName": "3. Liga"},
    {"leagueId": 4939, "leagueShortcut": "bl3", "leagueSeason": 2026, "leagueName": "3. Liga"},
    {"leagueId": 4100, "leagueShortcut": "bl3", "leagueSeason": 2024, "leagueName": "3. Liga"},
    {"leagueId": 4101, "leagueShortcut": "bl3", "leagueSeason": "n/a", "leagueName": "3. Liga"},
]


def test_seasons_are_discovered_newest_first_for_the_shortcut() -> None:
    sources = parse_openligadb_seasons(LEAGUES, LIGA3, "bl3")

    assert [source.season for source in sources] == ["2026-2027", "2025-2026", "2024-2025"]
    assert sources[0].source_url == SOURCE_URL
    for source in sources:
        assert source_from_url(source.season, source.source_url) == source
    with pytest.raises(TypeError):
        parse_openligadb_seasons({"leagues": LEAGUES}, LIGA3, "bl3")


def mock_scraper(handler) -> OpenLigaDbScraper:
    return OpenLigaDbScraper(
        1, "test", 0, clock=lambda: NOW, transport=httpx.MockTransport(handler)
    )


async def test_scrape_requests_the_whole_season_once() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=[match(), unplayed("2026-11-29T12:00:00Z")])

    matches = await mock_scraper(handler).scrape(LIGA3, SOURCE)

    assert requested == [SOURCE_URL]
    assert [item.status for item in matches] == ["finished", "scheduled"]


async def test_discovery_uses_getavailableleagues() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=LEAGUES)

    discovered = await mock_scraper(handler).discover_seasons(LIGA3)

    assert requested == ["https://api.openligadb.de/getavailableleagues"]
    assert discovered[0] == SOURCE


async def test_discovery_needs_an_openligadb_source() -> None:
    competition = LIGA3.model_copy(update={"seasons": {}})
    with pytest.raises(ValueError, match="OpenLigaDB league shortcut"):
        await mock_scraper(lambda request: httpx.Response(500)).discover_seasons(competition)


def test_valid_openligadb_urls_are_accepted() -> None:
    assert SOURCE.provider == "openligadb"
    assert source_from_url("2026", "https://api.openligadb.de/getmatchdata/wm26/2026").season == (
        "2026"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openligadb.de/getmatchdata/bl3/2026",
        "https://openligadb.de/getmatchdata/bl3/2026",
        "https://api.openligadb.de/getmatchdata/bl3/2025",
        "https://api.openligadb.de/getmatchdata/BL3/2026",
        "https://api.openligadb.de/getmatchdata/bl-3/2026",
        "https://api.openligadb.de/getmatchdata/bl3/2026/1",
        "https://api.openligadb.de/getmatchdata/bl3/2026?x=1",
        "https://api.openligadb.de/getmatchdata/bl3",
        "https://api.openligadb.de/getavailableleagues",
        "https://api.openligadb.de:8443/getmatchdata/bl3/2026",
    ],
)
def test_invalid_openligadb_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        source_from_url("2026-2027", url)


def write_config(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "competitions.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def liga3_entry(data: dict[str, object]) -> dict[str, object]:
    return next(item for item in data["competitions"] if item["slug"] == "3-liga")


def test_config_rejects_mixed_shortcuts(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    liga3_entry(data)["seasons"]["2025-2026"]["source_url"] = (
        "https://api.openligadb.de/getmatchdata/bl2/2025"
    )

    with pytest.raises(ValueError, match="mixes provider competition codes"):
        load_config(write_config(tmp_path, data))


def test_postponed_after_hours_is_only_for_openligadb(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_CONFIG.read_text())
    assert load_config(DEFAULT_CONFIG).providers["openligadb"].postponed_after() == 24

    data["providers"]["openligadb"] = {"min_request_interval_seconds": 2}
    assert load_config(write_config(tmp_path, data)).providers["openligadb"].postponed_after() == 24

    data["providers"]["espn"]["postponed_after_hours"] = 12
    with pytest.raises(ValueError, match="only supported for 'openligadb'"):
        load_config(write_config(tmp_path, data))


def test_registry_passes_the_postponed_threshold() -> None:
    registry = create_scraper_registry(
        1, "test", None, 0, openligadb_min_interval_seconds=5, openligadb_postponed_after_hours=6
    )
    scraper = registry.provider("openligadb")

    assert scraper.min_interval_seconds == 5
    assert scraper.postponed_after == timedelta(hours=6)


def test_agent_docs_attribute_openligadb_only_when_configured() -> None:
    competitions = [competition_summary(item) for item in load_config().to_competitions().values()]
    arguments = {
        "base_url": "https://football.example",
        "competitions_live": True,
        "rate_limits": agent_docs.RateLimits(True, 120, 10),
        "cache_max_age_seconds": 60,
    }
    with_openligadb = agent_docs.build_guide(
        app.openapi(),
        competitions,
        provider_competitions={"openligadb": ["3-liga"], "espn": ["premier-league"]},
        **arguments,
    )
    without = agent_docs.build_guide(
        app.openapi(), competitions, provider_competitions={"espn": ["x"]}, **arguments
    )

    for page in (
        agent_docs.llms_txt(with_openligadb),
        agent_docs.agent_guide_markdown(with_openligadb),
    ):
        assert "`3-liga`: data from [OpenLigaDB](https://www.openligadb.de)" in page
        assert "Open Database License (ODbL)" in page
    assert 'href="https://www.openligadb.de"' in agent_docs.agent_guide_html(with_openligadb)
    assert "ODbL" not in agent_docs.llms_txt(without)
    assert "ODbL" not in agent_docs.agent_guide_markdown(without)


def test_served_agent_docs_attribute_the_configured_openligadb_competition() -> None:
    client = TestClient(app)

    assert "`3-liga`: data from [OpenLigaDB]" in client.get("/llms.txt").text
    assert "ODbL" in client.get("/agents").text
