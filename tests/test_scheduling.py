from datetime import UTC, datetime, timedelta

from app.models import ScrapedMatch
from app.scheduling import calculate_next_check, failure_backoff

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
