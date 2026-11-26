"""Operational state for admin endpoints and Prometheus metrics.

Pure rules (stale season, stalled scheduler) and metric rendering. The API's `/metrics` reads the
scheduler's state from PostgreSQL, so the scheduler process needs no HTTP server.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.metadata import version
from typing import Literal

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    ProcessCollector,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.metrics_core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from app.models import SchedulerWorker, SeasonSchedule

# A season whose scheduled update is later than this is a stale season. The CLI's
# `--fail-if-stale` uses the same grace period (a test checks).
STALE_GRACE = timedelta(minutes=15)

SchedulerState = Literal["ok", "stalled", "not_started"]


@dataclass(frozen=True)
class SeasonState:
    """Scheduler state of a current season with automatic refresh."""

    competition: str
    season: str
    scheduler_state: str
    fetched_at: datetime | None
    next_check_at: datetime | None
    last_checked_at: datetime | None
    consecutive_failures: int
    active_job_id: int | None
    primary_consecutive_failures: int = 0
    # The last successful import came from the fallback source.
    fallback_active: bool = False


@dataclass(frozen=True)
class WorkerHeartbeat:
    worker_id: str
    started_at: datetime
    heartbeat_at: datetime


@dataclass(frozen=True)
class ScrapeRunCount:
    competition: str
    status: str
    count: int


@dataclass(frozen=True)
class Snapshot:
    """Everything `/metrics` reports from the database, read at scrape time."""

    latest_heartbeat_at: datetime | None
    seasons: Sequence[SeasonState]
    run_counts: Sequence[ScrapeRunCount]


def stall_window(loop_seconds: float) -> timedelta:
    """How long the scheduler may go without a heartbeat before it counts as stalled."""
    return timedelta(seconds=max(loop_seconds * 4, 120))


def scheduler_state(
    latest_heartbeat_at: datetime | None, now: datetime, window: timedelta
) -> SchedulerState:
    if latest_heartbeat_at is None:
        return "not_started"
    return "ok" if now - latest_heartbeat_at <= window else "stalled"


def overdue_seconds(next_check_at: datetime | None, now: datetime) -> float:
    if next_check_at is None:
        return 0.0
    return max((now - next_check_at).total_seconds(), 0.0)


def is_stale(state: SeasonState, now: datetime) -> bool:
    """Never fetched, or the scheduled update is more than ``STALE_GRACE`` overdue."""
    return (
        state.fetched_at is None
        or overdue_seconds(state.next_check_at, now) > STALE_GRACE.total_seconds()
    )


def season_schedule(state: SeasonState, now: datetime) -> SeasonSchedule:
    return SeasonSchedule(
        competition=state.competition,
        season=state.season,
        scheduler_state=state.scheduler_state,
        data_as_of=state.fetched_at,
        next_update_at=state.next_check_at,
        last_checked_at=state.last_checked_at,
        overdue_seconds=overdue_seconds(state.next_check_at, now),
        stale=is_stale(state, now),
        consecutive_failures=state.consecutive_failures,
        primary_consecutive_failures=state.primary_consecutive_failures,
        fallback_active=state.fallback_active,
        active_job_id=state.active_job_id,
    )


def scheduler_worker(
    heartbeat: WorkerHeartbeat, now: datetime, window: timedelta
) -> SchedulerWorker:
    return SchedulerWorker(
        worker_id=heartbeat.worker_id,
        started_at=heartbeat.started_at,
        heartbeat_at=heartbeat.heartbeat_at,
        alive=now - heartbeat.heartbeat_at <= window,
    )


# --- Prometheus ------------------------------------------------------------------------------

PREFIX = "football_results"

# Per-process HTTP metrics of the API. Labels use the operation ID or route template, never the
# raw path, so the number of series stays bounded.
disable_created_metrics()
HTTP_REGISTRY = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=HTTP_REGISTRY)
HTTP_REQUESTS = Counter(
    f"{PREFIX}_http_requests",
    "HTTP requests handled by the API",
    ("operation", "method", "status"),
    registry=HTTP_REGISTRY,
)
HTTP_DURATION = Histogram(
    f"{PREFIX}_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("operation",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=HTTP_REGISTRY,
)


# Label `key` is a keys-file name, `env`, or `anonymous`: bounded by the operator's keys file.
KEYED_REQUESTS = Counter(
    f"{PREFIX}_keyed_requests",
    "Rate-limited requests by API key name and quota policy",
    ("key", "policy"),
    registry=HTTP_REGISTRY,
)
RATE_LIMITED = Counter(
    f"{PREFIX}_rate_limited",
    "Requests rejected with 429 by API key name and quota policy",
    ("key", "policy"),
    registry=HTTP_REGISTRY,
)


def record_quota_use(key: str, policy: str, allowed: bool) -> None:
    KEYED_REQUESTS.labels(key, policy).inc()
    if not allowed:
        RATE_LIMITED.labels(key, policy).inc()


AccessRequestOutcome = Literal["notified", "not_configured", "failed"]
ACCESS_REQUEST_OUTCOMES: tuple[AccessRequestOutcome, ...] = (
    "notified",
    "not_configured",
    "failed",
)
ACCESS_REQUESTS = Counter(
    f"{PREFIX}_access_requests",
    "Created access requests by outcome of the operator notification",
    ("outcome",),
    registry=HTTP_REGISTRY,
)
for _outcome in ACCESS_REQUEST_OUTCOMES:
    ACCESS_REQUESTS.labels(_outcome)


def record_access_request(outcome: AccessRequestOutcome) -> None:
    ACCESS_REQUESTS.labels(outcome).inc()


def record_request(operation: str, method: str, status: int, seconds: float) -> None:
    HTTP_REQUESTS.labels(operation, method, str(status)).inc()
    HTTP_DURATION.labels(operation).observe(seconds)


class _SnapshotCollector(Collector):
    def __init__(
        self,
        snapshot: Snapshot,
        now: datetime,
        window: timedelta,
        key_expiries: Sequence[tuple[str, datetime]],
    ) -> None:
        self.snapshot = snapshot
        self.now = now
        self.window = window
        self.key_expiries = key_expiries

    def collect(self) -> Iterator[Metric]:
        snapshot, now = self.snapshot, self.now
        build = GaugeMetricFamily(
            f"{PREFIX}_build_info", "Version of the running API", labels=("version",)
        )
        build.add_metric((version("football-results-api"),), 1)
        yield build

        state = scheduler_state(snapshot.latest_heartbeat_at, now, self.window)
        yield GaugeMetricFamily(
            f"{PREFIX}_scheduler_stalled",
            "1 when no scheduler heartbeat is within the stall window (stalled or never started)",
            value=0 if state == "ok" else 1,
        )
        if snapshot.latest_heartbeat_at is not None:
            yield GaugeMetricFamily(
                f"{PREFIX}_scheduler_heartbeat_age_seconds",
                "Seconds since the latest scheduler heartbeat",
                value=(now - snapshot.latest_heartbeat_at).total_seconds(),
            )

        labels = ("competition", "season")
        stale = GaugeMetricFamily(
            f"{PREFIX}_season_stale",
            "1 when a current season was never fetched or its update is more than the grace "
            "period overdue",
            labels=labels,
        )
        overdue = GaugeMetricFamily(
            f"{PREFIX}_season_overdue_seconds",
            "Seconds the scheduled update of a current season is overdue (0 when not due)",
            labels=labels,
        )
        failures = GaugeMetricFamily(
            f"{PREFIX}_season_consecutive_failures",
            "Failed imports in a row of a current season",
            labels=labels,
        )
        primary_failures = GaugeMetricFamily(
            f"{PREFIX}_season_primary_consecutive_failures",
            "Primary source failures in a row of a current season (a fallback success does not "
            "reset it)",
            labels=labels,
        )
        fallback_active = GaugeMetricFamily(
            f"{PREFIX}_season_fallback_active",
            "1 when the last successful import of a current season came from its fallback source",
            labels=labels,
        )
        data_age = GaugeMetricFamily(
            f"{PREFIX}_season_data_age_seconds",
            "Seconds since the last successful import of a current season (absent if never)",
            labels=labels,
        )
        next_update = GaugeMetricFamily(
            f"{PREFIX}_season_next_update_timestamp_seconds",
            "Unix time of the next scheduled update of a current season",
            labels=labels,
        )
        for season in snapshot.seasons:
            key = (season.competition, season.season)
            stale.add_metric(key, 1 if is_stale(season, now) else 0)
            overdue.add_metric(key, overdue_seconds(season.next_check_at, now))
            failures.add_metric(key, season.consecutive_failures)
            primary_failures.add_metric(key, season.primary_consecutive_failures)
            fallback_active.add_metric(key, 1 if season.fallback_active else 0)
            if season.fetched_at is not None:
                data_age.add_metric(key, (now - season.fetched_at).total_seconds())
            if season.next_check_at is not None:
                next_update.add_metric(key, season.next_check_at.timestamp())
        yield from (stale, overdue, failures, primary_failures, fallback_active, data_age)
        yield next_update

        runs = GaugeMetricFamily(
            f"{PREFIX}_scrape_runs",
            "Stored import jobs by competition and status",
            labels=("competition", "status"),
        )
        for item in snapshot.run_counts:
            runs.add_metric((item.competition, item.status), item.count)
        yield runs

        expiry = GaugeMetricFamily(
            f"{PREFIX}_api_key_expires_in_seconds",
            "Seconds until a keys-file API key expires (negative once expired), by key name",
            labels=("key",),
        )
        for name, expires_at in self.key_expiries:
            expiry.add_metric((name,), (expires_at - self.now).total_seconds())
        yield expiry


def render_metrics(
    snapshot: Snapshot,
    now: datetime,
    window: timedelta,
    key_expiries: Sequence[tuple[str, datetime]] = (),
) -> bytes:
    """Prometheus text exposition of the database snapshot, API key expiry (by key name), and
    this process's HTTP metrics."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_SnapshotCollector(snapshot, now, window, key_expiries))
    return generate_latest(registry) + generate_latest(HTTP_REGISTRY)
