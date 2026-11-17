import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from app import api_docs as docs
from app.competitions import provider_competition_code, source_from_url
from app.config import load_config, settings
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
    Competition,
    CompetitionSummary,
    HealthStatus,
    HistoricalFetchRequest,
    JobAccepted,
    Match,
    MatchesResponse,
    MatchQuery,
    MessageError,
    Pagination,
    ParameterError,
    RefreshRequest,
    RequestValidationErrorResponse,
    ResultsResponse,
    ScrapeJobStatus,
    StandingRow,
    StandingsResponse,
    TeamsResponse,
)
from app.repository import ActiveScrapeJobConflict, PostgresResultRepository
from app.seasons import SEASON_PATTERN, validate_season
from app.standings import compute_standings

store = PostgresResultRepository(session_factory)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key: str | None = Depends(api_key_header)) -> None:
    configured_keys = tuple(key.strip() for key in settings.api_keys.split(",") if key.strip())
    if not configured_keys:
        raise HTTPException(status_code=503, detail="Write endpoints are disabled")
    supplied_key = api_key or ""
    authenticated = False
    for configured_key in configured_keys:
        authenticated = secrets.compare_digest(supplied_key, configured_key) | authenticated
    if not authenticated:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def _job_accepted(response: Response, job_id: int, status: str) -> JobAccepted:
    status_url = f"/api/v1/jobs/{job_id}"
    response.headers["Location"] = status_url
    return JobAccepted(job_id=job_id, status=status, status_url=status_url)


def _contract[T](parse: Callable[[], T]) -> T:
    try:
        return parse()
    except ContractError as error:
        raise HTTPException(status_code=error.status_code, detail=error.detail) from error


