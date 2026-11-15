from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version

from fastapi import FastAPI, HTTPException, Query

from app.competitions import COMPETITIONS, source_from_url
from app.config import settings
from app.database import session_factory
from app.models import (
    CompetitionSummary,
    HistoricalFetchRequest,
    RefreshRequest,
    ResultsResponse,
)
from app.repository import PostgresResultRepository
from app.scraper import create_scraper_registry

store = PostgresResultRepository(session_factory)
scraper = create_scraper_registry(
    settings.request_timeout_seconds,
    settings.user_agent,
    settings.football_data_api_token,
    settings.football_data_min_request_interval_seconds,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await store.seed_competitions(COMPETITIONS)
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
    season: str | None = Query(default=None, pattern=r"^\d{4}-\d{4}$"),
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


@app.post("/api/v1/results/refresh", response_model=ResultsResponse)
async def refresh_results(request: RefreshRequest) -> ResultsResponse:
    competition = await store.get_competition(request.competition)
    if competition is None:
        raise HTTPException(status_code=404, detail=f"Unknown competition: {request.competition}")
    season = request.season or competition.current_season
    if season != competition.current_season:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Refresh supports only the current season {competition.current_season}; "
                "use /api/v1/results/history/fetch for historical seasons"
            ),
        )
    if (
        request.first_round is not None
        and request.last_round is not None
        and request.first_round > request.last_round
    ):
        raise HTTPException(status_code=422, detail="first_round cannot exceed last_round")
    if (request.first_round is None) != (request.last_round is None):
        raise HTTPException(
            status_code=422,
            detail="first_round and last_round must be provided together",
        )
    if competition.format != "league" and request.first_round is not None:
        raise HTTPException(
            status_code=422,
            detail="Round ranges are available only for league competitions",
        )
    source = competition.seasons[competition.current_season]
    try:
        all_results = await scraper.scrape(competition, source)
    except Exception as error:
        await store.record_scrape_failure(competition, source, error)
        raise HTTPException(status_code=502, detail=f"Source refresh failed: {error}") from error
    refreshed_at = await store.replace_competition(competition, source, all_results)
    fresh = [result for item in all_results if (result := item.as_result()) is not None]
    if request.first_round is not None and request.last_round is not None:
        fresh = [
            item
            for item in fresh
            if item.round is not None and request.first_round <= item.round <= request.last_round
        ]
    return ResultsResponse(
        results=fresh,
        count=len(fresh),
        refreshed_at=refreshed_at,
        stale=False,
    )


@app.post("/api/v1/results/history/fetch", response_model=ResultsResponse)
async def fetch_historical_results(request: HistoricalFetchRequest) -> ResultsResponse:
    competition = await store.get_competition(request.competition)
    if competition is None:
        raise HTTPException(status_code=404, detail=f"Unknown competition: {request.competition}")
    if request.season == competition.current_season:
        raise HTTPException(
            status_code=422,
            detail="Use /api/v1/results/refresh for the current season",
        )
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
    if not request.force_refresh and await store.is_cached(
        competition.slug,
        request.season,
    ):
        cached, refreshed_at = await store.get_results(
            competition=competition.slug,
            season=request.season,
        )
        return ResultsResponse(
            results=cached,
            count=len(cached),
            refreshed_at=refreshed_at,
            stale=False,
            fetched=False,
        )
    try:
        fresh = await scraper.scrape(competition, source)
    except Exception as error:
        await store.record_scrape_failure(competition, source, error)
        raise HTTPException(status_code=502, detail=f"Source fetch failed: {error}") from error
    refreshed_at = await store.replace_competition(competition, source, fresh)
    results = [result for item in fresh if (result := item.as_result()) is not None]
    return ResultsResponse(
        results=results,
        count=len(results),
        refreshed_at=refreshed_at,
        stale=False,
        fetched=True,
    )
