import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib.metadata import version
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from sqlalchemy.exc import SQLAlchemyError

from app import agent_docs
from app import api_docs as docs
from app.caching import (
    CACHEABLE_OPERATIONS,
    CACHEABLE_PAGES,
    cache_control,
    etag_for,
    if_none_match,
    max_age_seconds,
)
from app.competitions import competition_summary, provider_competition_code, source_from_url
from app.config import load_config, parse_networks, settings
from app.contract import (
    DEFAULT_LIMIT,
    ContractError,
    compact,
    encode_cursor,
    filter_hash,
    match_keyset,
    parse_date_bound,
    parse_fields,
    parse_statuses,
    slug_keyset,
)
from app.database import session_factory
from app.models import (
    PROBLEM_TYPES,
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
    ScrapeJobStatus,
    StandingRow,
    StandingsResponse,
    TeamsResponse,
)
from app.problems import ProblemError, install_problem_handlers, problem_response
from app.ratelimit import (
    EXEMPT_PATHS,
    READ_METHODS,
    FixedWindowRateLimiter,
    RateLimitPolicy,
    api_key_client,
    client_ip,
)
from app.repository import ActiveScrapeJobConflict, PostgresResultRepository
from app.seasons import SEASON_PATTERN, validate_season
from app.standings import compute_standings

logger = logging.getLogger(__name__)
store = PostgresResultRepository(session_factory)
rate_limiter = FixedWindowRateLimiter()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _configured_api_keys() -> tuple[str, ...]:
    return tuple(key.strip() for key in settings.api_keys.split(",") if key.strip())


def _is_valid_api_key(supplied_key: str, configured_keys: tuple[str, ...]) -> bool:
    authenticated = False
    for configured_key in configured_keys:
        authenticated = secrets.compare_digest(supplied_key, configured_key) | authenticated
    return authenticated


async def require_api_key(api_key: str | None = Depends(api_key_header)) -> None:
    configured_keys = _configured_api_keys()
    if not configured_keys:
        raise ProblemError(
            "writes-disabled",
            "Write endpoints are disabled",
            hint="The operator must set FOOTBALL_RESULTS_API_KEYS.",
        )
    if not _is_valid_api_key(api_key or "", configured_keys):
        raise ProblemError(
            "unauthorized",
            "Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
            hint="Send a configured API key in the X-API-Key header.",
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
    config = load_config()
    await store.seed_competitions(config.to_competitions(), config.auto_rollover_slugs())
    yield


app = FastAPI(
    title="Football Results API",
    version=version("football-results-api"),
    description=docs.API_DESCRIPTION,
    openapi_tags=docs.OPENAPI_TAGS,
    lifespan=lifespan,
)
install_problem_handlers(app)
docs.install_openapi(app)


_trusted_proxies = lru_cache(maxsize=8)(parse_networks)


def _rate_limit_client(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    configured_keys = _configured_api_keys()
    if api_key and configured_keys and _is_valid_api_key(api_key, configured_keys):
        return api_key_client(api_key)
    peer = request.client.host if request.client else None
    forwarded = request.headers.getlist("X-Forwarded-For")
    return "ip:" + client_ip(peer, forwarded, _trusted_proxies(settings.trusted_proxies))


def _rate_limit_policy(method: str) -> RateLimitPolicy:
    if method in READ_METHODS:
        return RateLimitPolicy("read", settings.rate_limit_read_per_minute, 60)
    return RateLimitPolicy("write", settings.rate_limit_write_per_minute, 60)


@app.middleware("http")
async def rate_limit_and_cache(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    rate_headers: dict[str, str] = {}
    if settings.rate_limit_enabled and request.url.path not in EXEMPT_PATHS:
        decision = rate_limiter.hit(
            _rate_limit_policy(request.method), _rate_limit_client(request), time.monotonic()
        )
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

    response = await call_next(request)
    response.headers.update(rate_headers)
    route = request.scope.get("route")
    operation_id = getattr(route, "operation_id", None)
    cacheable = (
        operation_id in CACHEABLE_OPERATIONS or getattr(route, "path", None) in CACHEABLE_PAGES
    )
    if response.status_code >= 400 or (operation_id is not None and not cacheable):
        response.headers["Cache-Control"] = "no-store"
        return response
    if not cacheable or request.method != "GET" or response.status_code != 200:
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[attr-defined]
    etag = etag_for(body)
    headers = {key: value for key, value in response.headers.items() if key != "content-length"}
    headers["ETag"] = etag
    headers.setdefault("cache-control", cache_control(settings.cache_max_age_seconds))
    if if_none_match(request.headers.get("If-None-Match"), etag):
        headers.pop("content-type", None)
        return Response(status_code=304, headers=headers)
    return Response(content=body, status_code=200, headers=headers)


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
    heartbeat = await store.latest_scheduler_heartbeat()
    scheduler_status = "not_started"
    if heartbeat is not None:
        stale_after = timedelta(seconds=max(settings.scheduler_loop_seconds * 4, 120))
        scheduler_status = "ok" if datetime.now(UTC) - heartbeat <= stale_after else "stale"
    return {"status": "ok", "database": "ok", "scheduler": scheduler_status}


@app.get(
    "/api/v1/competitions",
    operation_id="list_competitions",
    tags=[docs.TAG_COMPETITIONS],
    summary="List competitions and their seasons",
    description=(
        "Use first to discover competition slugs, the current season, available and fetched "
        "seasons, the season format, and scheduler state. Not paginated."
    ),
    response_model=list[CompetitionSummary],
    responses=docs.cached_read("/api/v1/competitions"),
)
async def get_competitions() -> list[CompetitionSummary]:
    return await store.list_competitions()


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


# Agent documentation: `/llms.txt`, `/llms-full.txt`, `/agents`, `/agents.md`, `/problems/...`.
# Generated from the OpenAPI spec and the competitions; not part of the OpenAPI spec itself.


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
    )
    return guide, base_url, request_derived


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
