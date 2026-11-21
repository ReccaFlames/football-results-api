import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from football_results_cli import output

from app.logs import JsonFormatter, configure_logging
from app.observability import (
    STALE_GRACE,
    SeasonState,
    is_stale,
    scheduler_state,
    stall_window,
)

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def state(fetched_at: datetime | None, next_check_at: datetime | None) -> SeasonState:
    return SeasonState(
        competition="ekstraklasa",
        season="2026-2027",
        scheduler_state="waiting_for_schedule",
        fetched_at=fetched_at,
        next_check_at=next_check_at,
        last_checked_at=fetched_at,
        consecutive_failures=0,
        active_job_id=None,
    )


def test_the_api_and_the_cli_share_the_stale_grace_period() -> None:
    assert STALE_GRACE == output.STALE_GRACE


def test_a_season_is_stale_once_its_update_is_overdue_by_more_than_the_grace_period() -> None:
    fetched = NOW - timedelta(days=1)
    assert not is_stale(state(fetched, NOW - STALE_GRACE), NOW)
    assert is_stale(state(fetched, NOW - STALE_GRACE - timedelta(seconds=1)), NOW)
    assert not is_stale(state(fetched, None), NOW)
    assert is_stale(state(None, NOW + timedelta(hours=1)), NOW)


def test_the_scheduler_stalls_without_a_heartbeat_inside_the_window() -> None:
    window = stall_window(30)
    assert window == timedelta(minutes=2)
    assert stall_window(60) == timedelta(minutes=4)
    assert scheduler_state(None, NOW, window) == "not_started"
    assert scheduler_state(NOW - window, NOW, window) == "ok"
    assert scheduler_state(NOW - window - timedelta(seconds=1), NOW, window) == "stalled"


def test_json_logs_carry_extras_access_fields_and_exceptions() -> None:
    formatter = JsonFormatter()
    record = logging.makeLogRecord(
        {"name": "app.scheduler", "levelname": "INFO", "msg": "Scraped %s", "args": ("x",)}
    )
    record.competition = "ekstraklasa"
    entry = json.loads(formatter.format(record))
    assert entry["message"] == "Scraped x"
    assert entry["logger"] == "app.scheduler"
    assert entry["competition"] == "ekstraklasa"

    access = logging.makeLogRecord(
        {
            "name": "uvicorn.access",
            "levelname": "INFO",
            "msg": '%s - "%s %s HTTP/%s" %d',
            "args": ("1.2.3.4:5", "GET", "/health", "1.1", 200),
        }
    )
    entry = json.loads(formatter.format(access))
    assert (entry["method"], entry["path"], entry["status"]) == ("GET", "/health", 200)

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        failed = logging.makeLogRecord(
            {"name": "app", "levelname": "ERROR", "msg": "failed", "exc_info": sys.exc_info()}
        )
    assert "RuntimeError: boom" in json.loads(formatter.format(failed))["exception"]


def test_logging_replaces_console_handlers_but_keeps_others() -> None:
    root = logging.getLogger()
    saved = list(root.handlers), root.level
    console = logging.StreamHandler(sys.stderr)
    kept = logging.NullHandler()
    root.handlers = [console, kept]
    try:
        configure_logging("json")
        configure_logging("json")
        [own] = [handler for handler in root.handlers if handler not in (console, kept)]
        assert console not in root.handlers and kept in root.handlers
        assert isinstance(own.formatter, JsonFormatter)
    finally:
        root.handlers, level = saved
        root.setLevel(level)
