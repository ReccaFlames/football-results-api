import pytest

from app.competitions import source_from_url
from app.config import load_config
from app.models import ScrapedMatch
from app.scraper import (
    MATCH_STATISTICS,
    EspnScraper,
    ScraperRegistry,
    espn_request_headers,
    parse_espn_matches,
)

PREMIER_LEAGUE = load_config().to_competitions()["premier-league"]
SOURCE = source_from_url(
    "2026-2027",
    "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2026",
)
FULHAM, PALACE = "370", "384"


def detail(kind: str, minute: str, team: str, **flags: bool) -> dict[str, object]:
    return {
        "type": {"text": kind},
        "clock": {"displayValue": minute},
        "team": {"id": team},
        **flags,
    }


def goal(minute: str, team: str, **flags: bool) -> dict[str, object]:
    return detail("Goal", minute, team, scoringPlay=True, **flags)


def card(minute: str, team: str, red: bool = False) -> dict[str, object]:
    kind = "Red Card" if red else "Yellow Card"
    return detail(kind, minute, team, scoringPlay=False, yellowCard=not red, redCard=red)


def competitor(side: str, team_id: str, name: str, score: str, fouls: str, corners: str):
    return {
        "homeAway": side,
        "score": score,
        "team": {"id": team_id, "displayName": name},
        "statistics": [
            {"name": "foulsCommitted", "displayValue": fouls},
            {"name": "wonCorners", "displayValue": corners},
        ],
    }


def event(
    status: str = "STATUS_FULL_TIME",
    completed: bool = True,
    score: tuple[str, str] = ("2", "3"),
    details: list[dict[str, object]] | None = None,
    year: int = 2026,
    event_id: str = "401879289",
) -> dict[str, object]:
    return {
        "id": event_id,
        "date": "2026-09-05T14:00Z",
        "season": {"year": year},
        "status": {"type": {"name": status, "completed": completed}},
        "competitions": [
            {
                "competitors": [
                    competitor("home", FULHAM, "Fulham", score[0], "6", "7"),
                    competitor("away", PALACE, "Crystal Palace", score[1], "7", "1"),
                ],
                "details": details if details is not None else [],
            }
        ],
    }


FULHAM_PALACE = [
    goal("11'", FULHAM),
    goal("35'", PALACE),
    # ESPN credits an own goal to the team whose score it raised.
    goal("45'+2'", FULHAM, ownGoal=True),
    goal("54'", PALACE),
    card("60'", PALACE),
    card("70'", PALACE),
    card("85'", PALACE, red=True),  # second yellow: a yellow and a red card event
    goal("90'+4'", PALACE),
]


def parse(*events: dict[str, object]) -> list[ScrapedMatch]:
    return parse_espn_matches([{"events": list(events)}], PREMIER_LEAGUE, SOURCE)


def test_parses_finished_match_with_every_statistic() -> None:
    [parsed] = parse(event(details=FULHAM_PALACE))

    assert (parsed.status, parsed.home_team, parsed.away_team) == (
        "finished",
        "Fulham",
        "Crystal Palace",
    )
    assert (parsed.home_score, parsed.away_score) == (2, 3)
    assert (parsed.round, parsed.stage) == (None, "Premier League")
    assert parsed.played_at.isoformat() == "2026-09-05T14:00:00+00:00"
    assert parsed.provider_match_id == "401879289"
    assert parsed.source_url == "https://www.espn.com/soccer/match/_/gameId/401879289"
    assert (parsed.home_goals_first_half, parsed.away_goals_first_half) == (2, 1)
    assert (parsed.home_goals_second_half, parsed.away_goals_second_half) == (0, 2)
    assert (parsed.home_cards, parsed.away_cards) == (0, 3)
    assert (parsed.home_fouls, parsed.away_fouls) == (6, 7)
    assert (parsed.home_corners, parsed.away_corners) == (7, 1)
    assert (parsed.first_team_to_score, parsed.first_goal_minute) == ("Fulham", "11")


def test_stoppage_time_first_goal() -> None:
    [parsed] = parse(event(score=("1", "0"), details=[goal("45'+2'", FULHAM)]))

    assert (parsed.first_team_to_score, parsed.first_goal_minute) == ("Fulham", "45+2")
    assert parsed.home_goals_first_half == 1


def test_incomplete_goal_events_leave_goal_details_and_cards_unknown() -> None:
    [parsed] = parse(event(details=[goal("11'", FULHAM)]))

    assert (parsed.home_goals, parsed.away_goals) == (2, 3)
    assert parsed.home_goals_first_half is None
    assert parsed.first_team_to_score is None
    assert parsed.first_goal_minute is None
    assert (parsed.home_cards, parsed.away_cards) == (None, None)


