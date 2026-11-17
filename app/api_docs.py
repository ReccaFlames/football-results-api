"""OpenAPI metadata: tags, shared query parameters, and documented error responses.

Operation IDs are snake_case and part of the public contract: the CLI, skill, llms.txt, and MCP
surfaces are derived from them, so never rename one without a deprecation.
"""

from typing import Annotated, Any

from fastapi import FastAPI, Query
from pydantic.json_schema import models_json_schema

from app.contract import MAX_LIMIT
from app.models import Problem
from app.problems import PROBLEM_MEDIA_TYPE, problem_body
from app.seasons import SEASON_PATTERN

API_DESCRIPTION = """\
Football fixtures, results, standings, and teams imported from external sources and refreshed
automatically around kickoff times.

Start with `list_competitions` to find competition slugs and seasons, then use `list_matches` for
fixtures and results, `get_standings` for league tables, and `list_teams` to resolve team slugs.
List responses are cursor-paginated and report per-season `freshness`. Unknown values are `null`,
never `0`. Write operations require an `X-API-Key` header.

Errors are RFC 9457 problem details (`application/problem+json`): branch on `type`, and use
`parameter`, `valid_values`, `hint`, and `examples` to correct the request. Requests are rate
limited per client (`RateLimit`, `RateLimit-Policy`; `429` with `Retry-After`). Read responses
carry a weak `ETag` and `Cache-Control`; send `If-None-Match` to get `304 Not Modified`.
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


PROBLEM_REF = "#/components/schemas/Problem"


def example(
    summary: str, problem_type: str, detail: str, instance: str, **extensions: Any
) -> dict[str, Any]:
    return {"summary": summary, "value": problem_body(problem_type, detail, instance, **extensions)}


def error(
    description: str,
    headers: dict[str, Any] | None = None,
    **examples: dict[str, Any],
) -> dict[str, Any]:
    """One problem+json entry of a route's ``responses=``."""
    content: dict[str, Any] = {"schema": {"$ref": PROBLEM_REF}}
    if examples:
        content["examples"] = examples
    response: dict[str, Any] = {
        "description": description,
        "content": {PROBLEM_MEDIA_TYPE: content},
    }
    if headers:
        response["headers"] = headers
    return response


def _header(description: str, example_value: str | int, kind: str = "string") -> dict[str, Any]:
    return {"description": description, "schema": {"type": kind, "example": example_value}}


RATE_LIMIT_HEADERS = {
    "RateLimit-Policy": _header(
        "Quota policy (draft-ietf-httpapi-ratelimit-headers-11): `q` requests per `w` seconds",
        '"read";q=120;w=60',
    ),
    "RateLimit": _header(
        "Remaining requests `r` in the current window, which resets in `t` seconds",
        '"read";r=87;t=42',
    ),
}
CACHE_HEADERS = {
    "ETag": _header("Weak validator of the body; send it back in `If-None-Match`", 'W/"3f2a"'),
    "Cache-Control": _header(
        "`public, max-age=N` until the next scheduled update (at most 60 s by default), or "
        "`public, no-cache` when an update is due",
        "public, max-age=60",
    ),
}
NO_STORE_HEADER = {"Cache-Control": _header("Never cached", "no-store")}
LOCATION_HEADER = {"Location": _header("The job `status_url`", "/api/v1/jobs/42")}


def cached_read(path: str) -> dict[int | str, dict[str, Any]]:
    """Responses shared by cacheable, rate-limited read operations."""
    return {
        200: {"description": "Successful Response", "headers": CACHE_HEADERS | RATE_LIMIT_HEADERS},
        304: {
            "description": "Not Modified: the `If-None-Match` ETag is current (empty body)",
            "headers": CACHE_HEADERS | RATE_LIMIT_HEADERS,
        },
        429: rate_limited(path, "read"),
    }


def rate_limited(path: str, policy: str) -> dict[str, Any]:
    quota = 120 if policy == "read" else 10
    return error(
        "Too many requests from this client; retry after `Retry-After` seconds",
        headers={
            "Retry-After": _header("Seconds until the window resets", 42, "integer"),
            **RATE_LIMIT_HEADERS,
        },
        rate_limited=example(
            "Rate limit exceeded",
            "rate-limited",
            f"Rate limit exceeded: {quota} {policy} requests per 60 seconds",
            path,
            hint="Retry after 42 seconds.",
        ),
    )


