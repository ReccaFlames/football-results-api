import pytest

from app.competitions import source_from_url
from app.config import load_config
from app.scraper import (
    MATCH_STATISTICS,
    FootballDataScraper,
    NinetyMinutScraper,
    parse_football_data_matches,
    parse_football_data_seasons,
)

COMPETITIONS = load_config().to_competitions()


def source():
    competition = COMPETITIONS["premier-league"]
    return competition.seasons[competition.current_season]


def match(status: str, home_score=None, away_score=None) -> dict[str, object]:
    return {
        "id": 537963,
        "utcDate": "2026-08-30T15:30:00Z",
        "status": status,
        "matchday": 2,
        "homeTeam": {"id": 64, "name": "Liverpool FC"},
        "awayTeam": {"id": 57, "name": "Arsenal FC"},
        "score": {
            "fullTime": {"home": home_score, "away": away_score},
            "halfTime": {"home": 1, "away": 0},
        },
        "goals": [
            {
                "minute": 45,
                "injuryTime": 2,
                "team": {"id": 64, "name": "Liverpool FC"},
            },
            {"minute": 70, "team": {"id": 57, "name": "Arsenal FC"}},
        ],
        "bookings": [
            {"minute": 25, "team": {"id": 64, "name": "Liverpool FC"}},
            {"minute": 82, "team": {"id": 57, "name": "Arsenal FC"}},
            {"minute": 89, "team": {"id": 57, "name": "Arsenal FC"}},
        ],
    }


def test_parses_finished_match() -> None:
    matches = parse_football_data_matches(
        {"matches": [match("FINISHED", 2, 1)]},
        COMPETITIONS["premier-league"],
        source(),
    )
    assert len(matches) == 1
    parsed = matches[0]
    assert parsed.status == "finished"
    assert (parsed.home_score, parsed.away_score) == (2, 1)
    assert parsed.round == 2
    assert parsed.stage == "Matchweek 2"
    assert parsed.played_at.isoformat() == "2026-08-30T15:30:00+00:00"
    assert parsed.source_url == "https://api.football-data.org/v4/matches/537963"
    assert parsed.provider_match_id == "537963"
    assert parsed.home_goals == 2
    assert parsed.away_goals == 1
    assert parsed.home_goals_first_half == 1
    assert parsed.away_goals_first_half == 0
    assert parsed.home_goals_second_half == 1
    assert parsed.away_goals_second_half == 1
    assert parsed.home_cards == 1
    assert parsed.away_cards == 2
    assert parsed.first_team_to_score == "Liverpool FC"
    assert parsed.first_goal_minute == "45+2"
    assert parsed.home_corners is None
    assert parsed.home_fouls is None


def test_scheduled_match_has_no_score() -> None:
    matches = parse_football_data_matches(
        {"matches": [match("TIMED")]},
        COMPETITIONS["premier-league"],
        source(),
    )
    assert matches[0].status == "scheduled"
    assert matches[0].home_score is None
    assert matches[0].away_score is None


def test_postponed_match_does_not_trigger_overdue_polling() -> None:
    matches = parse_football_data_matches(
        {"matches": [match("POSTPONED")]},
        COMPETITIONS["premier-league"],
        source(),
    )
    assert matches[0].status == "inactive"


def test_finished_status_requires_actual_scores() -> None:
    matches = parse_football_data_matches(
        {"matches": [match("FINISHED")]},
        COMPETITIONS["premier-league"],
        source(),
    )
    assert matches[0].status == "scheduled"


def test_historical_url_detects_provider() -> None:
    value = source_from_url(
        "2024-2025",
        "https://api.football-data.org/v4/competitions/PL/matches?season=2024",
    )
    assert value.provider == "football-data"


def test_historical_url_must_match_requested_season() -> None:
    try:
        source_from_url(
            "2024-2025",
            "https://api.football-data.org/v4/competitions/PL/matches?season=2025",
        )
    except ValueError:
        return
    raise AssertionError("mismatched football-data.org season was accepted")


