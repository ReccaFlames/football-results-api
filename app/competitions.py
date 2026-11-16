import re
from urllib.parse import parse_qs, urlsplit

from app.models import Competition, CompetitionSummary, SeasonSource
from app.seasons import season_years

NINETY_MINUT_PATH_RE = re.compile(r"^/liga/1/liga\d+\.html$")
FOOTBALL_DATA_HOST = "api.football-data.org"
FOOTBALL_DATA_CODE_RE = re.compile(r"[A-Z0-9]{2,10}")
FOOTBALL_DATA_PATH_RE = re.compile(r"^/v4/competitions/([A-Z0-9]{2,10})/matches$")


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


def provider_competition_code(source: SeasonSource) -> str | None:
    """Return the provider's own competition identifier when the source URL contains one.

    90minut uses a different page ID for each season, so it has no stable code.
    """
    if source.provider != "football-data":
        return None
    match = FOOTBALL_DATA_PATH_RE.fullmatch(urlsplit(source.source_url).path)
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
    if common_invalid or not (is_90minut or is_football_data):
        raise ValueError(
            "source_url must be a supported 90minut page or football-data.org "
            "/v4/competitions/<CODE>/matches?season=<start year> endpoint"
        )
    return SeasonSource(
        season=season,
        source_url=source_url,
        provider="football-data" if is_football_data else "90minut",
    )
