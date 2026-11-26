import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import version
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy.exc import SQLAlchemyError
from starlette.routing import Match as RouteMatch

from app import access, agent_docs, notify, oauth, observability
from app import api_docs as docs
from app.caching import (
    CACHEABLE_OPERATIONS,
    CACHEABLE_PAGES,
    cache_control,
    etag_for,
    if_none_match,
    max_age_seconds,
    private_cache_control,
)
from app.competitions import (
    competition_summary,
    provider_competition_code,
    source_from_url,
    with_data_attributions,
)
from app.config import (
    check_demo_mode,
    check_private_instance,
    load_config,
    parse_networks,
    settings,
    telegram_errors,
)
from app.contract import (
    DEFAULT_LIMIT,
    ContractError,
    compact,
    encode_cursor,
    filter_hash,
    id_keyset,
    match_keyset,
    parse_access_request_statuses,
    parse_date_bound,
    parse_fields,
    parse_job_statuses,
    parse_statuses,
    slug_keyset,
)
from app.database import session_factory
from app.logs import configure_logging
from app.mcp_server import (
    AUTH_SCOPE_KEY,
    CLIENT_SCOPE_KEY,
    IDENTITY_SCOPE_KEY,
    INTERNAL_SCOPE_KEY,
    MCP_PATH,
    McpEndpoint,
    McpRoute,
    create_server,
)
from app.models import (
    ACCESS_REQUEST_STATUSES,
    PROBLEM_TYPES,
    SCRAPE_RUN_STATUSES,
    AccessRequestCreate,
    AccessRequestCreated,
    AccessRequestDetails,
    AccessRequestsResponse,
    AccessRequestStatus,
    Competition,
    CompetitionSummary,
    Freshness,
    HealthStatus,
    HistoricalFetchRequest,
    JobAccepted,
    Match,
    MatchesResponse,
    MatchQuery,
    Pagination,
    RefreshRequest,
    ResultsResponse,
    SchedulerStatus,
    ScrapeJobStatus,
    ScrapeRunsResponse,
    StandingRow,
    StandingsResponse,
    TeamsResponse,
)
from app.problems import ProblemError, install_problem_handlers, problem_response
from app.ratelimit import (
    ACCESS_REQUEST_POLICY,
    EXEMPT_PATHS,
    READ_METHODS,
    Client,
    FixedWindowRateLimiter,
    RateLimitPolicy,
    client_ip,
)
from app.repository import (
    CLAIM_WINDOW,
    UNAPPROVED_RETENTION,
    AccessRequestPending,
    AccessRequestStateConflict,
    ActiveScrapeJobConflict,
    PostgresResultRepository,
    StoredAccessRequest,
)
from app.seasons import SEASON_PATTERN, validate_season
from app.standings import compute_standings

logger = logging.getLogger(__name__)
SCHEDULER_WORKERS_SHOWN = 10
store = PostgresResultRepository(session_factory)
rate_limiter = FixedWindowRateLimiter()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
METRICS_PATH = "/metrics"


# Keys from FOOTBALL_RESULTS_API_KEYS_FILE (ADR 0005), reloaded when the file changes (ADR 0008).
file_keys: tuple[access.FileKey, ...] = ()
keys_watcher: access.KeysFileWatcher | None = None
KEY_EXPIRES_HEADER = "API-Key-Expires"
PRIVATE_HINT = (
    "This is a private instance: send a key issued by the operator in the X-API-Key header. "
    "Request one with create_access_request (POST /api/v1/access-requests), or ask the "
    "operator for a new one if yours expired."
)
ADMIN_KEY_HINT = (
    "The operator must configure an admin API key (FOOTBALL_RESULTS_API_KEYS or "
    "FOOTBALL_RESULTS_API_KEYS_FILE)."
)
DEMO_HINT = (
    "This is a read-only demo instance (ADR 0007): writes, jobs, admin operations, and metrics "
    "are unavailable. Self-host the API to use them."
)


def _current_keys() -> tuple[access.FileKey, ...]:
    global file_keys
    if keys_watcher is not None:
        file_keys = keys_watcher.poll(time.monotonic())
    return file_keys


def _identity(supplied: str | None) -> access.Identity | None:
    return access.resolve(supplied or "", access.env_keys(settings.api_keys), _current_keys())


def _subject_identity(subject: str) -> access.Identity:
    return access.resolve_subject(subject, _current_keys())


def _metrics_credential(request: Request) -> str:
    """`Authorization: Bearer` (Prometheus' native auth), else `X-API-Key`."""
    scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
    supplied = credentials.strip() if scheme.lower() == "bearer" else ""
    return supplied or request.headers.get("X-API-Key", "")


def _require_admin(
    identity: access.Identity | None, *, scheme: str, disabled: str, hint: str
) -> None:
    if not access.has_admin_key(access.env_keys(settings.api_keys), _current_keys()):
        raise ProblemError(
            "writes-disabled", disabled, hint=DEMO_HINT if settings.demo_mode else ADMIN_KEY_HINT
        )
    if identity is None:
        raise ProblemError(
            "unauthorized",
            "Invalid or missing API key",
            headers={"WWW-Authenticate": scheme},
            hint=hint,
        )
    if identity.role != "admin":
        raise ProblemError(
            "forbidden",
            f"The caller {identity.name!r} has role {identity.role}, which does not allow "
            "this operation",
            hint="Use an admin key, or leave this operation to the operator. Read operations "
            "work with any key" + ("." if not settings.anonymous_access else " or none."),
        )


async def require_api_key(request: Request, api_key: str | None = Depends(api_key_header)) -> None:
    # A tool's in-process call acts as the identity of the `/mcp` caller (key or OAuth subject).
    if INTERNAL_SCOPE_KEY in request.scope:
        identity = request.scope.get(IDENTITY_SCOPE_KEY)
    else:
        identity = _identity(api_key)
    _require_admin(
        identity,
        scheme="ApiKey",
        disabled="Write endpoints are disabled",
        hint="Send an admin API key in the X-API-Key header.",
    )


def require_metrics_key(request: Request) -> None:
    """Like `require_api_key`, but also accepts `Authorization: Bearer` (Prometheus' native auth)."""
    _require_admin(
        _identity(_metrics_credential(request)),
        scheme="Bearer",
        disabled="Metrics are disabled",
        hint="Send an admin API key as Authorization: Bearer <key> or in X-API-Key.",
    )


def _job_accepted(response: Response, job_id: int, status: str) -> JobAccepted:
    status_url = f"/api/v1/jobs/{job_id}"
    response.headers["Location"] = status_url
    return JobAccepted(job_id=job_id, status=status, status_url=status_url)


def _contract[T](parse: Callable[[], T]) -> T:
    try:
        return parse()
    except ContractError as error:
        extensions = {key: value for key, value in error.detail.items() if key != "message"}
        raise ProblemError(error.problem_type, error.detail["message"], **extensions) from error


async def _unknown_competition(slug: str) -> ProblemError:
    return ProblemError(
        "unknown-competition",
        f"Unknown competition: {slug}",
        parameter="competition",
        invalid=[slug],
        valid_values=sorted(item.slug for item in await store.list_competitions()),
        hint="Use a slug from list_competitions.",
    )


async def _require_season(slug: str, season: str | None) -> Competition:
    competition = await store.get_competition(slug)
    if competition is None:
        raise await _unknown_competition(slug)
    if season is not None and season not in competition.seasons:
        raise ProblemError(
            "unknown-season",
            f"Unknown season {season} for {competition.slug}",
            parameter="season",
            invalid=[season],
            valid_values=sorted(competition.seasons, reverse=True),
        )
    return competition


