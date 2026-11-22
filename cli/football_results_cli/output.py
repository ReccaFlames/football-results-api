"""Rendering: JSON for pipes and agents, Rich tables for people at a terminal.

JSON output is the API response body unchanged. Tables show `null` as an em dash, never as 0.
"""

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from rich.console import Console
from rich.table import Table

NULL = "—"
STALE_GRACE = timedelta(minutes=15)


def stdout_is_tty() -> bool:
    return sys.stdout.isatty()


def use_json(json_flag: bool) -> bool:
    return json_flag or not stdout_is_tty()


def print_json(body: Any) -> None:
    if stdout_is_tty():
        text = json.dumps(body, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def note(message: str) -> None:
    """A human hint on stderr (table mode only), so stdout stays pure data."""
    Console(file=sys.stderr, highlight=False, soft_wrap=True).print(message, markup=False)


# --- Values --------------------------------------------------------------------------------


def format_datetime(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def format_value(value: Any, field: str = "") -> str:
    if value is None:
        return NULL
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(format_value(item) for item in value) or NULL
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, str) and field.endswith("_at"):
        return format_datetime(value)
    return str(value)


# --- Tables --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    header: str
    value: Callable[[Mapping[str, Any]], str]
    numeric: bool = False


def field_column(field: str) -> Column:
    return Column(field, lambda item: format_value(item.get(field), field))


def score(home: Any, away: Any) -> str:
    return NULL if home is None or away is None else f"{home}–{away}"


def round_or_stage(item: Mapping[str, Any]) -> str:
    return str(item["round"]) if item.get("round") is not None else format_value(item.get("stage"))


MATCH_COLUMNS = (
    Column("ID", lambda item: str(item["id"]), numeric=True),
    Column("Kickoff (UTC)", lambda item: format_value(item.get("kickoff_at"), "kickoff_at")),
    Column("Competition", lambda item: format_value(item.get("competition"))),
    Column("Round/stage", round_or_stage),
    Column("Home", lambda item: format_value(item.get("home_team"))),
    Column("Score", lambda item: score(item.get("home_goals"), item.get("away_goals"))),
    Column("Away", lambda item: format_value(item.get("away_team"))),
    Column("Status", lambda item: format_value(item.get("status"))),
)
RESULT_COLUMNS = (
    Column("ID", lambda item: format_value(item.get("id")), numeric=True),
    Column("Kickoff (UTC)", lambda item: format_value(item.get("played_at"), "played_at")),
    Column("Round/stage", round_or_stage),
    Column("Home", lambda item: format_value(item.get("home_team"))),
    Column("Score", lambda item: score(item.get("home_score"), item.get("away_score"))),
    Column("Away", lambda item: format_value(item.get("away_team"))),
)
STANDING_COLUMNS = (
    Column("Pos", lambda item: str(item["position"]), numeric=True),
    Column("Team", lambda item: format_value(item.get("team_name"))),
    *(
        Column(header, lambda item, field=field: format_value(item.get(field)), numeric=True)
        for header, field in (
            ("P", "played"),
            ("W", "won"),
            ("D", "drawn"),
            ("L", "lost"),
            ("GF", "goals_for"),
            ("GA", "goals_against"),
            ("GD", "goal_difference"),
            ("Pts", "points"),
        )
    ),
    Column("Form", lambda item: format_value(item.get("form"))),
)
COMPETITION_COLUMNS = (
    Column("Slug", lambda item: item["slug"]),
    Column("Name", lambda item: format_value(item.get("name"))),
    Column("Country", lambda item: format_value(item.get("country"))),
    Column("Format", lambda item: format_value(item.get("format"))),
    Column("Current season", lambda item: format_value(item.get("current_season"))),
    Column("Stored seasons", lambda item: format_value(item.get("fetched_seasons"))),
    Column("Next check (UTC)", lambda item: format_value(item.get("next_check_at"), "_at")),
)
TEAM_COLUMNS = (
    Column("Slug", lambda item: item["slug"]),
    Column("Name", lambda item: format_value(item.get("name"))),
    Column("Aliases", lambda item: format_value(item.get("aliases"))),
)


def truncate(value: Any, width: int = 60) -> str:
    text = format_value(value)
    return text if len(text) <= width else text[: width - 1] + "…"


SCRAPE_RUN_COLUMNS = (
    Column("Job", lambda item: str(item["job_id"]), numeric=True),
    Column("Competition", lambda item: format_value(item.get("competition"))),
    Column("Season", lambda item: format_value(item.get("season"))),
    Column("Status", lambda item: format_value(item.get("status"))),
    Column("Queued (UTC)", lambda item: format_value(item.get("queued_at"), "_at")),
    Column("Completed (UTC)", lambda item: format_value(item.get("completed_at"), "_at")),
    Column("Matches", lambda item: format_value(item.get("matches_found")), numeric=True),
    Column("Skipped", lambda item: format_value(item.get("matches_skipped")), numeric=True),
    Column("Error", lambda item: truncate(item.get("error_message"))),
)
SEASON_SCHEDULE_COLUMNS = (
    Column("Competition", lambda item: format_value(item.get("competition"))),
    Column("Season", lambda item: format_value(item.get("season"))),
    Column("State", lambda item: format_value(item.get("scheduler_state"))),
    Column("Data as of (UTC)", lambda item: format_value(item.get("data_as_of"), "_at")),
    Column("Next update (UTC)", lambda item: format_value(item.get("next_update_at"), "_at")),
    Column("Overdue (s)", lambda item: f"{item.get('overdue_seconds') or 0:.0f}", numeric=True),
    Column("Stale", lambda item: format_value(item.get("stale"))),
    Column("Failures", lambda item: format_value(item.get("consecutive_failures")), numeric=True),
    Column("Job", lambda item: format_value(item.get("active_job_id")), numeric=True),
)
WORKER_COLUMNS = (
    Column("Worker", lambda item: format_value(item.get("worker_id"))),
    Column("Started (UTC)", lambda item: format_value(item.get("started_at"), "_at")),
    Column("Heartbeat (UTC)", lambda item: format_value(item.get("heartbeat_at"), "_at")),
    Column("Alive", lambda item: format_value(item.get("alive"))),
)


def selected_columns(
    defaults: Sequence[Column], fields: Sequence[str] | None, always: str
) -> Sequence[Column]:
    """The default columns, or exactly the requested fields (plus the always-included key)."""
    if not fields:
        return defaults
    names = [always, *(field for field in fields if field != always)]
    return [field_column(name) for name in names]


def print_table(
    items: Sequence[Mapping[str, Any]], columns: Sequence[Column], *, title: str | None = None
) -> None:
    table = Table(title=title, title_justify="left", header_style="bold")
    for column in columns:
        table.add_column(column.header, justify="right" if column.numeric else "left")
    for item in items:
        table.add_row(*(column.value(item) for column in columns))
    Console(file=sys.stdout, highlight=False).print(table)
    if not items:
        note("No items.")


def print_record(record: Mapping[str, Any], *, title: str | None = None) -> None:
    table = Table(title=title, title_justify="left", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    for key, value in record.items():
        table.add_row(key, format_value(value, key))
    Console(file=sys.stdout, highlight=False).print(table)


# --- Footer --------------------------------------------------------------------------------


def print_footer(body: Mapping[str, Any]) -> None:
    freshness = body.get("freshness") or []
    stale = stale_entries(freshness)
    for entry in freshness:
        note(
            f"{entry.get('competition')} {entry.get('season')}: data as of "
            f"{format_value(entry.get('data_as_of'), '_at')}, next update "
            f"{format_value(entry.get('next_update_at'), '_at')}"
            f" (source: {(entry.get('source') or {}).get('provider', NULL)})"
            + (" [stale]" if entry in stale else "")
        )
    cursor = (body.get("pagination") or {}).get("next_cursor")
    if cursor:
        note(f"More items: repeat the command with --cursor {cursor}")


# --- Staleness -----------------------------------------------------------------------------


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def stale_entries(
    freshness: Sequence[Mapping[str, Any]], now: datetime | None = None
) -> list[Mapping[str, Any]]:
    """Seasons never fetched, or whose scheduled update is more than ``STALE_GRACE`` overdue."""
    now = now or datetime.now(UTC)
    stale = []
    for entry in freshness:
        next_update = _parse(entry.get("next_update_at"))
        if entry.get("data_as_of") is None or (
            next_update is not None and next_update < now - STALE_GRACE
        ):
            stale.append(entry)
    return stale
