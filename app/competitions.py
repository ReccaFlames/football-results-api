import re
from urllib.parse import parse_qs, urlsplit

from app.models import Competition, CompetitionSummary, SeasonSource
from app.seasons import season_years

NINETY_MINUT_PATH_RE = re.compile(r"^/liga/1/liga\d+\.html$")
FOOTBALL_DATA_HOST = "api.football-data.org"
FOOTBALL_DATA_CODE_RE = re.compile(r"[A-Z0-9]{2,10}")
FOOTBALL_DATA_PATH_RE = re.compile(r"^/v4/competitions/([A-Z0-9]{2,10})/matches$")
ESPN_HOST = "site.api.espn.com"
ESPN_LEAGUE_PATTERN = r"[a-z]{2,10}\.[a-z0-9_]{1,30}"
ESPN_LEAGUE_RE = re.compile(ESPN_LEAGUE_PATTERN)
ESPN_PATH_RE = re.compile(rf"^/apis/site/v2/sports/soccer/({ESPN_LEAGUE_PATTERN})/scoreboard$")
THESPORTSDB_HOST = "www.thesportsdb.com"
THESPORTSDB_LEAGUE_RE = re.compile(r"[1-9][0-9]{0,9}")
THESPORTSDB_PATH_RE = re.compile(r"^/league/([1-9][0-9]{0,9})$")
PROVIDER_CODE_PATHS = {
    "football-data": FOOTBALL_DATA_PATH_RE,
    "espn": ESPN_PATH_RE,
    "thesportsdb": THESPORTSDB_PATH_RE,
}


def competition_summary(
    competition: Competition,
    fetched_seasons: set[str] | None = None,
) -> CompetitionSummary:
    fetched = fetched_seasons or set()
    return CompetitionSummary(
        slug=competition.slug,
        name=competition.name,
        country=competition.country,
        format=competition.format,
        season_format=competition.season_format,
        season_start_month=competition.season_start_month,
        current_season=competition.current_season,
        available_seasons=sorted(set(competition.seasons) | fetched, reverse=True),
        fetched_seasons=sorted(fetched, reverse=True),
    )


def football_data_source_url(code: str, season: str) -> str:
    if FOOTBALL_DATA_CODE_RE.fullmatch(code) is None:
        raise ValueError(f"invalid football-data.org competition code {code!r}")
    start_year, _ = season_years(season)
    return f"https://{FOOTBALL_DATA_HOST}/v4/competitions/{code}/matches?season={start_year}"


def espn_source_url(league: str, season: str) -> str:
    if ESPN_LEAGUE_RE.fullmatch(league) is None:
        raise ValueError(f"invalid ESPN league {league!r}")
    start_year, _ = season_years(season)
    return f"https://{ESPN_HOST}/apis/site/v2/sports/soccer/{league}/scoreboard?season={start_year}"


def thesportsdb_source_url(league: str, season: str) -> str:
    """Key-free TheSportsDB season source; the API key is added only when requesting."""
    if THESPORTSDB_LEAGUE_RE.fullmatch(league) is None:
        raise ValueError(f"invalid TheSportsDB league id {league!r}")
    season_years(season)
    return f"https://{THESPORTSDB_HOST}/league/{league}?season={season}"


def provider_competition_code(source: SeasonSource) -> str | None:
    """Return the provider's own competition identifier when the source URL contains one.

    That is the football-data.org code (``PL``), the ESPN league (``eng.1``), or the TheSportsDB
    league id (``4422``). 90minut uses a
    different page ID for each season, so it has no stable code.
    """
    pattern = PROVIDER_CODE_PATHS.get(source.provider)
    if pattern is None:
        return None
    match = pattern.fullmatch(urlsplit(source.source_url).path)
    return None if match is None else match.group(1)


def source_from_url(season: str, source_url: str) -> SeasonSource:
    start_year, _ = season_years(season)
    parsed = urlsplit(source_url)
    common_invalid = (
        parsed.scheme not in {"http", "https"}
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    )
    is_90minut = (
        parsed.hostname in {"90minut.pl", "www.90minut.pl"}
        and NINETY_MINUT_PATH_RE.fullmatch(parsed.path) is not None
        and not parsed.query
    )
    query = parse_qs(parsed.query, keep_blank_values=True)
    is_football_data = (
        parsed.scheme == "https"
        and parsed.hostname == FOOTBALL_DATA_HOST
        and FOOTBALL_DATA_PATH_RE.fullmatch(parsed.path) is not None
        and query == {"season": [str(start_year)]}
    )
    is_espn = (
        parsed.scheme == "https"
        and parsed.hostname == ESPN_HOST
        and ESPN_PATH_RE.fullmatch(parsed.path) is not None
        and query == {"season": [str(start_year)]}
    )
    is_thesportsdb = (
        parsed.scheme == "https"
        and parsed.hostname == THESPORTSDB_HOST
        and THESPORTSDB_PATH_RE.fullmatch(parsed.path) is not None
        and query == {"season": [season]}
    )
    if common_invalid or not (is_90minut or is_football_data or is_espn or is_thesportsdb):
        raise ValueError(
            "source_url must be a supported 90minut page, a football-data.org "
            "/v4/competitions/<CODE>/matches?season=<start year> endpoint, an ESPN "
            "/apis/site/v2/sports/soccer/<league>/scoreboard?season=<start year> endpoint, or a "
            "TheSportsDB https://www.thesportsdb.com/league/<id>?season=<season> page"
        )
    provider = (
        "football-data"
        if is_football_data
        else "espn"
        if is_espn
        else "thesportsdb"
        if is_thesportsdb
        else "90minut"
    )
    return SeasonSource(season=season, source_url=source_url, provider=provider)