def _freshness_cache_control(freshness: list[Freshness]) -> dict[str, str]:
    max_age = max_age_seconds(
        (item.next_update_at for item in freshness),
        datetime.now(UTC),
        settings.cache_max_age_seconds,
    )
    return {"Cache-Control": cache_control(max_age)}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings.log_format, anonymize_ips=settings.log_ips_anonymized)
    config = load_config()
    global file_keys, keys_watcher
    check_private_instance()
    if settings.api_keys_file is None:
        keys_watcher, file_keys = None, ()
    else:
        watcher = access.KeysFileWatcher(settings.api_keys_file)
        file_keys = watcher.load(time.monotonic())
        keys_watcher = watcher
    # Before seeding, so a demo never stores competitions it may not serve.
    check_demo_mode(config, file_keys)
    for warning in config.fallback_warnings(
        settings.football_data_api_token, settings.thesportsdb_api_key.get_secret_value()
    ):
        logger.warning(warning)
    for error in telegram_errors(settings):
        logger.warning("Telegram notifications of access requests are off: %s", error)
    await store.seed_competitions(config.to_competitions(), config.auto_rollover_slugs())
    try:
        async with mcp_endpoint.lifespan():
            yield
    finally:
        keys_watcher = None


app = FastAPI(
    title="Football Results API",
    version=version("football-results-api"),
    description=docs.API_DESCRIPTION,
    openapi_tags=docs.OPENAPI_TAGS,
    lifespan=lifespan,
)
install_problem_handlers(app)
docs.install_openapi(app)
# The MCP server is a plain Starlette route, so it stays out of the OpenAPI spec.
mcp_endpoint = McpEndpoint(create_server(app), settings, _identity, _subject_identity)
app.router.routes.append(McpRoute(mcp_endpoint))


_trusted_proxies = lru_cache(maxsize=8)(parse_networks)


def _request_identity(request: Request) -> access.Identity | None:
    """The caller of an outer request: the `/mcp` caller (key or OAuth subject), the `/metrics`
    credential, else `X-API-Key`."""
    auth = request.scope.get(AUTH_SCOPE_KEY)
    if auth is not None:
        return auth.identity
    if request.url.path == METRICS_PATH:
        return _identity(_metrics_credential(request))
    return _identity(request.headers.get("X-API-Key"))


def _rate_limit_client(request: Request, identity: access.Identity | None) -> Client:
    """A keyed client for a valid key or, on `/mcp`, OAuth subject (its own quotas), otherwise
    the client IP."""
    read, write = settings.rate_limit_read_per_minute, settings.rate_limit_write_per_minute
    if identity is not None:
        return Client(
            identity.bucket,
            identity.name,
            identity.read_per_minute or read,
            identity.write_per_minute or write,
        )
    return Client("ip:" + _client_address(request), access.ANONYMOUS, read, write)


def _client_address(request: Request) -> str:
    peer = request.client.host if request.client else None
    forwarded = request.headers.getlist("X-Forwarded-For")
    return client_ip(peer, forwarded, _trusted_proxies(settings.trusted_proxies))


def _rate_limit_hit(
    request: Request, identity: access.Identity | None
) -> tuple[RateLimitPolicy, Client] | None:
    """The policy and client to count this request against, or None if it is not limited.

    A `/mcp` request counts once as a read. The tools' in-process API calls are not counted
    again, except writes, which count against the write quota of the MCP caller. `/metrics`
    is exempt only with a valid key, so failed attempts count against the client IP.
    """
    if not settings.rate_limit_enabled:
        return None
    if request.url.path in EXEMPT_PATHS and not (
        request.url.path == METRICS_PATH and identity is None
    ):
        return None
    if INTERNAL_SCOPE_KEY in request.scope:
        outer: Client | None = request.scope[INTERNAL_SCOPE_KEY]
        if request.method in READ_METHODS or outer is None:
            return None
        return outer.policy(request.method), outer
    client = _rate_limit_client(request, identity)
    if request.url.path == MCP_PATH:
        request.scope[CLIENT_SCOPE_KEY] = client
        return client.policy("GET"), client
    return client.policy(request.method), client


@app.middleware("http")
async def rate_limit_and_cache(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    rate_headers: dict[str, str] = {}
    internal = INTERNAL_SCOPE_KEY in request.scope
    if request.url.path == MCP_PATH and not internal:
        await mcp_endpoint.authenticate(request.scope)
    identity = None if internal else _request_identity(request)
    if (hit := _rate_limit_hit(request, identity)) is not None:
        policy, client = hit
        decision = rate_limiter.hit(policy, client.bucket, time.monotonic())
        observability.record_quota_use(client.label, policy.name, decision.allowed)
        rate_headers = decision.headers()
        if not decision.allowed:
            policy = decision.policy
            return problem_response(
                request,
                "rate-limited",
                f"Rate limit exceeded: {policy.quota} {policy.name} requests per "
                f"{policy.window_seconds} seconds",
                headers={**rate_headers, "Retry-After": str(decision.reset_seconds)},
                hint=f"Retry after {decision.reset_seconds} seconds.",
            )
    # Counted above, so failed attempts use up the client IP's quota.
    if identity is None and not internal and _needs_key(request):
        return problem_response(
            request,
            "unauthorized",
            "This instance requires an API key: X-API-Key is missing, wrong, or expired",
            headers={**rate_headers, "WWW-Authenticate": "ApiKey"},
            hint=PRIVATE_HINT,
        )

    response = await call_next(request)
    # A route's own limit (`create_access_request`) keeps its headers.
    for name, value in rate_headers.items():
        response.headers.setdefault(name, value)
    if identity is not None and identity.expires_at is not None:
        response.headers[KEY_EXPIRES_HEADER] = access.format_time(identity.expires_at)
    route = request.scope.get("route")
    operation_id = getattr(route, "operation_id", None)
    cacheable = (
        operation_id in CACHEABLE_OPERATIONS or getattr(route, "path", None) in CACHEABLE_PAGES
    )
    uncached = operation_id is not None or isinstance(route, McpRoute)
    if response.status_code >= 400 or (uncached and not cacheable):
        response.headers["Cache-Control"] = "no-store"
        return response
    if not cacheable or request.method != "GET" or response.status_code != 200:
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
    etag = etag_for(body)
    headers = {key: value for key, value in response.headers.items() if key != "content-length"}
    headers["ETag"] = etag
    headers.setdefault("cache-control", cache_control(settings.cache_max_age_seconds))
    if operation_id in CACHEABLE_OPERATIONS and not settings.anonymous_access:
        # Keyed data must not be stored by a shared cache that would skip the key check.
        headers["cache-control"] = private_cache_control(headers["cache-control"])
    if if_none_match(request.headers.get("If-None-Match"), etag):
        headers.pop("content-type", None)
        return Response(status_code=304, headers=headers)
    return Response(content=body, status_code=200, headers=headers)


def _route_for(request: Request) -> object | None:
    route = request.scope.get("route")
    if route is not None:
        return route
    return next(
        (
            candidate
            for candidate in app.router.routes
            if candidate.matches(request.scope)[0] == RouteMatch.FULL
        ),
        None,
    )


def _needs_key(request: Request) -> bool:
    """On a private instance, every API operation except `access.PUBLIC_OPERATIONS` needs a key.

    `/mcp` and `/metrics` check their own credentials; documentation pages stay public.
    """
    if settings.anonymous_access:
        return False
    operation_id = getattr(_route_for(request), "operation_id", None)
    return operation_id is not None and operation_id not in access.PUBLIC_OPERATIONS


METRIC_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})


def _metric_operation(request: Request) -> str:
    """The operation ID, else the route template; requests rejected before routing are matched
    here so a `429` is counted against its operation."""
    route = _route_for(request)
    return getattr(route, "operation_id", None) or getattr(route, "path", None) or "unmatched"


