import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.security import APIKeyHeader

from app.competitions import provider_competition_code, source_from_url
from app.config import load_config, settings
from app.database import session_factory
from app.models import (
    CompetitionSummary,
    HistoricalFetchRequest,
    JobAccepted,
    RefreshRequest,
    ResultsResponse,
    ScrapeJobStatus,
)
from app.repository import ActiveScrapeJobConflict, PostgresResultRepository
from app.seasons import SEASON_PATTERN, validate_season

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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    config = load_config()
    await store.seed_competitions(config.to_competitions())
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
