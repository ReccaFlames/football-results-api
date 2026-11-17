"""OpenAPI metadata: tags, shared query parameters, and documented error responses.

Operation IDs are snake_case and part of the public contract: the CLI, skill, llms.txt, and MCP
surfaces are derived from them, so never rename one without a deprecation.
"""

from typing import Annotated, Any

from fastapi import Query
from pydantic import BaseModel

from app.contract import MAX_LIMIT
from app.models import MessageError, ParameterError, RequestValidationErrorResponse
from app.seasons import SEASON_PATTERN

API_DESCRIPTION = """\
Football fixtures, results, standings, and teams imported from external sources and refreshed
automatically around kickoff times.

Start with `list_competitions` to find competition slugs and seasons, then use `list_matches` for
fixtures and results, `get_standings` for league tables, and `list_teams` to resolve team slugs.
List responses are cursor-paginated and report per-season `freshness`. Unknown values are `null`,
never `0`. Write operations require an `X-API-Key` header.
"""

TAG_COMPETITIONS = "Competitions"
TAG_MATCHES = "Matches"
TAG_STANDINGS = "Standings"
TAG_REFRESH = "Data refresh"
TAG_SYSTEM = "System"

OPENAPI_TAGS = [
    {
        "name": TAG_COMPETITIONS,
        "description": (
            "Discover what data exists: competition slugs, seasons, and canonical team slugs. "
            "Use these first to build valid filters for the other endpoints."
        ),
    },
    {
        "name": TAG_MATCHES,
        "description": (
            "Fixtures and results. Use `list_matches` for any match question (results, upcoming "
            "fixtures, a team's form); `list_results` is kept only for existing clients."
        ),
    },
    {
        "name": TAG_STANDINGS,
        "description": "League tables computed from stored finished matches.",
    },
    {
        "name": TAG_REFRESH,
        "description": (
            "Queue imports from the upstream source and track them. Requires `X-API-Key`. "
            "Use only when data is missing or stale; the scheduler refreshes current seasons "
            "automatically."
        ),
    },
    {"name": TAG_SYSTEM, "description": "Service health for monitoring."},
]

CompetitionFilter = Annotated[
    str | None,
    Query(
        description="Competition slug from `list_competitions`; omit for all competitions",
        examples=["ekstraklasa"],
    ),
]
SeasonFilter = Annotated[
    str | None,
    Query(
        pattern=SEASON_PATTERN,
        description="Season in the competition's format: `2026-2027` (split) or `2026` (calendar)",
        examples=["2026-2027"],
    ),
]
RoundFilter = Annotated[
    int | None,
    Query(alias="round", ge=1, description="League round number", examples=[3]),
]
StageFilter = Annotated[
    str | None,
    Query(
        description="Case-insensitive substring of the stage name (cup rounds, groups)",
        examples=["Group A"],
    ),
]
TeamFilter = Annotated[
    str | None,
    Query(
        description=(
            "Canonical team slug (exactly that team), otherwise a case-insensitive substring of "
            "any team name or slug"
        ),
        examples=["legia-warszawa"],
    ),
]
Limit = Annotated[
    int,
    Query(ge=1, le=MAX_LIMIT, description=f"Page size (maximum {MAX_LIMIT})"),
]
Cursor = Annotated[
    str | None,
    Query(
        description=(
            "Opaque `pagination.next_cursor` from the previous page; repeat the same filters"
        ),
    ),
]


def example(summary: str, detail: Any) -> dict[str, Any]:
    return {"summary": summary, "value": {"detail": detail}}


def error(
    description: str, model: type[BaseModel] | Any, **examples: dict[str, Any]
) -> dict[str, Any]:
    """One entry of a route's ``responses=``; ``model`` may be a union of error models."""
    response: dict[str, Any] = {"model": model, "description": description}
    if examples:
        response["content"] = {"application/json": {"examples": examples}}
    return response


VALIDATION_EXAMPLE = example(
    "Request validation failed",
    [
        {
            "loc": ["query", "limit"],
            "msg": f"Input should be less than or equal to {MAX_LIMIT}",
            "type": "less_than_equal",
        }
    ],
)
UNKNOWN_COMPETITION_EXAMPLE = example("Unknown competition", "Unknown competition: la-liga")
UNKNOWN_SEASON_EXAMPLE = example(
    "Unknown season",
    {
        "message": "Unknown season 1999-2000 for ekstraklasa",
        "parameter": "season",
        "valid_values": ["2026-2027", "2025-2026"],
    },
)
INVALID_CURSOR_EXAMPLE = example(
    "Malformed cursor",
    {
        "message": "Invalid cursor",
        "parameter": "cursor",
        "hint": "Pass next_cursor from a previous response unchanged, or omit cursor.",
    },
)
MISMATCHED_CURSOR_EXAMPLE = example(
    "Cursor from other filters",
    {
        "message": "The cursor belongs to a request with different filters or order",
        "parameter": "cursor",
        "hint": "Repeat the original filters with this cursor, or omit cursor to restart.",
    },
)


def unknown_fields_example(valid_values: list[str]) -> dict[str, Any]:
    return example(
        "Unknown fields",
        {
            "message": "Unknown fields requested",
            "parameter": "fields",
            "invalid": ["score"],
            "valid_values": valid_values,
        },
    )


INVALID_CURSOR = error(
    "Malformed cursor, or a cursor used with other filters or order",
    ParameterError,
    invalid_cursor=INVALID_CURSOR_EXAMPLE,
    mismatched_cursor=MISMATCHED_CURSOR_EXAMPLE,
)
UNKNOWN_COMPETITION_OR_SEASON = error(
    "Unknown competition (plain message), or unknown season of it (with `valid_values`)",
    MessageError | ParameterError,
    unknown_competition=UNKNOWN_COMPETITION_EXAMPLE,
    unknown_season=UNKNOWN_SEASON_EXAMPLE,
)
REQUEST_VALIDATION = error(
    "Request validation failed",
    RequestValidationErrorResponse,
    validation=VALIDATION_EXAMPLE,
)
UNAUTHORIZED = error(
    "Missing or invalid `X-API-Key`",
    MessageError,
    unauthorized=example("Invalid key", "Invalid or missing API key"),
)
WRITES_DISABLED = error(
    "No API keys are configured, so write endpoints are disabled",
    MessageError,
    disabled=example("Writes disabled", "Write endpoints are disabled"),
)
JOB_CONFLICT = error(
    "A job for this competition-season is already active with a different source URL "
    "(a request for the same source returns the active job instead)",
    MessageError,
    conflict=example("Active job", "A scrape job for ekstraklasa 2025-2026 is already active"),
)
LOCATION_HEADER = {
    "Location": {"description": "The job `status_url`", "schema": {"type": "string"}}
}
AUTH_ERRORS: dict[int | str, dict[str, Any]] = {401: UNAUTHORIZED, 503: WRITES_DISABLED}