@app.middleware("http")
async def record_http_metrics(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        method = request.method if request.method in METRIC_METHODS else "other"
        observability.record_request(
            _metric_operation(request), method, status, time.perf_counter() - started
        )


@app.get(
    "/health",
    operation_id="get_health",
    tags=[docs.TAG_SYSTEM],
    summary="Check service health",
    description=(
        "Use for monitoring. Reports database connectivity and whether the scheduler heartbeat "
        "is recent (`ok`), old (`stale`), or missing (`not_started`)."
    ),
    response_model=HealthStatus,
    responses={
        200: {"description": "Successful Response", "headers": docs.NO_STORE_HEADER},
        503: docs.error(
            "The database is unreachable",
            database=docs.example(
                "Database down", "service-unavailable", "Database unavailable", "/health"
            ),
        ),
    },
)
async def health() -> dict[str, str]:
    try:
        await store.health()
    except Exception as error:
        raise ProblemError("service-unavailable", "Database unavailable") from error
    state = observability.scheduler_state(
        await store.latest_scheduler_heartbeat(),
        datetime.now(UTC),
        observability.stall_window(settings.scheduler_loop_seconds),
    )
    # `/health` predates the glossary term "stalled scheduler" and keeps its public value.
    return {"status": "ok", "database": "ok", "scheduler": "stale" if state == "stalled" else state}


@app.get(
    "/api/v1/competitions",
    operation_id="list_competitions",
    tags=[docs.TAG_COMPETITIONS],
    summary="List competitions and their seasons",
    description=(
        "Use first to discover competition slugs, the current season, available and fetched "
        "seasons, the season format, scheduler state, and the data attributions to keep when "
        "you publish the data. Not paginated."
    ),
    response_model=list[CompetitionSummary],
    responses=docs.cached_read("/api/v1/competitions"),
)
async def get_competitions() -> list[CompetitionSummary]:
    return with_data_attributions(
        await store.list_competitions(), _configured_provider_competitions()
    )


@app.get(
    "/api/v1/results",
    operation_id="list_results",
    tags=[docs.TAG_MATCHES],
    summary="List finished matches (legacy)",
    description=(
        "Deprecated: use `list_matches` with `status=finished`. Kept unchanged for existing "
        "clients. Returns every finished match matching the filters, without pagination."
    ),
    deprecated=True,
    response_model=ResultsResponse,
    responses={
        **docs.cached_read("/api/v1/results"),
        404: docs.unknown_competition("/api/v1/results"),
        422: docs.request_validation(
            "/api/v1/results",
            docs.minimum_validation_example("/api/v1/results", "query", "round"),
        ),
    },
)
async def get_results(
    competition: docs.CompetitionFilter = None,
    season: docs.SeasonFilter = None,
    round_number: docs.RoundFilter = None,
    stage: docs.StageFilter = None,
    team: docs.TeamFilter = None,
) -> ResultsResponse:
    if competition and await store.get_competition(competition) is None:
        raise await _unknown_competition(competition)
    results, refreshed_at = await store.get_results(
        competition=competition,
        season=season,
        round_number=round_number,
        stage=stage,
        team=team,
    )
    return ResultsResponse(
        results=results,
        count=len(results),
        refreshed_at=refreshed_at,
        stale=refreshed_at is None,
    )


@app.get(
    "/api/v1/matches",
    operation_id="list_matches",
    tags=[docs.TAG_MATCHES],
    summary="Find matches: results, fixtures, and postponed games",
    description=(
        "Use for any question about matches: results, upcoming fixtures, a team's recent form, "
        "or a round. Combine `status`, date range, `team`, `round`, and `stage`; use "
        "`order=desc` with a small `limit` for the latest matches. Items contain every Match "
        "field (unknown values are null, never 0) unless `fields` selects a subset; `id` is "
        "always included. `freshness` reports each competition-season in the filter."
    ),
    response_model=MatchesResponse,
    responses={
        **docs.cached_read("/api/v1/matches"),
        400: docs.invalid_cursor("/api/v1/matches"),
        404: docs.unknown_competition_or_season("/api/v1/matches"),
        422: docs.error(
            "Invalid `fields`, `status`, or date (`invalid-parameter` with `invalid` and "
            "`valid_values` or `examples`), or request validation failed (`validation-error`)",
            unknown_fields=docs.unknown_fields_example("/api/v1/matches", list(Match.model_fields)),
            unknown_status=docs.example(
                "Unknown status",
                "invalid-parameter",
                "Unknown match status",
                "/api/v1/matches",
                parameter="status",
                invalid=["played"],
                valid_values=["finished", "scheduled", "inactive"],
            ),
            invalid_date=docs.example(
                "Invalid date",
                "invalid-parameter",
                "date_from must be an ISO 8601 date or datetime",
                "/api/v1/matches",
                parameter="date_from",
                invalid=["01.08.2026"],
                examples=["2026-08-01", "2026-08-01T18:00:00+02:00"],
            ),
            validation=docs.limit_validation_example("/api/v1/matches"),
        ),
    },
)
async def get_matches(
    competition: docs.CompetitionFilter = None,
    season: docs.SeasonFilter = None,
    status: Annotated[
        str | None,
        Query(
            description="Comma-separated statuses: finished, scheduled, inactive (default: all)",
            examples=["finished", "scheduled,inactive"],
        ),
    ] = None,
    date_from: Annotated[
        str | None,
        Query(
            description="Earliest kickoff, inclusive: ISO date or datetime (naive values are UTC)",
            examples=["2026-08-01"],
        ),
    ] = None,
    date_to: Annotated[
        str | None,
        Query(
            description=(
                "Latest kickoff, inclusive: ISO date (the whole UTC day) or datetime "
                "(naive values are UTC)"
            ),
            examples=["2026-08-31"],
        ),
    ] = None,
    team: docs.TeamFilter = None,
    round_number: docs.RoundFilter = None,
    stage: docs.StageFilter = None,
    order: Annotated[
        Literal["asc", "desc"],
        Query(description="Kickoff order; matches without a kickoff sort last"),
    ] = "asc",
    limit: docs.Limit = DEFAULT_LIMIT,
    cursor: docs.Cursor = None,
    fields: Annotated[
        str | None,
        Query(
            description="Comma-separated Match fields for compact items; `id` is always included",
            examples=["kickoff_at,home_team_slug,away_team_slug,home_goals,away_goals"],
        ),
    ] = None,
) -> Response:
    selected = _contract(lambda: parse_fields(fields, Match, always=("id",)))
    statuses = _contract(lambda: parse_statuses(status))
    kickoff_from = _contract(lambda: parse_date_bound(date_from, "date_from", upper=False))
    kickoff_until = _contract(lambda: parse_date_bound(date_to, "date_to", upper=True))
    if competition:
        await _require_season(competition, season)
    query = MatchQuery(
        competition=competition.casefold() if competition else None,
        season=season,
        statuses=statuses,
        kickoff_from=kickoff_from,
        kickoff_until=kickoff_until,
        team=(team.strip() or None) if team else None,
        round=round_number,
        stage=stage,
        descending=order == "desc",
    )
    digest = filter_hash(query.model_dump(mode="json", exclude={"after"}))
    if cursor is not None:
        after = _contract(lambda: match_keyset(cursor, digest))
        query = query.model_copy(update={"after": after})
    matches = await store.find_matches(query, limit + 1)
    page = matches[:limit]
    next_cursor = None
    if len(matches) > limit:
        last = page[-1]
        kickoff = None if last.kickoff_at is None else last.kickoff_at.isoformat()
        next_cursor = encode_cursor([kickoff, last.id], digest)
    freshness = await store.get_freshness(query.competition, season)
    return JSONResponse(
        {
            "data": [compact(item, selected) for item in page],
            "pagination": {"limit": limit, "next_cursor": next_cursor},
            "freshness": [item.model_dump(mode="json") for item in freshness],
        },
        headers=_freshness_cache_control(freshness),
    )


@app.get(
    "/api/v1/standings",
    operation_id="get_standings",
    tags=[docs.TAG_STANDINGS],
    summary="Get the league table of a competition-season",
    description=(
        "Use for table positions, points, and recent form. Computed from stored finished "
        "matches: 3 points for a win, 1 for a draw; ties are broken by goal difference, goals "
        "for, wins, then team name (head-to-head is not applied). Cup competitions require "
        "`stage` (for example a group). Not paginated."
    ),
    response_model=StandingsResponse,
    responses={
        **docs.cached_read("/api/v1/standings"),
        404: docs.unknown_competition_or_season("/api/v1/standings"),
        422: docs.error(
            "Invalid `fields`, a cup without `stage`, or a competition without a current "
            "season (`invalid-parameter`, each with `valid_values`), or request validation "
            "failed (`validation-error`)",
            unknown_fields=docs.unknown_fields_example(
                "/api/v1/standings", list(StandingRow.model_fields)
            ),
            stage_required=docs.example(
                "Cup needs a stage",
                "invalid-parameter",
                "Standings for a cup competition need a stage (for example a group)",
                "/api/v1/standings",
                parameter="stage",
                valid_values=["Faza ligowa", "Runda eliminacyjna"],
            ),
            validation=docs.validation_example(
                "/api/v1/standings", ["query", "competition"], "Field required", "missing"
            ),
        ),
    },
)
async def get_standings(
    competition: Annotated[
        str,
        Query(description="Competition slug from `list_competitions`", examples=["ekstraklasa"]),
    ],
    season: Annotated[
        str | None,
        Query(
            pattern=SEASON_PATTERN,
            description="Season in the competition's format; defaults to the current season",
            examples=["2026-2027"],
        ),
    ] = None,
    stage: Annotated[
        str | None,
        Query(
            description="Stage substring; required for cup competitions",
            examples=["Group A"],
        ),
    ] = None,
    fields: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated StandingRow fields for compact rows; `team_slug` is always "
                "included"
            ),
            examples=["position,points,form"],
        ),
    ] = None,
) -> Response:
    selected = _contract(lambda: parse_fields(fields, StandingRow, always=("team_slug",)))
    found = await _require_season(competition, season)
    season = season or found.current_season
    if season is None:
        raise ProblemError(
            "invalid-parameter",
            f"{found.slug} has no current season; pass season",
            parameter="season",
            valid_values=sorted(found.seasons, reverse=True),
        )
    stage = stage.strip() if stage else None
    if found.format == "cup" and not stage:
        raise ProblemError(
            "invalid-parameter",
            "Standings for a cup competition need a stage (for example a group)",
            parameter="stage",
            valid_values=await store.list_stages(found.slug, season),
        )
    matches = await store.find_matches(
        MatchQuery(competition=found.slug, season=season, statuses=("finished",), stage=stage)
    )
    freshness = await store.get_freshness(found.slug, season)
    return JSONResponse(
        {
            "competition": found.slug,
            "season": season,
            "stage": stage,
            "data": [compact(row, selected) for row in compute_standings(matches)],
            "freshness": [item.model_dump(mode="json") for item in freshness],
        },
        headers=_freshness_cache_control(freshness),
    )