def test_goalless_match_has_no_first_goal_and_zero_cards() -> None:
    [parsed] = parse(event(score=("0", "0")))

    assert (parsed.first_team_to_score, parsed.first_goal_minute) == (None, None)
    assert (parsed.home_cards, parsed.away_cards) == (0, 0)


def test_scheduled_and_postponed_fixtures() -> None:
    scheduled, postponed = parse(
        event("STATUS_SCHEDULED", completed=False, score=("0", "0"), event_id="1"),
        event("STATUS_POSTPONED", completed=False, score=("0", "0"), event_id="2"),
    )

    assert scheduled.status == "scheduled"
    assert postponed.status == "inactive"
    for fixture in (scheduled, postponed):
        assert fixture.home_score is None
        assert all(getattr(fixture, field) is None for field in MATCH_STATISTICS)


def test_live_match_stays_scheduled() -> None:
    [parsed] = parse(event("STATUS_FIRST_HALF", completed=False, score=("1", "0")))

    assert parsed.status == "scheduled"
    assert parsed.home_score is None


def test_events_of_other_seasons_are_ignored() -> None:
    assert parse(event(year=2025)) == []


def test_missing_events_list_is_rejected() -> None:
    with pytest.raises(TypeError, match="events list"):
        parse_espn_matches([{"leagues": []}], PREMIER_LEAGUE, SOURCE)


def test_parsed_statistics_stay_within_declared_capabilities() -> None:
    [parsed] = parse(event(details=FULHAM_PALACE))
    undeclared = MATCH_STATISTICS - EspnScraper.capabilities.statistics

    assert all(getattr(parsed, field) is None for field in undeclared)
    assert all(getattr(parsed, field) is not None for field in EspnScraper.capabilities.statistics)


@pytest.mark.parametrize(
    "url",
    [
        "http://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2026",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2025",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?season=2026",
        "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/summary?season=2026",
        "https://espn.example.com/apis/site/v2/sports/soccer/eng.1/scoreboard?season=2026",
    ],
)
def test_invalid_espn_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        source_from_url("2026-2027", url)


async def test_split_season_requests_both_calendar_years(monkeypatch) -> None:
    requested: list[str] = []
    scraper = EspnScraper(1, "test", 0)

    async def fake_get_json(url: str) -> dict[str, object]:
        requested.append(url)
        return {
            "events": [event(details=FULHAM_PALACE)]
            if url.endswith("dates=2026&limit=1000")
            else []
        }

    monkeypatch.setattr(scraper, "_get_json", fake_get_json)
    matches = await scraper.scrape(PREMIER_LEAGUE, SOURCE)

    base = "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"
    assert requested == [f"{base}?dates=2026&limit=1000", f"{base}?dates=2027&limit=1000"]
    assert len(matches) == 1


async def test_truncated_event_list_fails_the_scrape(monkeypatch) -> None:
    scraper = EspnScraper(1, "test", 0)

    async def fake_get_json(url: str) -> dict[str, object]:
        return {"events": [{}] * 1000}

    monkeypatch.setattr(scraper, "_get_json", fake_get_json)
    with pytest.raises(ValueError, match="truncated"):
        await scraper.scrape(PREMIER_LEAGUE, SOURCE)


async def test_discovery_reports_the_current_espn_season(monkeypatch) -> None:
    requested: list[str] = []
    scraper = EspnScraper(1, "test", 0)

    async def fake_get_json(url: str) -> dict[str, object]:
        requested.append(url)
        return {"leagues": [{"season": {"year": 2027}}], "events": []}

    monkeypatch.setattr(scraper, "_get_json", fake_get_json)
    [discovered] = await scraper.discover_seasons(PREMIER_LEAGUE)

    assert requested == ["https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"]
    assert discovered.season == "2027-2028"
    assert source_from_url(discovered.season, discovered.source_url) == discovered


async def test_registry_applies_configured_team_aliases() -> None:
    class Provider:
        name = "espn"
        capabilities = EspnScraper.capabilities

        async def scrape(self, competition, source):
            return parse(event(details=FULHAM_PALACE))

        async def discover_seasons(self, competition):
            return []

    registry = ScraperRegistry({"espn": Provider()}, {"premier-league": {"Fulham": "Fulham FC"}})
    [parsed] = await registry.scrape(PREMIER_LEAGUE, SOURCE)

    assert (parsed.home_team, parsed.away_team) == ("Fulham FC", "Crystal Palace")
    assert parsed.first_team_to_score == "Fulham FC"


def test_contact_email_moves_from_user_agent_to_from_header() -> None:
    assert espn_request_headers("App/1.0 (contact: ops+pl@example.co.uk)") == {
        "User-Agent": "App/1.0 (contact: see From header)",
        "From": "ops+pl@example.co.uk",
    }
    assert espn_request_headers("App/1.0") == {"User-Agent": "App/1.0"}
