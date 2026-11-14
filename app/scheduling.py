from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.models import ScrapedMatch


@dataclass(frozen=True)
class ScheduleDecision:
    state: str
    next_check_at: datetime


def calculate_next_check(
    matches: list[ScrapedMatch],
    competition_format: str,
    now: datetime | None = None,
) -> ScheduleDecision:
    current = now or datetime.now(UTC)
    expected_duration = timedelta(minutes=135 if competition_format == "cup" else 105)
    scheduled = sorted(
        (match for match in matches if match.status == "scheduled"),
        key=lambda match: match.played_at,
    )

    overdue = [match for match in scheduled if match.played_at + expected_duration <= current]
    if overdue:
        oldest_delay = current - min(match.played_at + expected_duration for match in overdue)
        if oldest_delay <= timedelta(minutes=30):
            return ScheduleDecision("waiting_for_result", current + timedelta(minutes=5))
        if oldest_delay <= timedelta(hours=4):
            return ScheduleDecision("waiting_for_result", current + timedelta(minutes=15))
        return ScheduleDecision("delayed", current + timedelta(hours=1))

    in_progress = [
        match
        for match in scheduled
        if match.played_at <= current < match.played_at + expected_duration
    ]
    if in_progress:
        expected_end = min(match.played_at + expected_duration for match in in_progress)
        return ScheduleDecision("match_in_progress", expected_end)

    future = [match for match in scheduled if match.played_at > current]
    if future:
        next_kickoff = future[0].played_at
        pre_match_check = next_kickoff - timedelta(minutes=30)
        distance = next_kickoff - current
        safety_interval = timedelta(days=7) if distance > timedelta(days=30) else timedelta(days=1)
        candidates = [current + safety_interval]
        if pre_match_check > current:
            candidates.append(pre_match_check)
        state = "pre_match" if distance <= timedelta(hours=24) else "waiting_for_match"
        return ScheduleDecision(state, min(candidates))

    return ScheduleDecision("waiting_for_schedule", current + timedelta(days=1))


def failure_backoff(consecutive_failures: int) -> timedelta:
    minutes = (5, 15, 30, 60)
    index = min(max(consecutive_failures, 1), len(minutes)) - 1
    return timedelta(minutes=minutes[index])