@app.get(
    "/api/v1/teams",
    operation_id="list_teams",
    tags=[docs.TAG_COMPETITIONS],
    summary="Find teams and their canonical slugs",
    description=(
        "Use to resolve a team name to its canonical slug (for the `team` filter of "
        "`list_matches`) or to list the teams of a competition-season. Includes source aliases; "
        "ordered by slug."
    ),
    response_model=TeamsResponse,
    responses={
        **docs.cached_read("/api/v1/teams"),
        400: docs.invalid_cursor("/api/v1/teams"),
        404: docs.unknown_competition_or_season("/api/v1/teams"),
        422: docs.request_validation("/api/v1/teams"),
    },
)
async def get_teams(
    competition: docs.CompetitionFilter = None,
    season: docs.SeasonFilter = None,
    q: Annotated[
        str | None,
        Query(
            description="Canonical team slug, otherwise a case-insensitive name or slug substring",
            examples=["wisla"],
        ),
    ] = None,
    limit: docs.Limit = DEFAULT_LIMIT,
    cursor: docs.Cursor = None,
) -> TeamsResponse:
    if competition:
        await _require_season(competition, season)
    competition = competition.casefold() if competition else None
    q = (q.strip() or None) if q else None
    digest = filter_hash({"competition": competition, "season": season, "q": q})
    after_slug = None
    if cursor is not None:
        after_slug = _contract(lambda: slug_keyset(cursor, digest))
    teams = await store.list_teams(competition, season, q, limit + 1, after_slug)
    page = teams[:limit]
    next_cursor = encode_cursor([page[-1].slug], digest) if len(teams) > limit else None
    return TeamsResponse(data=page, pagination=Pagination(limit=limit, next_cursor=next_cursor))


