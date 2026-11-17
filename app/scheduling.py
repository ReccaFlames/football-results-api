"""Pure scheduling rules for the adaptive scheduler.

The thresholds come from ``SchedulePolicy``, built from ``defaults.schedule`` and the optional
per-competition ``schedule`` override in ``config/competitions.yaml``. ``SchedulePolicy()`` holds the
built-in defaults, which match the shipped configuration file.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from app.models import ScrapedMatch
from app.seasons import SeasonFormat, season_for_date, season_years

SeasonRollover = Literal["auto", "manual"]

DURATION_PATTERN = r"^[1-9]\d*[smhd]$"
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

# States in which the current season has no fixture that is upcoming, in progress, or awaiting a
# result. Only then may it be rolled over to the next season.
ROLLOVER_READY_STATES = frozenset({"waiting_for_schedule", "delayed"})


def parse_duration(value: str) -> timedelta:
    """Parse a duration such as ``30s``, ``15m``, ``4h`` or ``1d``."""
    if re.fullmatch(DURATION_PATTERN, value) is None:
        raise ValueError(f"invalid duration {value!r}; expected e.g. 30s, 15m, 4h or 1d")
    return timedelta(**{_DURATION_UNITS[value[-1]]: int(value[:-1])})


def _minutes(*values: int) -> tuple[timedelta, ...]:
    return tuple(timedelta(minutes=value) for value in values)


@dataclass(frozen=True)
class SchedulePolicy:
    pre_match_check: timedelta = timedelta(minutes=30)
    match_duration: dict[str, timedelta] = field(
        default_factory=lambda: {"league": timedelta(minutes=105), "cup": timedelta(minutes=135)}
    )
    # result_poll[i] applies while the oldest overdue result is late by at most
    # result_poll_until[i]; the last interval applies afterwards (state "delayed").
    result_poll: tuple[timedelta, ...] = _minutes(5, 15, 60)
    result_poll_until: tuple[timedelta, ...] = _minutes(30, 240)
    idle_interval: timedelta = timedelta(days=1)
    off_season_interval: timedelta = timedelta(days=7)
    off_season_after: timedelta = timedelta(days=30)
    failure_backoff: tuple[timedelta, ...] = _minutes(5, 15, 30, 60)
    season_rollover: SeasonRollover = "auto"
    season_check_interval: timedelta = timedelta(days=1)

    def __post_init__(self) -> None:
        if not self.result_poll or len(self.result_poll_until) != len(self.result_poll) - 1:
            raise ValueError("result_poll_until needs exactly one entry fewer than result_poll")
        if not self.failure_backoff:
            raise ValueError("failure_backoff needs at least one interval")

    def duration_for(self, competition_format: str) -> timedelta:
        """Expected match duration; unknown formats use the longest configured duration."""
        duration = self.match_duration.get(competition_format)
        if duration is not None:
            return duration
        return max(self.match_duration.values(), default=timedelta(minutes=135))


DEFAULT_POLICY = SchedulePolicy()


@dataclass(frozen=True)
class ScheduleDecision:
    state: str
    next_check_at: datetime


def calculate_next_check(
    matches: list[ScrapedMatch],
    competition_format: str,
    now: datetime | None = None,
    policy: SchedulePolicy = DEFAULT_POLICY,
) -> ScheduleDecision:
    return next_check_for_kickoffs(
        (match.played_at for match in matches if match.status == "scheduled"),
        competition_format,
        now,
        policy,
    )


def next_check_for_kickoffs(
    kickoffs: Iterable[datetime],
    competition_format: str,
    now: datetime | None = None,
    policy: SchedulePolicy = DEFAULT_POLICY,
) -> ScheduleDecision:
    """Decide the next check from the kickoff times of the season's scheduled fixtures."""
    current = now or datetime.now(UTC)
    expected_duration = policy.duration_for(competition_format)
    scheduled = sorted(kickoffs)

    overdue = [kickoff for kickoff in scheduled if kickoff + expected_duration <= current]
    if overdue:
        oldest_delay = current - (overdue[0] + expected_duration)
        for interval, until in zip(policy.result_poll, policy.result_poll_until, strict=False):
            if oldest_delay <= until:
                return ScheduleDecision("waiting_for_result", current + interval)
        return ScheduleDecision("delayed", current + policy.result_poll[-1])

    in_progress = [kickoff for kickoff in scheduled if kickoff <= current]
    if in_progress:
        return ScheduleDecision("match_in_progress", in_progress[0] + expected_duration)

    if scheduled:
        next_kickoff = scheduled[0]
        pre_match_check = next_kickoff - policy.pre_match_check
        distance = next_kickoff - current
        safety_interval = (
            policy.off_season_interval
            if distance > policy.off_season_after
            else policy.idle_interval
        )
        candidates = [current + safety_interval]
        if pre_match_check > current:
            candidates.append(pre_match_check)
        state = "pre_match" if distance <= timedelta(hours=24) else "waiting_for_match"
        return ScheduleDecision(state, min(candidates))

    return ScheduleDecision("waiting_for_schedule", current + policy.idle_interval)


def failure_backoff(
    consecutive_failures: int, policy: SchedulePolicy = DEFAULT_POLICY
) -> timedelta:
    index = min(max(consecutive_failures, 1), len(policy.failure_backoff)) - 1
    return policy.failure_backoff[index]


def rollover_target(
    current_season: str | None,
    known_seasons: Iterable[str],
    today: date,
    season_format: SeasonFormat,
    season_start_month: int,
    current_state: str,
) -> str | None:
    """Return the season to roll over to, or ``None`` to keep the current season.

    The target is the newest known season that has started by ``today`` and is newer than the
    current one. Rollover waits until the current season has no upcoming, in-progress, or
    pending-result fixtures (``current_state`` in ``ROLLOVER_READY_STATES``). A competition
    without a current season is never rolled over automatically.
    """
    if current_season is None or current_state not in ROLLOVER_READY_STATES:
        return None
    current_start = season_years(current_season)[0]
    today_start = season_years(season_for_date(today, season_format, season_start_month))[0]
    candidates = [
        season for season in known_seasons if current_start < season_years(season)[0] <= today_start
    ]
    return max(candidates, key=lambda season: season_years(season)[0], default=None)
