from app.competitions import source_from_url
from app.config import load_config
from app.scraper import parse_football_data_matches

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