@app.post(
    "/api/v1/results/refresh",
    operation_id="request_refresh",
    tags=[docs.TAG_REFRESH],
    summary="Queue a refresh of the current season",
    description=(
        "Use only when current-season data is stale and cannot wait for the scheduler. Returns "
        "`202` with a job `status_url` (also in `Location`); poll `get_job` until it completes. "
        "For other seasons use `fetch_historical_season`."
    ),
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
    responses={
        202: {
            "description": "The refresh is queued",
            "headers": docs.LOCATION_HEADER | docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        **docs.auth_errors("/api/v1/results/refresh"),
        404: docs.unknown_competition("/api/v1/results/refresh"),
        409: docs.job_conflict("/api/v1/results/refresh"),
        422: docs.error(
            "Not the current season or no current season configured (`invalid-request`), or "
            "request validation failed (`validation-error`)",
            not_current=docs.example(
                "Historical season",
                "invalid-request",
                "Refresh supports only the current season 2026-2027; "
                "use /api/v1/results/history/fetch for historical seasons",
                "/api/v1/results/refresh",
                parameter="season",
                invalid=["2025-2026"],
                valid_values=["2026-2027"],
                hint="Use fetch_historical_season for other seasons.",
            ),
            validation=docs.validation_example(
                "/api/v1/results/refresh", ["body", "competition"], "Field required", "missing"
            ),
        ),
        429: docs.rate_limited("/api/v1/results/refresh", "write"),
    },
)
async def refresh_results(request: RefreshRequest, response: Response) -> JobAccepted:
    competition = await store.get_competition(request.competition)
    if competition is None:
        raise await _unknown_competition(request.competition)
    if competition.current_season is None:
        raise ProblemError(
            "invalid-request",
            "No current season is configured",
            hint="Use fetch_historical_season with an explicit season.",
        )
    season = request.season or competition.current_season
    if season != competition.current_season:
        raise ProblemError(
            "invalid-request",
            f"Refresh supports only the current season {competition.current_season}; "
            "use /api/v1/results/history/fetch for historical seasons",
            parameter="season",
            invalid=[season],
            valid_values=[competition.current_season],
            hint="Use fetch_historical_season for other seasons.",
        )
    source = competition.seasons[competition.current_season]
    try:
        job_id, status = await store.enqueue_scrape(competition, source)
    except ActiveScrapeJobConflict as error:
        raise ProblemError("job-conflict", str(error)) from error
    return _job_accepted(response, job_id, status)


@app.get(
    "/api/v1/jobs/{job_id}",
    operation_id="get_job",
    tags=[docs.TAG_REFRESH],
    summary="Get the status of a queued import job",
    description=(
        "Use to poll a job from `request_refresh` or `fetch_historical_season` until its "
        "status is `completed` or `failed`."
    ),
    response_model=ScrapeJobStatus,
    dependencies=[Depends(require_api_key)],
    responses={
        200: {
            "description": "Successful Response",
            "headers": docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        **docs.auth_errors("/api/v1/jobs/42"),
        404: docs.error(
            "Unknown job",
            unknown_job=docs.example(
                "Unknown job",
                "unknown-job",
                "Unknown job: 42",
                "/api/v1/jobs/42",
                parameter="job_id",
                invalid=["42"],
            ),
        ),
        422: docs.request_validation(
            "/api/v1/jobs/0", docs.minimum_validation_example("/api/v1/jobs/0", "path", "job_id")
        ),
        429: docs.rate_limited("/api/v1/jobs/42", "read"),
    },
)
async def get_job_status(
    job_id: Annotated[int, Path(ge=1, description="Job ID from `status_url`", examples=[42])],
) -> ScrapeJobStatus:
    job = await store.get_scrape_job(job_id)
    if job is None:
        raise ProblemError(
            "unknown-job", f"Unknown job: {job_id}", parameter="job_id", invalid=[str(job_id)]
        )
    return job


@app.post(
    "/api/v1/results/history/fetch",
    operation_id="fetch_historical_season",
    tags=[docs.TAG_REFRESH],
    summary="Import a past season, or return it if already stored",
    description=(
        "Use when a non-current season is missing from `list_competitions` `fetched_seasons`. "
        "A stored season returns `200` with its results unless `force_refresh` is true; "
        "otherwise the fetch is queued and returns `202` with a job `status_url`. "
        "`source_url` is needed only for seasons that are not configured."
    ),
    response_model=None,
    status_code=202,
    dependencies=[Depends(require_api_key)],
    responses={
        200: {
            "model": ResultsResponse,
            "description": "The season is already stored",
            "headers": docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        202: {
            "model": JobAccepted,
            "description": "The fetch is queued",
            "headers": docs.LOCATION_HEADER | docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        **docs.auth_errors("/api/v1/results/history/fetch"),
        404: docs.unknown_competition("/api/v1/results/history/fetch"),
        409: docs.job_conflict("/api/v1/results/history/fetch"),
        422: docs.error(
            "The season or source is not acceptable for this competition (`invalid-request`), "
            "or request validation failed (`validation-error`)",
            missing_source=docs.example(
                "No source",
                "invalid-request",
                "No source configured for ekstraklasa 2019-2020; provide source_url",
                "/api/v1/results/history/fetch",
                parameter="source_url",
                hint="Pass a 90minut or football-data.org season URL as source_url.",
            ),
            current_season=docs.example(
                "Current season",
                "invalid-request",
                "Use /api/v1/results/refresh for the current season",
                "/api/v1/results/history/fetch",
                parameter="season",
                invalid=["2026-2027"],
                hint="Use request_refresh for the current season.",
            ),
            validation=docs.validation_example(
                "/api/v1/results/history/fetch",
                ["body", "season"],
                f"String should match pattern '{SEASON_PATTERN}'",
                "string_pattern_mismatch",
            ),
        ),
        429: docs.rate_limited("/api/v1/results/history/fetch", "write"),
    },
)
async def fetch_historical_results(
    request: HistoricalFetchRequest, response: Response
) -> JobAccepted | ResultsResponse:
    competition = await store.get_competition(request.competition)
    if competition is None:
        raise await _unknown_competition(request.competition)
    if request.season == competition.current_season:
        raise ProblemError(
            "invalid-request",
            "Use /api/v1/results/refresh for the current season",
            parameter="season",
            invalid=[request.season],
            hint="Use request_refresh for the current season.",
        )
    try:
        validate_season(request.season, competition.season_format)
    except ValueError as error:
        raise ProblemError(
            "invalid-request", str(error), parameter="season", invalid=[request.season]
        ) from error
    if request.source_url is not None:
        try:
            source = source_from_url(request.season, request.source_url)
        except ValueError as error:
            raise ProblemError("invalid-request", str(error), parameter="source_url") from error
    else:
        source = competition.seasons.get(request.season)
    configured_providers = sorted({item.provider for item in competition.seasons.values()})
    if source is not None and source.provider not in configured_providers:
        raise ProblemError(
            "invalid-request",
            f"Provider {source.provider} is not configured for {competition.slug}",
            parameter="source_url",
            invalid=[source.provider],
            valid_values=configured_providers,
        )
    if source is None:
        raise ProblemError(
            "invalid-request",
            f"No source configured for {competition.slug} {request.season}; provide source_url",
            parameter="source_url",
            hint="Pass a season URL of a provider configured for this competition as source_url.",
        )
    configured_codes = {
        code
        for item in competition.seasons.values()
        if (code := provider_competition_code(item)) is not None
    }
    source_code = provider_competition_code(source)
    if configured_codes and source_code is not None and source_code not in configured_codes:
        raise ProblemError(
            "invalid-request",
            f"Provider competition code {source_code} is not configured for {competition.slug}",
            parameter="source_url",
            invalid=[source_code],
            valid_values=sorted(configured_codes),
        )
    if not request.force_refresh and await store.is_cached(
        competition.slug,
        request.season,
    ):
        cached, refreshed_at = await store.get_results(
            competition=competition.slug,
            season=request.season,
        )
        response.status_code = 200
        return ResultsResponse(
            results=cached,
            count=len(cached),
            refreshed_at=refreshed_at,
            stale=False,
            fetched=False,
        )
    try:
        job_id, status = await store.enqueue_scrape(competition, source)
    except ActiveScrapeJobConflict as error:
        raise ProblemError("job-conflict", str(error)) from error
    return _job_accepted(response, job_id, status)


ACCESS_REQUEST_PATH = "/api/v1/access-requests"
CLAIM_SECRET_HINT = (
    "Send the claim_secret from create_access_request in the Claim-Secret header. A lost "
    "secret cannot be recovered: create a new request."
)


def _access_request_status(
    request: StoredAccessRequest, api_key: str | None = None
) -> AccessRequestStatus:
    return AccessRequestStatus(
        id=request.id,
        status=request.status,
        client_type=request.client_type,
        created_at=request.created_at,
        decided_at=request.decided_at,
        claim_expires_at=request.claim_expires_at,
        key_name=request.key_name,
        api_key=api_key,
    )


@app.post(
    ACCESS_REQUEST_PATH,
    operation_id="create_access_request",
    tags=[docs.TAG_ACCESS],
    summary="Ask the operator for an API key",
    description=(
        "Use when this instance is private (operations return `401`) or you need your own "
        "quota. Needs no key. The operator reviews every request; nothing is granted "
        "automatically. Store `claim_secret` at once (it is shown only here) and poll "
        "`get_access_request` with it."
    ),
    response_model=AccessRequestCreated,
    status_code=201,
    responses={
        201: {
            "description": "The request is stored and waits for the operator",
            "headers": docs.ACCESS_REQUEST_LOCATION_HEADER
            | docs.NO_STORE_HEADER
            | docs.RATE_LIMIT_HEADERS,
        },
        409: docs.error(
            "This contact email already has a pending request",
            pending=docs.example(
                "Pending request",
                "access-request-pending",
                "An access request for this contact email is already pending",
                ACCESS_REQUEST_PATH,
                parameter="contact_email",
                hint="Wait for the operator, and poll the pending request with its claim secret.",
            ),
        ),
        422: docs.error(
            "Unknown competition slugs (`invalid-parameter`), or request validation failed "
            "(`validation-error`)",
            unknown_competition=docs.example(
                "Unknown competition",
                "invalid-parameter",
                "Unknown competitions: premier-league-2",
                ACCESS_REQUEST_PATH,
                parameter="competitions",
                invalid=["premier-league-2"],
                valid_values=["ekstraklasa", "premier-league"],
            ),
            validation=docs.validation_example(
                ACCESS_REQUEST_PATH, ["body", "contact_email"], "Field required", "missing"
            ),
        ),
        429: docs.access_request_rate_limited(ACCESS_REQUEST_PATH),
        503: docs.error(
            "Access requests are disabled on the read-only demo instance",
            demo=docs.example(
                "Demo instance",
                "writes-disabled",
                "Access requests are disabled on the demo instance",
                ACCESS_REQUEST_PATH,
                hint=DEMO_HINT,
            ),
        ),
    },
)
async def create_access_request(
    body: AccessRequestCreate,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
) -> AccessRequestCreated:
    if settings.demo_mode:
        raise ProblemError(
            "writes-disabled",
            "Access requests are disabled on the demo instance",
            hint=DEMO_HINT,
        )
    if body.competitions:
        valid = sorted(item.slug for item in await store.list_competitions())
        unknown = sorted(set(body.competitions) - set(valid))
        if unknown:
            raise ProblemError(
                "invalid-parameter",
                "Unknown competitions: " + ", ".join(unknown),
                parameter="competitions",
                invalid=unknown,
                valid_values=valid,
            )
    if settings.rate_limit_enabled:
        decision = rate_limiter.hit(
            ACCESS_REQUEST_POLICY, "ip:" + _client_address(request), time.monotonic()
        )
        if not decision.allowed:
            raise ProblemError(
                "rate-limited",
                f"Rate limit exceeded: {ACCESS_REQUEST_POLICY.quota} "
                f"{ACCESS_REQUEST_POLICY.name} requests per "
                f"{ACCESS_REQUEST_POLICY.window_seconds} seconds",
                headers={**decision.headers(), "Retry-After": str(decision.reset_seconds)},
                hint=f"Retry after {decision.reset_seconds} seconds.",
            )
    claim_secret = access.generate_claim_secret()
    try:
        stored = await store.create_access_request(
            name=body.name,
            contact_email=body.contact_email,
            intended_use=body.intended_use,
            client_type=body.client_type,
            competitions=body.competitions or None,
            message=body.message or None,
            claim_secret_hash=access.hash_key(claim_secret),
        )
    except AccessRequestPending as error:
        raise ProblemError(
            "access-request-pending",
            "An access request for this contact email is already pending",
            parameter="contact_email",
            hint="Wait for the operator, and poll the pending request with its claim secret.",
        ) from error
    # After the response is sent: a slow or failing Telegram never affects the request.
    background_tasks.add_task(
        notify.notify_access_request,
        stored,
        notify.TelegramConfig.from_settings(settings),
        settings.public_base_url,
    )
    status_url = f"{ACCESS_REQUEST_PATH}/{stored.id}"
    response.headers["Location"] = status_url
    return AccessRequestCreated(
        id=stored.id,
        status=stored.status,
        claim_secret=claim_secret,
        status_url=status_url,
        created_at=stored.created_at,
    )


@app.get(
    ACCESS_REQUEST_PATH + "/{access_request_id}",
    operation_id="get_access_request",
    tags=[docs.TAG_ACCESS],
    summary="Check an access request and claim its key",
    description=(
        "Use to poll a request from `create_access_request` with its `Claim-Secret`. Once "
        "approved, the first call returns the key in `api_key` and the status becomes "
        "`claimed`; later calls never show it again. Needs no key."
    ),
    response_model=AccessRequestStatus,
    responses={
        200: {
            "description": "Successful Response",
            "headers": docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        404: docs.error(
            "Unknown request, or the claim secret does not match",
            unknown=docs.example(
                "Unknown access request",
                "unknown-access-request",
                "Unknown access request, or the claim secret does not match: 7",
                ACCESS_REQUEST_PATH + "/7",
                parameter="access_request_id",
                invalid=["7"],
                hint=CLAIM_SECRET_HINT,
            ),
        ),
        422: docs.request_validation(
            ACCESS_REQUEST_PATH + "/7",
            docs.validation_example(
                ACCESS_REQUEST_PATH + "/7", ["header", "Claim-Secret"], "Field required", "missing"
            ),
        ),
        429: docs.rate_limited(ACCESS_REQUEST_PATH + "/7", "read"),
    },
)
async def get_access_request(
    access_request_id: Annotated[
        int, Path(ge=1, description="Access request ID from `status_url`", examples=[7])
    ],
    claim_secret: Annotated[
        str,
        Header(
            alias="Claim-Secret",
            max_length=200,
            description="The `claim_secret` returned by `create_access_request`",
        ),
    ],
) -> AccessRequestStatus:
    stored = await store.get_access_request(access_request_id)
    if stored is None or not access.claim_secret_matches(claim_secret, stored.claim_secret_hash):
        raise ProblemError(
            "unknown-access-request",
            f"Unknown access request, or the claim secret does not match: {access_request_id}",
            parameter="access_request_id",
            invalid=[str(access_request_id)],
            hint=CLAIM_SECRET_HINT,
        )
    if stored.status != "approved":
        return _access_request_status(stored)
    api_key = await store.claim_access_request(access_request_id)
    claimed = await store.get_access_request(access_request_id)
    return _access_request_status(claimed or stored, api_key)


# Agent documentation: `/llms.txt`, `/llms-full.txt`, `/agents`, `/agents.md`, `/problems/...`,
# and the static Agent Skill at `agent_docs.SKILL_PATH`.
# Generated from the OpenAPI spec and the competitions; not part of the OpenAPI spec itself.


@app.get(
    "/api/v1/admin/scrape-runs",
    operation_id="list_scrape_runs",
    tags=[docs.TAG_ADMIN],
    summary="List recent import jobs",
    description=(
        "Use to find out why data is stale or an import failed: scheduled and requested import "
        "jobs, newest first, with their error message and scheduler worker. Filter by "
        "competition, season, and `status`."
    ),
    response_model=ScrapeRunsResponse,
    dependencies=[Depends(require_api_key)],
    responses={
        200: {
            "description": "Successful Response",
            "headers": docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        400: docs.invalid_cursor("/api/v1/admin/scrape-runs"),
        **docs.auth_errors("/api/v1/admin/scrape-runs"),
        404: docs.unknown_competition_or_season("/api/v1/admin/scrape-runs"),
        422: docs.error(
            "Unknown `status` (`invalid-parameter` with `valid_values`), or request validation "
            "failed (`validation-error`)",
            unknown_status=docs.example(
                "Unknown status",
                "invalid-parameter",
                "Unknown job status",
                "/api/v1/admin/scrape-runs",
                parameter="status",
                invalid=["done"],
                valid_values=list(SCRAPE_RUN_STATUSES),
            ),
            validation=docs.limit_validation_example("/api/v1/admin/scrape-runs"),
        ),
        429: docs.rate_limited("/api/v1/admin/scrape-runs", "read"),
    },
)
async def list_scrape_runs(
    competition: docs.CompetitionFilter = None,
    season: docs.SeasonFilter = None,
    status: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated job statuses: "
                + ", ".join(SCRAPE_RUN_STATUSES)
                + " (default: all)"
            ),
            examples=["failed", "queued,running"],
        ),
    ] = None,
    limit: docs.Limit = DEFAULT_LIMIT,
    cursor: docs.Cursor = None,
) -> ScrapeRunsResponse:
    statuses = _contract(lambda: parse_job_statuses(status))
    if competition:
        await _require_season(competition, season)
    competition = competition.casefold() if competition else None
    digest = filter_hash({"competition": competition, "season": season, "status": statuses})
    before_id = None
    if cursor is not None:
        before_id = _contract(lambda: id_keyset(cursor, digest))
    runs = await store.list_scrape_runs(competition, season, statuses, limit + 1, before_id)
    page = runs[:limit]
    next_cursor = encode_cursor([page[-1].job_id], digest) if len(runs) > limit else None
    return ScrapeRunsResponse(
        data=page, pagination=Pagination(limit=limit, next_cursor=next_cursor)
    )


@app.get(
    "/api/v1/admin/scheduler",
    operation_id="get_scheduler_status",
    tags=[docs.TAG_ADMIN],
    summary="Show the scheduler and the refresh state of current seasons",
    description=(
        "Use to check whether the scheduler is running and which current seasons are stale, "
        "overdue, or failing. `status` is `stalled` when no worker heartbeat is within the "
        "stall window; a season is `stale` when it was never fetched or its update is more "
        "than the grace period overdue."
    ),
    response_model=SchedulerStatus,
    dependencies=[Depends(require_api_key)],
    responses={
        200: {
            "description": "Successful Response",
            "headers": docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        **docs.auth_errors("/api/v1/admin/scheduler"),
        429: docs.rate_limited("/api/v1/admin/scheduler", "read"),
    },
)
async def get_scheduler_status() -> SchedulerStatus:
    now = datetime.now(UTC)
    window = observability.stall_window(settings.scheduler_loop_seconds)
    latest = await store.latest_scheduler_heartbeat()
    workers = await store.scheduler_workers(SCHEDULER_WORKERS_SHOWN)
    seasons = await store.scheduled_seasons()
    return SchedulerStatus(
        status=observability.scheduler_state(latest, now, window),
        checked_at=now,
        latest_heartbeat_at=latest,
        stall_after_seconds=window.total_seconds(),
        stale_grace_seconds=observability.STALE_GRACE.total_seconds(),
        workers=[observability.scheduler_worker(item, now, window) for item in workers],
        seasons=[observability.season_schedule(item, now) for item in seasons],
    )


ADMIN_ACCESS_REQUEST_PATH = "/api/v1/admin/access-requests"


def _access_request_details(request: StoredAccessRequest) -> AccessRequestDetails:
    return AccessRequestDetails(
        id=request.id,
        status=request.status,
        client_type=request.client_type,
        name=request.name,
        contact_email=request.contact_email,
        intended_use=request.intended_use,
        competitions=None if request.competitions is None else list(request.competitions),
        message=request.message,
        key_name=request.key_name,
        created_at=request.created_at,
        decided_at=request.decided_at,
        claim_expires_at=request.claim_expires_at,
        claimed_at=request.claimed_at,
    )


@app.get(
    ADMIN_ACCESS_REQUEST_PATH,
    operation_id="list_access_requests",
    tags=[docs.TAG_ADMIN],
    summary="List access requests",
    description=(
        "Use to review who asked for an API key: access requests, newest first, with their "
        "contact details and intended use. Filter by `status` (for example `pending`). "
        "Approve a request on the server with `python -m app.config access-requests approve`."
    ),
    response_model=AccessRequestsResponse,
    dependencies=[Depends(require_api_key)],
    responses={
        200: {
            "description": "Successful Response",
            "headers": docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        400: docs.invalid_cursor(ADMIN_ACCESS_REQUEST_PATH),
        **docs.auth_errors(ADMIN_ACCESS_REQUEST_PATH),
        422: docs.error(
            "Unknown `status` (`invalid-parameter` with `valid_values`), or request validation "
            "failed (`validation-error`)",
            unknown_status=docs.example(
                "Unknown status",
                "invalid-parameter",
                "Unknown access request status",
                ADMIN_ACCESS_REQUEST_PATH,
                parameter="status",
                invalid=["open"],
                valid_values=list(ACCESS_REQUEST_STATUSES),
            ),
            validation=docs.limit_validation_example(ADMIN_ACCESS_REQUEST_PATH),
        ),
        429: docs.rate_limited(ADMIN_ACCESS_REQUEST_PATH, "read"),
    },
)
async def list_access_requests(
    status: Annotated[
        str | None,
        Query(
            description=(
                "Comma-separated access request statuses: "
                + ", ".join(ACCESS_REQUEST_STATUSES)
                + " (default: all)"
            ),
            examples=["pending", "approved,claimed"],
        ),
    ] = None,
    limit: docs.Limit = DEFAULT_LIMIT,
    cursor: docs.Cursor = None,
) -> AccessRequestsResponse:
    statuses = _contract(lambda: parse_access_request_statuses(status))
    digest = filter_hash({"status": statuses})
    before_id = None
    if cursor is not None:
        before_id = _contract(lambda: id_keyset(cursor, digest))
    requests = await store.list_access_requests(statuses, limit + 1, before_id)
    page = requests[:limit]
    next_cursor = encode_cursor([page[-1].id], digest) if len(requests) > limit else None
    return AccessRequestsResponse(
        data=[_access_request_details(item) for item in page],
        pagination=Pagination(limit=limit, next_cursor=next_cursor),
    )


@app.post(
    ADMIN_ACCESS_REQUEST_PATH + "/{access_request_id}/reject",
    operation_id="reject_access_request",
    tags=[docs.TAG_ADMIN],
    summary="Reject a pending access request",
    description=(
        "Use to turn down a `pending` access request from `list_access_requests`; no key is "
        "issued. The requester sees `rejected` with `get_access_request`. The request is "
        "deleted with the other unapproved requests after 30 days."
    ),
    response_model=AccessRequestDetails,
    dependencies=[Depends(require_api_key)],
    responses={
        200: {
            "description": "The request is rejected",
            "headers": docs.NO_STORE_HEADER | docs.RATE_LIMIT_HEADERS,
        },
        **docs.auth_errors(ADMIN_ACCESS_REQUEST_PATH + "/7/reject"),
        404: docs.error(
            "Unknown access request",
            unknown=docs.example(
                "Unknown access request",
                "unknown-access-request",
                "Unknown access request: 7",
                ADMIN_ACCESS_REQUEST_PATH + "/7/reject",
                parameter="access_request_id",
                invalid=["7"],
                hint="Find the ID with list_access_requests.",
            ),
        ),
        409: docs.error(
            "The request is no longer pending",
            decided=docs.example(
                "Already decided",
                "access-request-decided",
                "Access request 7 is approved, not pending",
                ADMIN_ACCESS_REQUEST_PATH + "/7/reject",
                parameter="access_request_id",
                hint="Only pending requests can be rejected; check it with list_access_requests.",
            ),
        ),
        422: docs.request_validation(
            ADMIN_ACCESS_REQUEST_PATH + "/0/reject",
            docs.minimum_validation_example(
                ADMIN_ACCESS_REQUEST_PATH + "/0/reject", "path", "access_request_id"
            ),
        ),
        429: docs.rate_limited(ADMIN_ACCESS_REQUEST_PATH + "/7/reject", "write"),
    },
)
async def reject_access_request(
    access_request_id: Annotated[
        int, Path(ge=1, description="Access request ID from `list_access_requests`", examples=[7])
    ],
) -> AccessRequestDetails:
    try:
        rejected = await store.reject_access_request(access_request_id)
    except AccessRequestStateConflict as error:
        raise ProblemError(
            "access-request-decided",
            str(error),
            parameter="access_request_id",
            hint="Only pending requests can be rejected; check it with list_access_requests.",
        ) from error
    if rejected is None:
        raise ProblemError(
            "unknown-access-request",
            f"Unknown access request: {access_request_id}",
            parameter="access_request_id",
            invalid=[str(access_request_id)],
            hint="Find the ID with list_access_requests.",
        )
    return _access_request_details(rejected)


@app.get(METRICS_PATH, include_in_schema=False, dependencies=[Depends(require_metrics_key)])
async def metrics() -> Response:
    snapshot = observability.Snapshot(
        latest_heartbeat_at=await store.latest_scheduler_heartbeat(),
        seasons=await store.scheduled_seasons(),
        run_counts=await store.scrape_run_counts(),
    )
    body = observability.render_metrics(
        snapshot,
        datetime.now(UTC),
        observability.stall_window(settings.scheduler_loop_seconds),
        [(item.name, item.expires_at) for item in _current_keys() if item.expires_at is not None],
    )
    return Response(body, media_type=CONTENT_TYPE_LATEST, headers={"Cache-Control": "no-store"})


@app.get(oauth.METADATA_PATH, include_in_schema=False)
async def protected_resource_metadata() -> Response:
    """RFC 9728 metadata of `/mcp` when OAuth is configured (ADR 0006)."""
    config = oauth.OAuthConfig.from_settings(settings)
    if config is None:
        raise ProblemError(
            "not-found",
            "OAuth is not configured on this instance",
            hint="Use /mcp anonymously or with an X-API-Key header.",
        )
    return JSONResponse(
        config.metadata(documentation=f"{settings.public_base_url}/agents#mcp"),
        headers={
            "Cache-Control": cache_control(settings.cache_max_age_seconds),
            "Access-Control-Allow-Origin": "*",
        },
    )


def _public_base_url(request: Request) -> tuple[str, bool]:
    """The base URL for links and examples, and whether it was derived from the request."""
    if settings.public_base_url:
        return settings.public_base_url, False
    base_url = agent_docs.request_base_url(
        scheme=request.url.scheme,
        host=request.headers.get("host"),
        root_path=request.scope.get("root_path", ""),
        peer=request.client.host if request.client else None,
        forwarded_proto=request.headers.get("X-Forwarded-Proto"),
        forwarded_host=request.headers.get("X-Forwarded-Host"),
        trusted_proxies=_trusted_proxies(settings.trusted_proxies),
    )
    return base_url, True


def _page_headers(
    base_url: str, request_derived: bool, *, cacheable: bool = True, alternate: str | None = None
) -> dict[str, str]:
    headers = {
        "Link": agent_docs.link_header(base_url, alternate=alternate),
        "Cache-Control": (
            cache_control(settings.cache_max_age_seconds) if cacheable else "no-store"
        ),
    }
    if request_derived:
        headers["Vary"] = agent_docs.REQUEST_DERIVED_VARY
    return headers


async def _agent_guide(
    request: Request, *, competitions_needed: bool = True
) -> tuple[agent_docs.AgentGuide, str, bool]:
    base_url, request_derived = _public_base_url(request)
    competitions: list[CompetitionSummary] = []
    live = True
    if competitions_needed:
        try:
            competitions = await store.list_competitions()
        except (SQLAlchemyError, OSError):
            logger.warning("Agent docs use the configuration file: database unavailable")
            competitions = [
                competition_summary(item) for item in load_config().to_competitions().values()
            ]
            live = False
    guide = agent_docs.build_guide(
        app.openapi(),
        competitions,
        base_url=base_url,
        competitions_live=live,
        rate_limits=agent_docs.RateLimits(
            settings.rate_limit_enabled,
            settings.rate_limit_read_per_minute,
            settings.rate_limit_write_per_minute,
        ),
        cache_max_age_seconds=settings.cache_max_age_seconds,
        tools=(
            agent_docs.cli_tool(base_url),
            agent_docs.skill_tool(base_url),
            agent_docs.mcp_tool(base_url),
        ),
        provider_competitions=_configured_provider_competitions(),
        oauth=_oauth_info(),
        demo=settings.demo_mode,
        key_required=not settings.anonymous_access,
    )
    return guide, base_url, request_derived


def _oauth_info() -> agent_docs.OAuthInfo | None:
    config = oauth.OAuthConfig.from_settings(settings)
    if config is None:
        return None
    return agent_docs.OAuthInfo(
        config.issuer,
        config.audience,
        config.metadata_url,
        config.mode == "required" or not settings.anonymous_access,
        config.scopes,
    )


def _configured_provider_competitions() -> dict[str, list[str]]:
    try:
        return load_config().competitions_by_provider()
    except (TypeError, ValueError):
        logger.warning("Data attributions omitted: the configuration file is invalid")
        return {}


def _markdown(content: str, headers: dict[str, str]) -> Response:
    return Response(content, media_type=agent_docs.MARKDOWN_MEDIA_TYPE, headers=headers)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt(request: Request) -> Response:
    guide, base_url, derived = await _agent_guide(request)
    headers = _page_headers(base_url, derived, cacheable=guide.competitions_live)
    return _markdown(agent_docs.llms_txt(guide), headers)


@app.get("/llms-full.txt", include_in_schema=False)
async def llms_full_txt(request: Request) -> Response:
    guide, base_url, derived = await _agent_guide(request)
    headers = _page_headers(base_url, derived, cacheable=guide.competitions_live)
    return _markdown(agent_docs.llms_full_txt(guide), headers)


@app.get("/agents.md", include_in_schema=False)
async def agents_markdown(request: Request) -> Response:
    guide, base_url, derived = await _agent_guide(request)
    headers = _page_headers(base_url, derived, cacheable=guide.competitions_live)
    return _markdown(agent_docs.agent_guide_markdown(guide), headers)


@app.get("/agents", include_in_schema=False)
async def agents_page(request: Request) -> Response:
    guide, base_url, derived = await _agent_guide(request)
    headers = _page_headers(
        base_url, derived, cacheable=guide.competitions_live, alternate="/agents.md"
    )
    return Response(
        agent_docs.agent_guide_html(guide), media_type=agent_docs.HTML_MEDIA_TYPE, headers=headers
    )


@lru_cache(maxsize=1)
def _skill_file() -> bytes:
    return agent_docs.SKILL_FILE.read_bytes()


@app.get(agent_docs.SKILL_PATH, include_in_schema=False)
async def skill_file(request: Request) -> Response:
    """The consumer Agent Skill, byte for byte as in the repository."""
    base_url, derived = _public_base_url(request)
    return Response(
        _skill_file(),
        media_type=agent_docs.MARKDOWN_MEDIA_TYPE,
        headers=_page_headers(base_url, derived),
    )


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request) -> Response:
    base_url, derived = _public_base_url(request)
    return Response(
        agent_docs.robots_txt(base_url, crawlable=settings.anonymous_access),
        media_type="text/plain; charset=utf-8",
        headers=_page_headers(base_url, derived),
    )


def _privacy_blocks(base_url: str) -> list[agent_docs.Block]:
    return agent_docs.privacy_blocks(
        agent_docs.PrivacyInfo(
            operator_contact=settings.operator_contact,
            log_retention_days=settings.log_retention_days,
            ips_anonymized=settings.log_ips_anonymized,
            rate_limited=settings.rate_limit_enabled,
            keys_enabled=bool(settings.api_keys or settings.api_keys_file),
            oauth_enabled=settings.oauth_issuer is not None,
            # Access requests are writes: the demo refuses them (`writes-disabled`).
            access_requests=None
            if settings.demo_mode
            else agent_docs.AccessRequestPrivacy(
                unapproved_retention=UNAPPROVED_RETENTION,
                claim_window=CLAIM_WINDOW,
                telegram_notifications=notify.TelegramConfig.from_settings(settings) is not None,
            ),
        ),
        base_url,
    )


@app.get("/privacy.md", include_in_schema=False)
async def privacy_markdown(request: Request) -> Response:
    base_url, derived = _public_base_url(request)
    return _markdown(
        agent_docs.render_markdown(_privacy_blocks(base_url)), _page_headers(base_url, derived)
    )


@app.get("/privacy", include_in_schema=False)
async def privacy_page(request: Request) -> Response:
    base_url, derived = _public_base_url(request)
    html = agent_docs.render_html(
        _privacy_blocks(base_url),
        title="Privacy",
        links={"alternate": f"{base_url}/privacy.md", "describedby": f"{base_url}/llms.txt"},
    )
    return Response(
        html,
        media_type=agent_docs.HTML_MEDIA_TYPE,
        headers=_page_headers(base_url, derived, alternate="/privacy.md"),
    )


@app.get("/problems", include_in_schema=False)
async def problem_index(request: Request) -> Response:
    guide, base_url, derived = await _agent_guide(request, competitions_needed=False)
    return _markdown(agent_docs.problem_index_markdown(guide), _page_headers(base_url, derived))


@app.get("/problems/{slug}", include_in_schema=False)
async def problem_page(request: Request, slug: str) -> Response:
    if slug not in PROBLEM_TYPES:
        raise ProblemError(
            "not-found",
            f"Unknown problem type: {slug}",
            parameter="slug",
            invalid=[slug],
            valid_values=list(PROBLEM_TYPES),
        )
    guide, base_url, derived = await _agent_guide(request, competitions_needed=False)
    return _markdown(agent_docs.problem_markdown(guide, slug), _page_headers(base_url, derived))
