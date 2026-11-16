import re
from urllib.parse import parse_qs, urlsplit

from app.models import Competition, CompetitionSummary, SeasonSource

NINETY_MINUT_PATH_RE = re.compile(r"^/liga/1/liga\d+\.html$")
FOOTBALL_DATA_PATH = "/v4/competitions/PL/matches"


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
        current_season=competition.current_season,
        available_seasons=sorted(set(competition.seasons) | fetched, reverse=True),
        fetched_seasons=sorted(fetched, reverse=True),
    )


def source_from_url(season: str, source_url: str) -> SeasonSource:
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
        and parsed.hostname == "api.football-data.org"
        and parsed.path == FOOTBALL_DATA_PATH
        and query == {"season": [season.split("-", maxsplit=1)[0]]}
    )
    if common_invalid or not (is_90minut or is_football_data):
        raise ValueError(
            "source_url must be a supported 90minut page or football-data.org PL endpoint"
        )
    return SeasonSource(
        season=season,
        source_url=source_url,
        provider="football-data" if is_football_data else "90minut",
    )