def test_missing_matches_list_is_rejected() -> None:
    try:
        parse_football_data_matches({}, COMPETITIONS["premier-league"], source())
    except TypeError as error:
        assert "matches list" in str(error)
        return
    raise AssertionError("invalid response was accepted")


@pytest.mark.parametrize(
    ("season", "url"),
    [
        ("2025-2026", "https://api.football-data.org/v4/competitions/BL1/matches?season=2025"),
        ("2025-2026", "https://api.football-data.org/v4/competitions/CL/matches?season=2025"),
        ("2026", "https://api.football-data.org/v4/competitions/BSA/matches?season=2026"),
    ],
)
def test_any_competition_code_is_accepted(season: str, url: str) -> None:
    assert source_from_url(season, url).provider == "football-data"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.football-data.org/v4/competitions/bl1/matches?season=2025",
        "https://api.football-data.org/v4/competitions/BL1/teams?season=2025",
        "https://api.football-data.org/v4/competitions/../matches?season=2025",
        "https://api.football-data.org/v4/competitions/BL1/matches?season=2025&limit=1",
    ],
)
def test_invalid_football_data_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="football-data.org"):
        source_from_url("2025-2026", url)


def test_cup_stages_and_groups_are_named() -> None:
    knockout = match("FINISHED", 2, 1) | {"stage": "LAST_16", "matchday": None}
    group = match("FINISHED", 2, 1) | {"id": 2, "stage": "GROUP_STAGE", "group": "GROUP_A"}
    league = match("FINISHED", 2, 1) | {"id": 3, "stage": "REGULAR_SEASON"}

    parsed = parse_football_data_matches(
        {"matches": [knockout, group, league]}, COMPETITIONS["premier-league"], source()
    )

    assert [(item.stage, item.round) for item in parsed] == [
        ("Last 16", None),
        ("Group Stage, Group A, Matchday 2", 2),
        ("Matchweek 2", 2),
    ]


def test_discovered_seasons_follow_the_competition_season_format() -> None:
    payload = {
        "seasons": [
            {"startDate": "2025-08-15", "endDate": "2026-05-24"},
            {"startDate": "2026-08-21", "endDate": "2027-05-30"},
            {"startDate": "not-a-date"},
        ]
    }
    split = parse_football_data_seasons(payload, COMPETITIONS["premier-league"], "PL")
    calendar = parse_football_data_seasons(
        {"seasons": [{"startDate": "2026-01-28", "endDate": "2026-12-06"}]},
        COMPETITIONS["premier-league"].model_copy(update={"season_format": "calendar"}),
        "BSA",
    )

    assert [(item.season, item.source_url) for item in split] == [
        ("2026-2027", "https://api.football-data.org/v4/competitions/PL/matches?season=2026"),
        ("2025-2026", "https://api.football-data.org/v4/competitions/PL/matches?season=2025"),
    ]
    assert [(item.season, item.source_url) for item in calendar] == [
        ("2026", "https://api.football-data.org/v4/competitions/BSA/matches?season=2026"),
    ]


def test_parsed_statistics_stay_within_declared_capabilities() -> None:
    parsed = parse_football_data_matches(
        {"matches": [match("FINISHED", 2, 1)]}, COMPETITIONS["premier-league"], source()
    )[0]
    undeclared = MATCH_STATISTICS - FootballDataScraper.capabilities.statistics

    assert all(getattr(parsed, field) is None for field in undeclared)
    assert FootballDataScraper.capabilities.season_discovery
    assert not NinetyMinutScraper.capabilities.season_discovery


async def test_discovery_uses_the_configured_competition_code(monkeypatch) -> None:
    requested: list[str] = []
    scraper = FootballDataScraper(1, "test", "token", 0)

    async def fake_get_json(url: str) -> dict[str, object]:
        requested.append(url)
        return {"seasons": [{"startDate": "2026-08-21"}]}

    monkeypatch.setattr(scraper, "_get_json", fake_get_json)
    discovered = await scraper.discover_seasons(COMPETITIONS["premier-league"])

    assert requested == ["https://api.football-data.org/v4/competitions/PL"]
    assert [item.season for item in discovered] == ["2026-2027"]
