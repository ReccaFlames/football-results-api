import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db_models import (
    CompetitionRow,
    CompetitionSeasonRow,
    MatchRow,
    SchedulerHeartbeatRow,
    ScrapeRunRow,
)
from app.models import (
    Competition,
    CompetitionSummary,
    MatchResult,
    ScrapedMatch,
    ScrapeJobStatus,
    SeasonSource,
)
from app.scheduling import calculate_next_check, failure_backoff


def _now() -> datetime:
    return datetime.now(UTC)


def _source_key(match: ScrapedMatch) -> str:
    identity = (
        f"{match.season}|{match.stage}|{match.home_team}|{match.away_team}|"
        f"{match.played_at.isoformat()}"
    )
    return hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class RefreshTarget:
    competition: Competition
    source: SeasonSource
    run_id: int | None = None


class ActiveScrapeJobConflict(ValueError):
    pass


class PostgresResultRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def health(self) -> None:
        async with self.sessions() as session:
            await session.execute(select(1))

    async def seed_competitions(self, seeds: dict[str, Competition]) -> None:
        async with self.sessions.begin() as session:
            now = _now()
            existing_rows = (
                await session.scalars(
                    select(CompetitionRow).options(selectinload(CompetitionRow.seasons))
                )
            ).all()
            for existing_row in existing_rows:
                if existing_row.slug in seeds or not existing_row.is_enabled:
                    continue
                existing_row.is_enabled = False
                existing_row.updated_at = now
                for season in existing_row.seasons:
                    season.is_current = False
                    season.automatic_refresh_enabled = False
                    season.scheduler_state = "disabled"
                    season.next_check_at = None
                    season.updated_at = now
                await session.execute(
                    update(ScrapeRunRow)
                    .where(
                        ScrapeRunRow.status == "queued",
                        ScrapeRunRow.competition_season_id.in_(
                            select(CompetitionSeasonRow.id).where(
                                CompetitionSeasonRow.competition_id == existing_row.id
                            )
                        ),
                    )
                    .values(
                        status="cancelled",
                        completed_at=now,
                        error_message="Competition removed from configuration",
                    )
                )
            for seed in seeds.values():
                row = await session.scalar(
                    select(CompetitionRow).where(CompetitionRow.slug == seed.slug)
                )
                if row is None:
                    row = CompetitionRow(
                        slug=seed.slug,
                        name=seed.name,
                        country=seed.country,
                        format=seed.format,
                        is_enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                    await session.flush()
                else:
                    row.is_enabled = True
                    row.name, row.country, row.format, row.updated_at = (
                        seed.name,
                        seed.country,
                        seed.format,
                        now,
                    )
                seasons = (
                    await session.scalars(
                        select(CompetitionSeasonRow).where(
                            CompetitionSeasonRow.competition_id == row.id
                        )
                    )
                ).all()
                existing = {item.season: item for item in seasons}
                for item in seasons:
                    item.is_current = False
                await session.flush()
                for season_name, source in seed.seasons.items():
                    is_current = season_name == seed.current_season
                    item = existing.get(season_name)
                    if item is None:
                        session.add(
                            CompetitionSeasonRow(
                                competition_id=row.id,
                                season=season_name,
                                source_url=source.source_url,
                                provider=source.provider,
                                is_current=is_current,
                                fetched_at=None,
                                automatic_refresh_enabled=is_current,
                                scheduler_state="waiting_for_schedule"
                                if is_current
                                else "disabled",
                                next_check_at=now if is_current else None,
                                consecutive_failures=0,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                    else:
                        source_changed = (
                            item.source_url != source.source_url or item.provider != source.provider
                        )
                        item.is_current = is_current
                        item.source_url = source.source_url
                        item.provider = source.provider
                        item.updated_at = now
                        if is_current:
                            item.automatic_refresh_enabled = True
                            if source_changed:
                                item.next_check_at = now
                                item.consecutive_failures = 0
                                item.scheduler_state = "waiting_for_schedule"
                            else:
                                item.next_check_at = item.next_check_at or now
                            if item.scheduler_state == "disabled":
                                item.scheduler_state = "waiting_for_schedule"

    async def get_competition(self, slug: str) -> Competition | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(CompetitionRow)
                .where(CompetitionRow.slug == slug.casefold(), CompetitionRow.is_enabled.is_(True))
                .options(selectinload(CompetitionRow.seasons))
            )
            return None if row is None else self._competition(row)

    async def list_competitions(self) -> list[CompetitionSummary]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(CompetitionRow)
                    .where(CompetitionRow.is_enabled.is_(True))
                    .options(selectinload(CompetitionRow.seasons))
                    .order_by(CompetitionRow.name)
                )
            ).all()
            result = []
            for row in rows:
                current = next((item for item in row.seasons if item.is_current), None)
                result.append(
                    CompetitionSummary(
                        slug=row.slug,
                        name=row.name,
                        country=row.country,
                        format=row.format,
                        current_season=None if current is None else current.season,
                        available_seasons=sorted((s.season for s in row.seasons), reverse=True),
                        fetched_seasons=sorted(
                            (s.season for s in row.seasons if s.fetched_at), reverse=True
                        ),
                        automatic_refresh_enabled=(
                            False if current is None else current.automatic_refresh_enabled
                        ),
                        scheduler_state=None if current is None else current.scheduler_state,
                        next_check_at=None if current is None else current.next_check_at,
                    )
                )
            return result

    async def get_results(
        self,
        competition: str | None = None,
        season: str | None = None,
        round_number: int | None = None,
        stage: str | None = None,
        team: str | None = None,
    ) -> tuple[list[MatchResult], datetime | None]:
        query = (
            select(MatchRow, CompetitionRow.slug, CompetitionSeasonRow.season)
            .select_from(MatchRow)
            .join(CompetitionSeasonRow, MatchRow.competition_season_id == CompetitionSeasonRow.id)
            .join(CompetitionRow, CompetitionSeasonRow.competition_id == CompetitionRow.id)
            .where(MatchRow.status == "finished", CompetitionRow.is_enabled.is_(True))
            .order_by(MatchRow.played_at, MatchRow.id)
        )
        if competition:
            query = query.where(CompetitionRow.slug == competition.casefold())
        if season:
            query = query.where(CompetitionSeasonRow.season == season)
        if round_number:
            query = query.where(MatchRow.round == round_number)
        if stage:
            query = query.where(MatchRow.stage.ilike(f"%{stage}%"))
        if team:
            query = query.where(
                or_(MatchRow.home_team.ilike(f"%{team}%"), MatchRow.away_team.ilike(f"%{team}%"))
            )
        async with self.sessions() as session:
            rows = (await session.execute(query)).all()
            refresh = (
                select(func.max(CompetitionSeasonRow.fetched_at))
                .select_from(CompetitionSeasonRow)
                .join(CompetitionRow, CompetitionSeasonRow.competition_id == CompetitionRow.id)
                .where(CompetitionRow.is_enabled.is_(True))
            )
            if competition:
                refresh = refresh.where(CompetitionRow.slug == competition.casefold())
            if season:
                refresh = refresh.where(CompetitionSeasonRow.season == season)
            refreshed_at = await session.scalar(refresh)
        results = [
            MatchResult(
                competition=slug,
                season=season_name,
                stage=row.stage,
                round=row.round,
                home_team=row.home_team,
                away_team=row.away_team,
                home_score=row.home_score,
                away_score=row.away_score,
                home_goals=row.home_goals,
                away_goals=row.away_goals,
                home_goals_first_half=row.home_goals_first_half,
                away_goals_first_half=row.away_goals_first_half,
                home_goals_second_half=row.home_goals_second_half,
                away_goals_second_half=row.away_goals_second_half,
                home_corners=row.home_corners,
                away_corners=row.away_corners,
                home_cards=row.home_cards,
                away_cards=row.away_cards,
                home_fouls=row.home_fouls,
                away_fouls=row.away_fouls,
                first_team_to_score=row.first_team_to_score,
                first_goal_minute=row.first_goal_minute,
                played_at=row.played_at,
                source_url=row.source_url,
            )
            for row, slug, season_name in rows
            if row.home_score is not None and row.away_score is not None
        ]
        return results, refreshed_at

    async def is_cached(self, competition: str, season: str) -> bool:
        async with self.sessions() as session:
            value = await session.scalar(
                select(CompetitionSeasonRow.fetched_at)
                .select_from(CompetitionSeasonRow)
                .join(CompetitionRow, CompetitionSeasonRow.competition_id == CompetitionRow.id)
                .where(
                    CompetitionRow.is_enabled.is_(True),
                    CompetitionRow.slug == competition.casefold(),
                    CompetitionSeasonRow.season == season,
                )
            )
            return value is not None

    async def enqueue_scrape(
        self, competition: Competition, source: SeasonSource
    ) -> tuple[int, str]:
        now = _now()
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(CompetitionRow)
                .where(
                    CompetitionRow.slug == competition.slug,
                    CompetitionRow.is_enabled.is_(True),
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError(f"Unknown competition: {competition.slug}")
            season = await self._ensure_season(session, row, competition, source, now)
            season = await session.scalar(
                select(CompetitionSeasonRow)
                .where(CompetitionSeasonRow.id == season.id)
                .with_for_update()
            )
            if season is None:
                raise ValueError(f"Unknown season: {competition.slug} {source.season}")
            active_run = await session.scalar(
                select(ScrapeRunRow)
                .where(
                    ScrapeRunRow.competition_season_id == season.id,
                    ScrapeRunRow.status.in_(("queued", "running")),
                )
                .order_by(ScrapeRunRow.id.desc())
                .limit(1)
                .with_for_update()
            )
            if active_run is not None:
                if active_run.source_url != source.source_url:
                    raise ActiveScrapeJobConflict(
                        f"A scrape job for {competition.slug} {source.season} is already active"
                    )
                return active_run.id, active_run.status

            run = ScrapeRunRow(
                competition_season_id=season.id,
                status="queued",
                source_url=source.source_url,
                queued_at=now,
                started_at=None,
                completed_at=None,
                matches_found=None,
                error_message=None,
            )
            session.add(run)
            await session.flush()
            return run.id, run.status

    async def get_scrape_job(self, run_id: int) -> ScrapeJobStatus | None:
        async with self.sessions() as session:
            row = await session.execute(
                select(ScrapeRunRow, CompetitionSeasonRow.season, CompetitionRow.slug)
                .join(
                    CompetitionSeasonRow,
                    ScrapeRunRow.competition_season_id == CompetitionSeasonRow.id,
                )
                .join(CompetitionRow, CompetitionSeasonRow.competition_id == CompetitionRow.id)
                .where(ScrapeRunRow.id == run_id)
            )
            result = row.one_or_none()
            if result is None:
                return None
            run, season, competition = result
            return ScrapeJobStatus(
                job_id=run.id,
                competition=competition,
                season=season,
                status=run.status,
                queued_at=run.queued_at,
                started_at=run.started_at,
                completed_at=run.completed_at,
                matches_found=run.matches_found,
                error_message=run.error_message,
            )

    async def claim_queued_scrape_runs(self, limit: int) -> list[RefreshTarget]:
        now = _now()
        async with self.sessions.begin() as session:
            rows = (
                await session.execute(
                    select(ScrapeRunRow, CompetitionSeasonRow, CompetitionRow)
                    .join(
                        CompetitionSeasonRow,
                        ScrapeRunRow.competition_season_id == CompetitionSeasonRow.id,
                    )
                    .join(CompetitionRow, CompetitionSeasonRow.competition_id == CompetitionRow.id)
                    .options(selectinload(CompetitionRow.seasons))
                    .where(
                        ScrapeRunRow.status == "queued",
                        CompetitionRow.is_enabled.is_(True),
                    )
                    .order_by(ScrapeRunRow.queued_at, ScrapeRunRow.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=ScrapeRunRow)
                )
            ).all()
            result = []
            for run, season, competition_row in rows:
                run.status = "running"
                run.started_at = now
                competition = self._competition(competition_row)
                source = SeasonSource(
                    season=season.season,
                    source_url=run.source_url,
                    provider=season.provider,
                )
                result.append(RefreshTarget(competition, source, run.id))
            return result

    async def replace_competition(
        self,
        competition: Competition,
        source: SeasonSource,
        fresh: list[ScrapedMatch],
        run_id: int | None = None,
    ) -> datetime:
        started = _now()
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(CompetitionRow).where(CompetitionRow.slug == competition.slug)
            )
            if row is None:
                raise ValueError(f"Unknown competition: {competition.slug}")
            season = await self._ensure_season(session, row, competition, source, started)
            if run_id is None:
                run = ScrapeRunRow(
                    competition_season_id=season.id,
                    status="running",
                    source_url=source.source_url,
                    queued_at=started,
                    started_at=started,
                    completed_at=None,
                    matches_found=None,
                    error_message=None,
                )
                session.add(run)
            else:
                run = await session.get(ScrapeRunRow, run_id)
                if run is None or run.competition_season_id != season.id or run.status != "running":
                    raise ValueError(f"Scrape job {run_id} is not running for {source.season}")
            await session.execute(
                delete(MatchRow).where(MatchRow.competition_season_id == season.id)
            )
            now = _now()
            session.add_all(
                [
                    MatchRow(
                        competition_season_id=season.id,
                        source_key=_source_key(match),
                        source_url=match.source_url,
                        stage=match.stage,
                        round=match.round,
                        home_team=match.home_team,
                        away_team=match.away_team,
                        status=match.status,
                        home_score=match.home_score,
                        away_score=match.away_score,
                        home_goals=match.home_goals,
                        away_goals=match.away_goals,
                        home_goals_first_half=match.home_goals_first_half,
                        away_goals_first_half=match.away_goals_first_half,
                        home_goals_second_half=match.home_goals_second_half,
                        away_goals_second_half=match.away_goals_second_half,
                        home_corners=match.home_corners,
                        away_corners=match.away_corners,
                        home_cards=match.home_cards,
                        away_cards=match.away_cards,
                        home_fouls=match.home_fouls,
                        away_fouls=match.away_fouls,
                        first_team_to_score=match.first_team_to_score,
                        first_goal_minute=match.first_goal_minute,
                        played_at=match.played_at,
                        created_at=now,
                        updated_at=now,
                    )
                    for match in fresh
                ]
            )
            season.fetched_at = season.last_checked_at = season.last_successful_check_at = now
            season.consecutive_failures, season.updated_at = 0, now
            if season.is_current and season.automatic_refresh_enabled:
                decision = calculate_next_check(fresh, competition.format, now)
                season.scheduler_state, season.next_check_at = (
                    decision.state,
                    decision.next_check_at,
                )
            run.status, run.completed_at, run.matches_found = "completed", now, len(fresh)
        return now

    async def record_scrape_failure(
        self,
        competition: Competition,
        source: SeasonSource,
        error: Exception,
        run_id: int | None = None,
    ) -> None:
        now = _now()
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(CompetitionRow).where(CompetitionRow.slug == competition.slug)
            )
            if row is None:
                if run_id is not None:
                    raise ValueError(f"Unknown competition: {competition.slug}")
                return
            season = await self._ensure_season(session, row, competition, source, now)
            season.last_checked_at, season.updated_at = now, now
            season.consecutive_failures += 1
            if season.is_current and season.automatic_refresh_enabled:
                season.scheduler_state = "retrying"
                season.next_check_at = now + failure_backoff(season.consecutive_failures)
            if run_id is not None:
                run = await session.get(ScrapeRunRow, run_id)
                if run is None or run.competition_season_id != season.id or run.status != "running":
                    raise ValueError(f"Scrape job {run_id} is not running for {source.season}")
                run.started_at = run.started_at or now
                run.status, run.completed_at, run.error_message = (
                    "failed",
                    now,
                    str(error)[:4000],
                )
            else:
                session.add(
                    ScrapeRunRow(
                        competition_season_id=season.id,
                        status="failed",
                        source_url=source.source_url,
                        queued_at=now,
                        started_at=now,
                        completed_at=now,
                        matches_found=None,
                        error_message=str(error)[:4000],
                    )
                )

    async def claim_due_seasons(self, limit: int) -> list[RefreshTarget]:
        now = _now()
        async with self.sessions.begin() as session:
            rows = (
                await session.execute(
                    select(CompetitionSeasonRow, CompetitionRow)
                    .select_from(CompetitionSeasonRow)
                    .join(CompetitionRow, CompetitionSeasonRow.competition_id == CompetitionRow.id)
                    .where(
                        CompetitionRow.is_enabled.is_(True),
                        CompetitionSeasonRow.is_current,
                        CompetitionSeasonRow.automatic_refresh_enabled,
                        CompetitionSeasonRow.next_check_at <= now,
                        ~select(ScrapeRunRow.id)
                        .where(
                            ScrapeRunRow.competition_season_id == CompetitionSeasonRow.id,
                            ScrapeRunRow.status.in_(("queued", "running")),
                        )
                        .exists(),
                    )
                    .order_by(CompetitionSeasonRow.next_check_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True, of=CompetitionSeasonRow)
                )
            ).all()
            result = []
            for season, competition in rows:
                season.next_check_at = now + timedelta(minutes=10)
                source = SeasonSource(
                    season=season.season,
                    source_url=season.source_url,
                    provider=season.provider,
                )
                value = Competition(
                    slug=competition.slug,
                    name=competition.name,
                    country=competition.country,
                    format=competition.format,
                    current_season=season.season,
                    seasons={season.season: source},
                )
                run = ScrapeRunRow(
                    competition_season_id=season.id,
                    status="running",
                    source_url=source.source_url,
                    queued_at=now,
                    started_at=now,
                    completed_at=None,
                    matches_found=None,
                    error_message=None,
                )
                session.add(run)
                await session.flush()
                result.append(RefreshTarget(value, source, run.id))
            return result

    async def heartbeat(self, worker_id: str, started_at: datetime) -> None:
        now = _now()
        async with self.sessions.begin() as session:
            row = await session.get(SchedulerHeartbeatRow, worker_id)
            if row is None:
                session.add(
                    SchedulerHeartbeatRow(
                        worker_id=worker_id, started_at=started_at, heartbeat_at=now
                    )
                )
            else:
                row.heartbeat_at = now

    async def latest_scheduler_heartbeat(self) -> datetime | None:
        async with self.sessions() as session:
            return await session.scalar(select(func.max(SchedulerHeartbeatRow.heartbeat_at)))

    @staticmethod
    async def _ensure_season(
        session: AsyncSession,
        row: CompetitionRow,
        competition: Competition,
        source: SeasonSource,
        now: datetime,
    ) -> CompetitionSeasonRow:
        season = await session.scalar(
            select(CompetitionSeasonRow).where(
                CompetitionSeasonRow.competition_id == row.id,
                CompetitionSeasonRow.season == source.season,
            )
        )
        if season is None:
            current = source.season == competition.current_season
            season = CompetitionSeasonRow(
                competition_id=row.id,
                season=source.season,
                source_url=source.source_url,
                provider=source.provider,
                is_current=current,
                fetched_at=None,
                automatic_refresh_enabled=current,
                scheduler_state="waiting_for_schedule" if current else "disabled",
                next_check_at=now if current else None,
                consecutive_failures=0,
                created_at=now,
                updated_at=now,
            )
            session.add(season)
            await session.flush()
        else:
            season.source_url, season.provider, season.updated_at = (
                source.source_url,
                source.provider,
                now,
            )
        return season

    @staticmethod
    def _competition(row: CompetitionRow) -> Competition:
        current = next((season for season in row.seasons if season.is_current), None)
        return Competition(
            slug=row.slug,
            name=row.name,
            country=row.country,
            format=row.format,
            current_season=None if current is None else current.season,
            seasons={
                s.season: SeasonSource(
                    season=s.season,
                    source_url=s.source_url,
                    provider=s.provider,
                )
                for s in row.seasons
            },
        )