async def _require_season(slug: str, season: str | None) -> Competition:
    competition = await store.get_competition(slug)
    if competition is None:
        raise HTTPException(status_code=404, detail=f"Unknown competition: {slug}")
    if season is not None and season not in competition.seasons:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"Unknown season {season} for {competition.slug}",
                "parameter": "season",
                "valid_values": sorted(competition.seasons, reverse=True),
            },
        )
    return competition


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
        503: docs.error(
            "The database is unreachable",
            MessageError,
            database=docs.example("Database down", "Database unavailable"),
        )
    },
)
async def health() -> dict[str, str]:
    try:
        await store.health()
    except Exception as error:
        raise HTTPException(status_code=503, detail="Database unavailable") from error
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
        404: docs.error(
            "Unknown competition",
            MessageError,
            unknown_competition=docs.UNKNOWN_COMPETITION_EXAMPLE,
        ),
        422: docs.REQUEST_VALIDATION,
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
        raise HTTPException(status_code=404, detail=f"Unknown competition: {competition}")
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
        400: docs.INVALID_CURSOR,
        404: docs.UNKNOWN_COMPETITION_OR_SEASON,
        422: docs.error(
            "Invalid `fields`, `status`, or date (with `invalid` and `valid_values` or "
            "`examples`), or request validation failed",
            ParameterError | RequestValidationErrorResponse,
            unknown_fields=docs.unknown_fields_example(list(Match.model_fields)),
            unknown_status=docs.example(
                "Unknown status",
                {
                    "message": "Unknown match status",
                    "parameter": "status",
                    "invalid": ["played"],
                    "valid_values": ["finished", "scheduled", "inactive"],
                },
            ),
            invalid_date=docs.example(
                "Invalid date",
                {
                    "message": "date_from must be an ISO 8601 date or datetime",
                    "parameter": "date_from",
                    "invalid": ["01.08.2026"],
                    "examples": ["2026-08-01", "2026-08-01T18:00:00+02:00"],
                },
            ),
            validation=docs.VALIDATION_EXAMPLE,
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
        }
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
        404: docs.UNKNOWN_COMPETITION_OR_SEASON,
        422: docs.error(
            "Invalid `fields`, a cup without `stage`, or a competition without a current "
            "season (each with `valid_values`), or request validation failed",
            ParameterError | RequestValidationErrorResponse,
            unknown_fields=docs.unknown_fields_example(list(StandingRow.model_fields)),
            stage_required=docs.example(
                "Cup needs a stage",
                {
                    "message": (
                        "Standings for a cup competition need a stage (for example a group)"
                    ),
                    "parameter": "stage",
                    "valid_values": ["Faza ligowa", "Runda eliminacyjna"],
                },
            ),
            validation=docs.example(
                "Missing competition",
                [{"loc": ["query", "competition"], "msg": "Field required", "type": "missing"}],
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
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"{found.slug} has no current season; pass season",
                "parameter": "season",
                "valid_values": sorted(found.seasons, reverse=True),
            },
        )
    stage = stage.strip() if stage else None
    if found.format == "cup" and not stage:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Standings for a cup competition need a stage (for example a group)",
                "parameter": "stage",
                "valid_values": await store.list_stages(found.slug, season),
            },
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
        }
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
        400: docs.INVALID_CURSOR,
        404: docs.UNKNOWN_COMPETITION_OR_SEASON,
        422: docs.REQUEST_VALIDATION,
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
        202: {"description": "The refresh is queued", "headers": docs.LOCATION_HEADER},
        **docs.AUTH_ERRORS,
        404: docs.error(
            "Unknown competition",
            MessageError,
            unknown_competition=docs.UNKNOWN_COMPETITION_EXAMPLE,
        ),
        409: docs.JOB_CONFLICT,
        422: docs.error(
            "Not the current season, no current season configured, or request validation failed",
            MessageError | RequestValidationErrorResponse,
            not_current=docs.example(
                "Historical season",
                "Refresh supports only the current season 2026-2027; "
                "use /api/v1/results/history/fetch for historical seasons",
            ),
            validation=docs.example(
                "Missing competition",
                [{"loc": ["body", "competition"], "msg": "Field required", "type": "missing"}],
            ),
        ),
    },
)
async def refresh_results(request: RefreshRequest, response: Response) -> JobAccepted:
    competition = await store.get_competition(request.competition)
    if competition is None:
        raise HTTPException(status_code=404, detail=f"Unknown competition: {request.competition}")
    if competition.current_season is None:
        raise HTTPException(status_code=422, detail="No current season is configured")
    season = request.season or competition.current_season
    if season != competition.current_season:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Refresh supports only the current season {competition.current_season}; "
                "use /api/v1/results/history/fetch for historical seasons"
            ),
        )
    source = competition.seasons[competition.current_season]
    try:
        job_id, status = await store.enqueue_scrape(competition, source)
    except ActiveScrapeJobConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
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
        **docs.AUTH_ERRORS,
        404: docs.error(
            "Unknown job", MessageError, unknown_job=docs.example("Unknown job", "Unknown job: 42")
        ),
        422: docs.REQUEST_VALIDATION,
    },
)
async def get_job_status(
    job_id: Annotated[int, Path(ge=1, description="Job ID from `status_url`", examples=[42])],
) -> ScrapeJobStatus:
    job = await store.get_scrape_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
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
        200: {"model": ResultsResponse, "description": "The season is already stored"},
        202: {
            "model": JobAccepted,
            "description": "The fetch is queued",
            "headers": docs.LOCATION_HEADER,
        },
        **docs.AUTH_ERRORS,
        404: docs.error(
            "Unknown competition",
            MessageError,
            unknown_competition=docs.UNKNOWN_COMPETITION_EXAMPLE,
        ),
        409: docs.JOB_CONFLICT,
        422: docs.error(
            "The season or source is not acceptable for this competition, or request "
            "validation failed",
            MessageError | RequestValidationErrorResponse,
            missing_source=docs.example(
                "No source",
                "No source configured for ekstraklasa 2019-2020; provide source_url",
            ),
            current_season=docs.example(
                "Current season", "Use /api/v1/results/refresh for the current season"
            ),
            validation=docs.example(
                "Bad season format",
                [
                    {
                        "loc": ["body", "season"],
                        "msg": f"String should match pattern '{SEASON_PATTERN}'",
                        "type": "string_pattern_mismatch",
                    }
                ],
            ),
        ),
    },
)
async def fetch_historical_results(
    request: HistoricalFetchRequest, response: Response
) -> JobAccepted | ResultsResponse:
    competition = await store.get_competition(request.competition)
    if competition is None:
        raise HTTPException(status_code=404, detail=f"Unknown competition: {request.competition}")
    if request.season == competition.current_season:
        raise HTTPException(
            status_code=422,
            detail="Use /api/v1/results/refresh for the current season",
        )
    try:
        validate_season(request.season, competition.season_format)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if request.source_url is not None:
        try:
            source = source_from_url(request.season, request.source_url)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    else:
        source = competition.seasons.get(request.season)
    if source is not None and source.provider not in {
        item.provider for item in competition.seasons.values()
    }:
        raise HTTPException(
            status_code=422,
            detail=f"Provider {source.provider} is not configured for {competition.slug}",
        )
    if source is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No source configured for {competition.slug} {request.season}; provide source_url"
            ),
        )
    configured_codes = {
        code
        for item in competition.seasons.values()
        if (code := provider_competition_code(item)) is not None
    }
    source_code = provider_competition_code(source)
    if configured_codes and source_code is not None and source_code not in configured_codes:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Provider competition code {source_code} is not configured for {competition.slug}"
            ),
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
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _job_accepted(response, job_id, status)
