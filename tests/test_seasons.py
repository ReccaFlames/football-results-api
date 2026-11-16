from datetime import date

import pytest

from app.seasons import season_for_date, season_year_for_month, season_years, validate_season


def test_season_years_accept_calendar_and_consecutive_split_seasons() -> None:
    assert season_years("2026") == (2026, 2026)
    assert season_years("2026-2027") == (2026, 2027)
    with pytest.raises(ValueError, match="consecutive"):
        season_years("2026-2028")
    with pytest.raises(ValueError, match="expected YYYY"):
        season_years("26/27")


def test_validate_season_requires_the_competition_format() -> None:
    validate_season("2026", "calendar")
    with pytest.raises(ValueError, match="calendar format"):
        validate_season("2026-2027", "calendar")
    with pytest.raises(ValueError, match="split format"):
        validate_season("2026", "split")


@pytest.mark.parametrize(
    ("season", "month", "start_month", "year"),
    [
        ("2026-2027", 7, 7, 2026),
        ("2026-2027", 6, 7, 2027),
        ("2026-2027", 7, 8, 2027),
        ("2026-2027", 2, 2, 2026),
        ("2026", 12, 1, 2026),
    ],
)
def test_season_year_for_month_uses_the_start_month(
    season: str, month: int, start_month: int, year: int
) -> None:
    assert season_year_for_month(season, month, start_month) == year


def test_season_for_date_is_the_inverse_mapping() -> None:
    assert season_for_date(date(2026, 7, 31), "split", 8) == "2025-2026"
    assert season_for_date(date(2026, 8, 1), "split", 8) == "2026-2027"
    assert season_for_date(date(2026, 12, 6), "calendar", 1) == "2026"
