from datetime import UTC, date, datetime, timedelta

import pytest

from app.models import ScrapedMatch
from app.scheduling import (
    SchedulePolicy,
    calculate_next_check,
    failure_backoff,
    parse_duration,
    rollover_target,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


def fixture(kickoff: datetime) -> ScrapedMatch:
    return ScrapedMatch(
        competition="ekstraklasa",
        season="2026-2027",
        stage="Kolejka 7",
        round=7,
        home_team="A",
        away_team="B",
        status="scheduled",
        played_at=kickoff,
        source_url="http://www.90minut.pl/liga/1/liga14675.html",
    )


def test_no_schedule_checks_daily() -> None:
    decision = calculate_next_check([], "league", NOW)
    assert decision.state == "waiting_for_schedule"
    assert decision.next_check_at == NOW + timedelta(days=1)


def test_future_match_checks_before_kickoff() -> None:
    decision = calculate_next_check([fixture(NOW + timedelta(hours=6))], "league", NOW)
    assert decision.state == "pre_match"
    assert decision.next_check_at == NOW + timedelta(hours=5, minutes=30)


def test_in_progress_waits_for_expected_end() -> None:
    kickoff = NOW - timedelta(minutes=30)
    decision = calculate_next_check([fixture(kickoff)], "league", NOW)
    assert decision.state == "match_in_progress"
    assert decision.next_check_at == kickoff + timedelta(minutes=105)


def test_recently_overdue_result_is_polled_quickly() -> None:
    kickoff = NOW - timedelta(minutes=110)
    decision = calculate_next_check([fixture(kickoff)], "league", NOW)
    assert decision.state == "waiting_for_result"
    assert decision.next_check_at == NOW + timedelta(minutes=5)


def test_failure_backoff_is_bounded() -> None:
    assert failure_backoff(1) == timedelta(minutes=5)
    assert failure_backoff(2) == timedelta(minutes=15)
    assert failure_backoff(99) == timedelta(minutes=60)


CUSTOM = SchedulePolicy(
    pre_match_check=timedelta(minutes=60),
    match_duration={"league": timedelta(minutes=100)},
    result_poll=(timedelta(minutes=2), timedelta(minutes=30)),
    result_poll_until=(timedelta(minutes=10),),
    idle_interval=timedelta(hours=6),
    off_season_interval=timedelta(days=3),
    off_season_after=timedelta(days=10),
    failure_backoff=(timedelta(minutes=1), timedelta(minutes=9)),
)


def test_default_policy_matches_the_previous_thresholds() -> None:
    far = calculate_next_check([fixture(NOW + timedelta(days=40))], "league", NOW)
    cup_kickoff = NOW - timedelta(minutes=130)
    in_cup = calculate_next_check([fixture(cup_kickoff)], "cup", NOW)
    late = calculate_next_check([fixture(NOW - timedelta(hours=3))], "league", NOW)
    very_late = calculate_next_check([fixture(NOW - timedelta(hours=7))], "league", NOW)

    assert far.next_check_at == NOW + timedelta(days=7)
    assert in_cup.next_check_at == cup_kickoff + timedelta(minutes=135)
    assert late.next_check_at == NOW + timedelta(minutes=15)
    assert (very_late.state, very_late.next_check_at) == ("delayed", NOW + timedelta(hours=1))


def test_custom_policy_drives_every_threshold() -> None:
    pre_match = calculate_next_check([fixture(NOW + timedelta(hours=3))], "league", NOW, CUSTOM)
    kickoff = NOW - timedelta(minutes=50)
    in_progress = calculate_next_check([fixture(kickoff)], "league", NOW, CUSTOM)
    soon_late = calculate_next_check([fixture(NOW - timedelta(minutes=105))], "league", NOW, CUSTOM)
    delayed = calculate_next_check([fixture(NOW - timedelta(minutes=120))], "league", NOW, CUSTOM)
    off_season = calculate_next_check([fixture(NOW + timedelta(days=11))], "league", NOW, CUSTOM)
    idle = calculate_next_check([], "league", NOW, CUSTOM)

    assert pre_match.next_check_at == NOW + timedelta(hours=2)
    assert in_progress.next_check_at == kickoff + timedelta(minutes=100)
    assert soon_late.next_check_at == NOW + timedelta(minutes=2)
    assert (delayed.state, delayed.next_check_at) == ("delayed", NOW + timedelta(minutes=30))
    assert off_season.next_check_at == NOW + timedelta(days=3)
    assert idle.next_check_at == NOW + timedelta(hours=6)
    assert failure_backoff(5, CUSTOM) == timedelta(minutes=9)


def test_unknown_format_uses_the_longest_match_duration() -> None:
    assert SchedulePolicy().duration_for("playoff") == timedelta(minutes=135)


def test_policy_rejects_mismatched_poll_limits() -> None:
    with pytest.raises(ValueError, match="one entry fewer"):
        SchedulePolicy(
            result_poll=(timedelta(minutes=5),), result_poll_until=(timedelta(minutes=30),)
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("45s", timedelta(seconds=45)), ("15m", timedelta(minutes=15)), ("2d", timedelta(days=2))],
)
def test_parse_duration(value: str, expected: timedelta) -> None:
    assert parse_duration(value) == expected


def test_parse_duration_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration("0d")


SPLIT_SEASONS = ["2025-2026", "2026-2027", "2027-2028"]


@pytest.mark.parametrize(
    ("today", "state", "expected"),
    [
        (date(2027, 6, 30), "waiting_for_schedule", None),  # next season has not started
        (date(2027, 7, 1), "waiting_for_schedule", "2027-2028"),
        (date(2027, 7, 1), "delayed", "2027-2028"),
        (date(2027, 7, 1), "pre_match", None),  # current season still has fixtures
        (date(2027, 7, 1), "waiting_for_result", None),
        (date(2029, 8, 1), "waiting_for_schedule", "2027-2028"),  # newest known season
    ],
)
def test_split_season_rollover(today: date, state: str, expected: str | None) -> None:
    assert rollover_target("2026-2027", SPLIT_SEASONS, today, "split", 7, state) == expected


def test_calendar_season_rollover_and_missing_current() -> None:
    seasons = ["2026", "2027"]
    assert rollover_target("2026", seasons, date(2027, 1, 1), "calendar", 1, "delayed") == "2027"
    assert rollover_target("2026", seasons, date(2026, 12, 31), "calendar", 1, "delayed") is None
    assert rollover_target(None, seasons, date(2027, 3, 1), "calendar", 1, "delayed") is None
