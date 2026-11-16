"""Season identifiers and the mapping between seasons and calendar dates.

Two formats are supported, selected per competition:

- ``split``: ``YYYY-YYYY`` with consecutive years (``2026-2027``). The season starts in
  ``season_start_month`` of the first year; earlier months belong to the second year.
- ``calendar``: ``YYYY`` (``2026``). The season is the calendar year, so it starts in January.
"""

import re
from datetime import date
from typing import Literal

SeasonFormat = Literal["split", "calendar"]

SEASON_PATTERN = r"^\d{4}(?:-\d{4})?$"
DEFAULT_SPLIT_START_MONTH = 7
_SEASON_RE = re.compile(r"(\d{4})(?:-(\d{4}))?")


def season_years(season: str) -> tuple[int, int]:
    """Return the first and last calendar year covered by ``season``."""
    match = _SEASON_RE.fullmatch(season)
    if match is None:
        raise ValueError(f"invalid season {season!r}; expected YYYY or YYYY-YYYY")
    start = int(match.group(1))
    if match.group(2) is None:
        return start, start
    end = int(match.group(2))
    if end != start + 1:
        raise ValueError(f"invalid season {season!r}; split seasons must span consecutive years")
    return start, end


def season_format_of(season: str) -> SeasonFormat:
    start, end = season_years(season)
    return "calendar" if start == end else "split"


def validate_season(season: str, season_format: SeasonFormat) -> None:
    if season_format_of(season) != season_format:
        expected = "YYYY" if season_format == "calendar" else "YYYY-YYYY"
        raise ValueError(f"season {season!r} does not match the {season_format} format {expected}")


def default_start_month(season_format: SeasonFormat) -> int:
    return 1 if season_format == "calendar" else DEFAULT_SPLIT_START_MONTH


def season_year_for_month(season: str, month: int, start_month: int) -> int:
    """Return the calendar year in which ``month`` of ``season`` falls."""
    start, end = season_years(season)
    return start if month >= start_month else end


def season_for_date(value: date, season_format: SeasonFormat, start_month: int) -> str:
    """Return the season identifier that contains ``value``."""
    if season_format == "calendar":
        return str(value.year)
    start = value.year if value.month >= start_month else value.year - 1
    return f"{start}-{start + 1}"
