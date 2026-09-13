from app.competitions import COMPETITIONS
from app.scraper import parse_matches, parse_ninety_minut_details, parse_results


def current_source(slug: str):
    competition = COMPETITIONS[slug]
    return competition.seasons[competition.current_season]


def page_with_rows(header: str, rows: str) -> str:
    return f"""
    <table class="main"><tr><td><b><u>{header}</u></b></td></tr></table>
    <p></p><table class="main">{rows}</table>
    """


def result_row(score: str = "2-1") -> str:
    return f"""
      <tr align="left">
        <td width="180"><b> Górnik Zabrze </b></td>
        <td align="center" width="50">
          <a href="/mecz.php?id_mecz=2113280"><b>{score}</b></a>
        </td>
        <td width="180"><b> Śląsk Wrocław </b></td>
        <td width="190">25 lipca, 17:30 (<nobr>25 998</nobr>)</td>
      </tr>
    """


def test_parses_actual_90minut_league_markup() -> None:
    competition = COMPETITIONS["ekstraklasa"]
    results = parse_results(
        page_with_rows("Kolejka 1 - 25-26 lipca", result_row()),
        competition,
        current_source("ekstraklasa"),
    )
    assert len(results) == 1
    assert results[0].competition == "ekstraklasa"
    assert results[0].stage == "Kolejka 1 - 25-26 lipca"
    assert results[0].round == 1
    assert results[0].home_team == "Górnik Zabrze"
    assert results[0].away_team == "Śląsk Wrocław"
    assert (results[0].home_score, results[0].away_score) == (2, 1)
    assert (results[0].home_goals, results[0].away_goals) == (2, 1)
    assert results[0].home_goals_first_half is None
    assert results[0].played_at.isoformat() == "2026-07-25T17:30:00+02:00"
    assert results[0].source_url.endswith("/mecz.php?id_mecz=2113280")


def test_parses_named_europa_league_stage() -> None:
    competition = COMPETITIONS["europa-league"]
    cup_row = (
        result_row("1-1")
        .replace('<tr align="left">', '<tr align="left"><td></td>')
        .replace("</tr>", "<td></td></tr>")
    )
    results = parse_results(
        page_with_rows("I runda eliminacyjna - 9 lipca, 16 lipca", cup_row),
        competition,
        current_source("europa-league"),
    )
    assert len(results) == 1
    assert results[0].competition == "europa-league"
    assert results[0].stage.startswith("I runda eliminacyjna")
    assert results[0].round is None


def test_parses_unlinked_cup_score_with_match_date() -> None:
    competition = COMPETITIONS["europa-league"]
    row = result_row("1-1").replace('<a href="/mecz.php?id_mecz=2113280"><b>1-1</b></a>', "1-1")
    results = parse_results(
        page_with_rows("II runda eliminacyjna - 23 lipca, 30 lipca", row),
        competition,
        current_source("europa-league"),
    )
    assert len(results) == 1
    assert results[0].source_url == current_source("europa-league").source_url


def test_never_treats_kickoff_time_as_score() -> None:
    html = page_with_rows(
        "Kolejka 1",
        """
        <tr><td>Wisła Kraków</td><td>-</td><td>Śląsk Wrocław</td>
        <td>18 września, 20:30</td></tr>
        """,
    )
    assert (
        parse_results(
            html,
            COMPETITIONS["pierwsza-liga"],
            current_source("pierwsza-liga"),
        )
        == []
    )

    fixtures = parse_matches(
        html,
        COMPETITIONS["pierwsza-liga"],
        current_source("pierwsza-liga"),
    )
    assert len(fixtures) == 1
    assert fixtures[0].status == "scheduled"
    assert fixtures[0].home_score is None
    assert fixtures[0].played_at.hour == 20
    assert fixtures[0].played_at.minute == 30


def test_rejects_score_like_text_without_match_link() -> None:
    html = page_with_rows(
        "Kolejka 1",
        "<tr><td>Team A</td><td>20-30</td><td>Team B</td><td>18:00</td></tr>",
    )
    assert (
        parse_results(
            html,
            COMPETITIONS["ekstraklasa"],
            current_source("ekstraklasa"),
        )
        == []
    )


def test_parses_90minut_match_detail_statistics() -> None:
    match = parse_matches(
        page_with_rows("Kolejka 1", result_row()),
        COMPETITIONS["ekstraklasa"],
        current_source("ekstraklasa"),
    )[0]
    html = """
    <table>
      <tr><td>Górnik Zabrze</td><td><nobr>2 - 1</nobr></td><td>Śląsk Wrocław</td></tr>
      <tr><td></td><td><b>0 - 1</b></td>
          <td>Przemysław Banaszak 7 <img src="/img/goal.gif"></td></tr>
      <tr><td><img src="/img/own.gif"> Jehor Macenko 45+2 (s)</td>
          <td><b>1 - 1</b></td><td></td></tr>
      <tr><td><img src="/img/goal.gif"> Yvan Ikia Dimi 79</td>
          <td><b>2 - 1</b></td><td></td></tr>
      <tr><td>Player <img src="/img/yel.gif" alt="ŻK"></td><td></td>
          <td>Player <img src="/img/yel.gif" alt="ŻK"></td></tr>
      <tr><td>Player <img src="/img/red.gif" alt="CZ"></td><td></td><td>Player</td></tr>
    </table>
    """

    details = parse_ninety_minut_details(html, match)

    assert details == {
        "home_goals_first_half": 1,
        "away_goals_first_half": 1,
        "home_goals_second_half": 1,
        "away_goals_second_half": 0,
        "home_cards": 2,
        "away_cards": 1,
        "first_team_to_score": "Śląsk Wrocław",
        "first_goal_minute": "7",
    }


def test_parses_goalless_90minut_detail() -> None:
    match = parse_matches(
        page_with_rows("Kolejka 1", result_row("0-0")),
        COMPETITIONS["ekstraklasa"],
        current_source("ekstraklasa"),
    )[0]
    details = parse_ninety_minut_details(
        "<table><tr><td>A</td><td><nobr>0 - 0</nobr></td><td>B</td></tr></table>",
        match,
    )
    assert details["home_goals_first_half"] == 0
    assert details["away_goals_second_half"] == 0
    assert details["first_team_to_score"] is None
    assert details["first_goal_minute"] is None


def test_missing_90minut_goal_minute_is_non_fatal() -> None:
    match = parse_matches(
        page_with_rows("Kolejka 1", result_row("1-0")),
        COMPETITIONS["ekstraklasa"],
        current_source("ekstraklasa"),
    )[0]
    details = parse_ninety_minut_details(
        """
        <table>
          <tr><td>Górnik</td><td><nobr>1 - 0</nobr></td><td>Śląsk</td></tr>
          <tr><td><img src="/img/goal.gif"> Unknown scorer</td>
              <td><b>1 - 0</b></td><td></td></tr>
        </table>
        """,
        match,
    )
    assert details["first_team_to_score"] == "Górnik Zabrze"
    assert details["first_goal_minute"] is None
    assert details["home_goals_first_half"] is None
    assert details["away_goals_first_half"] is None
    assert details["home_goals_second_half"] is None
    assert details["away_goals_second_half"] is None