def validation_example(path: str, loc: list[str | int], msg: str, kind: str) -> dict[str, Any]:
    return example(
        "Request validation failed",
        "validation-error",
        f"{loc[-1]}: {msg}",
        path,
        parameter=str(loc[-1]),
        errors=[{"loc": loc, "msg": msg, "type": kind}],
    )


def limit_validation_example(path: str) -> dict[str, Any]:
    return validation_example(
        path,
        ["query", "limit"],
        f"Input should be less than or equal to {MAX_LIMIT}",
        "less_than_equal",
    )


def unknown_competition_example(path: str) -> dict[str, Any]:
    return example(
        "Unknown competition",
        "unknown-competition",
        "Unknown competition: la-liga",
        path,
        parameter="competition",
        invalid=["la-liga"],
        valid_values=["ekstraklasa", "premier-league"],
        hint="Use a slug from list_competitions.",
    )


def unknown_fields_example(path: str, valid_values: list[str]) -> dict[str, Any]:
    return example(
        "Unknown fields",
        "invalid-parameter",
        "Unknown fields requested",
        path,
        parameter="fields",
        invalid=["score"],
        valid_values=valid_values,
    )


def invalid_cursor(path: str) -> dict[str, Any]:
    return error(
        "Malformed cursor, or a cursor used with other filters or order",
        invalid_cursor=example(
            "Malformed cursor",
            "invalid-cursor",
            "Invalid cursor",
            path,
            parameter="cursor",
            hint="Pass next_cursor from a previous response unchanged, or omit cursor.",
        ),
        mismatched_cursor=example(
            "Cursor from other filters",
            "invalid-cursor",
            "The cursor belongs to a request with different filters or order",
            path,
            parameter="cursor",
            hint="Repeat the original filters with this cursor, or omit cursor to restart.",
        ),
    )


def unknown_competition(path: str) -> dict[str, Any]:
    return error(
        "Unknown competition (with `valid_values`)",
        unknown_competition=unknown_competition_example(path),
    )


def unknown_competition_or_season(path: str) -> dict[str, Any]:
    return error(
        "Unknown competition or unknown season of it (each with `valid_values`)",
        unknown_competition=unknown_competition_example(path),
        unknown_season=example(
            "Unknown season",
            "unknown-season",
            "Unknown season 1999-2000 for ekstraklasa",
            path,
            parameter="season",
            invalid=["1999-2000"],
            valid_values=["2026-2027", "2025-2026"],
        ),
    )


def request_validation(path: str, example_value: dict[str, Any] | None = None) -> dict[str, Any]:
    return error(
        "Request validation failed", validation=example_value or limit_validation_example(path)
    )


def minimum_validation_example(path: str, location: str, name: str) -> dict[str, Any]:
    return validation_example(
        path, [location, name], "Input should be greater than or equal to 1", "greater_than_equal"
    )


def auth_errors(path: str) -> dict[int | str, dict[str, Any]]:
    return {
        401: error(
            "Missing or invalid `X-API-Key`",
            headers={"WWW-Authenticate": _header("Authentication scheme", "ApiKey")},
            unauthorized=example(
                "Invalid key",
                "unauthorized",
                "Invalid or missing API key",
                path,
                hint="Send a configured API key in the X-API-Key header.",
            ),
        ),
        503: error(
            "No API keys are configured, so write endpoints are disabled",
            disabled=example(
                "Writes disabled",
                "writes-disabled",
                "Write endpoints are disabled",
                path,
                hint="The operator must set FOOTBALL_RESULTS_API_KEYS.",
            ),
        ),
    }


def job_conflict(path: str) -> dict[str, Any]:
    return error(
        "A job for this competition-season is already active with a different source URL "
        "(a request for the same source returns the active job instead)",
        conflict=example(
            "Active job",
            "job-conflict",
            "A scrape job for ekstraklasa 2025-2026 is already active",
            path,
        ),
    )


def install_openapi(app: FastAPI) -> None:
    """Add the `Problem` schema, which responses reference under `application/problem+json`."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        schema = generate()
        _, definitions = models_json_schema(
            [(Problem, "validation")], ref_template="#/components/schemas/{model}"
        )
        schema.setdefault("components", {}).setdefault("schemas", {}).update(definitions["$defs"])
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
