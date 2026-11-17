import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from app.competitions import provider_competition_code, source_from_url
from app.config import load_config, settings
from app.contract import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
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
    title="Football Results API", version=version("football-results-api"), lifespan=lifespan
)


@app.get("/health")
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


@app.get("/api/v1/competitions", response_model=list[CompetitionSummary])
async def get_competitions() -> list[CompetitionSummary]:
    return await store.list_competitions()


@app.get("/api/v1/results", response_model=ResultsResponse)
async def get_results(
    competition: str | None = None,
    season: str | None = Query(default=None, pattern=SEASON_PATTERN),
    round_number: int | None = Query(default=None, alias="round", ge=1),
    stage: str | None = None,
    team: str | None = None,
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


@app.get("/api/v1/matches", response_model=MatchesResponse)
async def get_matches(
    competition: str | None = None,
    season: str | None = Query(default=None, pattern=SEASON_PATTERN),
    status: str | None = Query(
        default=None, description="Comma-separated: finished, scheduled, inactive"
    ),
    date_from: str | None = Query(default=None, description="ISO date or datetime, inclusive"),
    date_to: str | None = Query(default=None, description="ISO date or datetime, inclusive"),
    team: str | None = Query(default=None, description="Canonical team slug, or name substring"),
    round_number: int | None = Query(default=None, alias="round", ge=1),
    stage: str | None = None,
    order: Literal["asc", "desc"] = "asc",
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
    fields: str | None = Query(default=None, description="Comma-separated Match fields"),
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


@app.get("/api/v1/standings", response_model=StandingsResponse)
async def get_standings(
    competition: str,
    season: str | None = Query(default=None, pattern=SEASON_PATTERN),
    stage: str | None = Query(
        default=None, description="Stage substring; required for cup competitions"
    ),
    fields: str | None = Query(default=None, description="Comma-separated StandingRow fields"),
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


@app.get("/api/v1/teams", response_model=TeamsResponse)
async def get_teams(
    competition: str | None = None,
    season: str | None = Query(default=None, pattern=SEASON_PATTERN),
    q: str | None = Query(default=None, description="Canonical team slug, or name substring"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
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
    response_model=JobAccepted,
    status_code=202,
    dependencies=[Depends(require_api_key)],
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
    response_model=ScrapeJobStatus,
    dependencies=[Depends(require_api_key)],
)
async def get_job_status(job_id: int = Path(ge=1)) -> ScrapeJobStatus:
    job = await store.get_scrape_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
    return job


@app.post(
    "/api/v1/results/history/fetch",
    response_model=JobAccepted | ResultsResponse,
    status_code=202,
    responses={200: {"model": ResultsResponse}},
    dependencies=[Depends(require_api_key)],
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
