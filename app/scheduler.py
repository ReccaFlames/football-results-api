import asyncio
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from app.config import CompetitionFile, ConfigFileWatcher, settings
from app.database import session_factory
from app.logs import configure_logging
from app.models import Competition, SeasonSource
from app.repository import PostgresResultRepository, ScheduledRefresh
from app.scheduling import SchedulePolicy
from app.scraper import ScraperRegistry, create_scraper_registry

logger = logging.getLogger(__name__)


def _create_scraper(config: CompetitionFile) -> ScraperRegistry:
    ninety_minut = config.providers.get("90minut")
    football_data = config.providers.get("football-data")
    espn = config.providers.get("espn")
    return create_scraper_registry(
        settings.request_timeout_seconds,
        settings.user_agent,
        settings.football_data_api_token,
        settings.football_data_min_request_interval_seconds
        if settings.football_data_min_request_interval_seconds is not None
        else (6.0 if football_data is None else football_data.min_request_interval_seconds),
        settings.ninety_minut_min_request_interval_seconds
        if settings.ninety_minut_min_request_interval_seconds is not None
        else (2.0 if ninety_minut is None else ninety_minut.min_request_interval_seconds),
        settings.ninety_minut_detail_cache_max_entries,
        settings.espn_min_request_interval_seconds
        if settings.espn_min_request_interval_seconds is not None
        else (2.0 if espn is None else espn.min_request_interval_seconds),
        config.team_aliases(),
    )


async def check_seasons(
    repository: PostgresResultRepository,
    scraper: ScraperRegistry,
    config: CompetitionFile,
    competition: Competition,
) -> None:
    """Discover new seasons of one competition and roll its current season over when due."""
    configured = next((item for item in config.competitions if item.slug == competition.slug), None)
    if configured is None:
        return
    policy = config.schedule_for(competition.slug)
    provider = scraper.provider(configured.provider)
    discovered: list[SeasonSource] = []
    next_check_in = policy.season_check_interval
    if provider.capabilities.season_discovery:
        try:
            discovered = [
                source
                for source in await provider.discover_seasons(competition)
                if source.provider == provider.name
            ]
        except Exception:
            logger.exception("season discovery failed for %s", competition.slug)
            next_check_in = policy.failure_backoff[-1]
    result = await repository.apply_season_check(
        competition.slug, discovered, datetime.now(UTC) + next_check_in, policy
    )
    if result.added_seasons:
        logger.info("discovered %s seasons %s", competition.slug, result.added_seasons)
    if result.rejected_sources:
        logger.warning(
            "ignored invalid discovered %s seasons %s", competition.slug, result.rejected_sources
        )
    if result.rolled_over_to:
        logger.info(
            "rolled %s over from %s to %s",
            competition.slug,
            result.rolled_over_from,
            result.rolled_over_to,
        )
    elif result.missing_season:
        logger.info(
            "%s season %s has started but has no source yet; add it to the configuration",
            competition.slug,
            result.missing_season,
        )


def worker_name() -> str:
    # Containers restarted in place keep their hostname and PID 1, so add a per-process suffix:
    # a restarted worker must not look alive to recovery of the jobs its predecessor abandoned.
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def keep_heartbeat(
    repository: PostgresResultRepository, worker_id: str, started_at: datetime, interval: float
) -> None:
    """Heartbeat independently of the cycle, so long scrapes do not look like a dead worker."""
    while True:
        try:
            await repository.heartbeat(worker_id, started_at)
        except Exception:
            logger.exception("scheduler heartbeat failed; will retry")
        await asyncio.sleep(interval)


async def recover_abandoned_runs(repository: PostgresResultRepository) -> None:
    stale_after = timedelta(seconds=settings.scheduler_worker_timeout_seconds)
    for run in await repository.fail_abandoned_scrape_runs(stale_after):
        logger.warning(
            "failed abandoned scrape job %d for %s %s (worker %s stopped heartbeating)",
            run.run_id,
            run.competition,
            run.season,
            run.worker_id or "unknown",
        )


def describe_idle(next_refresh: ScheduledRefresh | None, now: datetime) -> str:
    if next_refresh is None:
        return "idle; no current season has automatic refresh enabled"
    name = f"{next_refresh.competition} {next_refresh.season}"
    due = next_refresh.next_check_at.isoformat(timespec="seconds")
    if next_refresh.next_check_at > now:
        return f"idle; next refresh {name} at {due}"
    if next_refresh.active_run_id is not None:
        return (
            f"idle; {name} was due at {due} and waits for scrape job {next_refresh.active_run_id}"
        )
    return f"idle; {name} was due at {due} and is claimed by another worker"


async def run() -> None:
    repository = PostgresResultRepository(session_factory)
    config_watcher = ConfigFileWatcher(settings.config_file)
    config, _ = config_watcher.load_if_changed()
    await repository.seed_competitions(config.to_competitions(), config.auto_rollover_slugs())
    config_watcher.mark_applied()
    scraper = _create_scraper(config)
    schedules: dict[str, SchedulePolicy] = config.schedules()
    worker_id = worker_name()
    started_at = datetime.now(UTC)
    heartbeat_interval = min(
        settings.scheduler_loop_seconds, settings.scheduler_worker_timeout_seconds / 3
    )
    await repository.heartbeat(worker_id, started_at)
    heartbeat = asyncio.create_task(
        keep_heartbeat(repository, worker_id, started_at, heartbeat_interval)
    )
    logger.info(
        "scheduler worker %s started (cycle every %ss, claim limit %d, abandoned jobs after %ss)",
        worker_id,
        settings.scheduler_loop_seconds,
        settings.scheduler_claim_limit,
        settings.scheduler_worker_timeout_seconds,
    )
    last_idle_message = None
    while True:
        try:
            config, changed = config_watcher.load_if_changed()
            if changed:
                await repository.seed_competitions(
                    config.to_competitions(), config.auto_rollover_slugs()
                )
                config_watcher.mark_applied()
                scraper = _create_scraper(config)
                schedules = config.schedules()
            await recover_abandoned_runs(repository)
            for competition in await repository.claim_season_checks(settings.scheduler_claim_limit):
                try:
                    await check_seasons(repository, scraper, config, competition)
                except Exception:
                    logger.exception("season check failed for %s", competition.slug)
            targets = await repository.claim_queued_scrape_runs(
                settings.scheduler_claim_limit, worker_id
            )
            targets.extend(
                await repository.claim_due_seasons(settings.scheduler_claim_limit, worker_id)
            )
            for target in targets:
                logger.info(
                    "refreshing %s %s (job %s)",
                    target.competition.slug,
                    target.source.season,
                    target.run_id,
                )
                policy = schedules.get(target.competition.slug, SchedulePolicy())
                try:
                    matches = await scraper.scrape(target.competition, target.source)
                    await repository.replace_competition(
                        target.competition,
                        target.source,
                        matches,
                        run_id=target.run_id,
                        policy=policy,
                    )
                    logger.info(
                        "refreshed %s %s (%d fixtures)",
                        target.competition.slug,
                        target.source.season,
                        len(matches),
                    )
                except Exception as error:
                    logger.exception(
                        "refresh failed for %s %s", target.competition.slug, target.source.season
                    )
                    # Keep going: one unrecorded failure must not strand the other claimed jobs.
                    try:
                        await repository.record_scrape_failure(
                            target.competition,
                            target.source,
                            error,
                            run_id=target.run_id,
                            policy=policy,
                        )
                    except Exception:
                        logger.exception(
                            "could not record the failed refresh of %s %s",
                            target.competition.slug,
                            target.source.season,
                        )
            if targets:
                last_idle_message = None
            else:
                message = describe_idle(
                    await repository.next_scheduled_refresh(), datetime.now(UTC)
                )
                if message != last_idle_message:
                    logger.info(message)
                    last_idle_message = message
        except Exception:
            logger.exception("scheduler cycle failed; will retry")
        if heartbeat.done():
            heartbeat = asyncio.create_task(
                keep_heartbeat(repository, worker_id, started_at, heartbeat_interval)
            )
        await asyncio.sleep(settings.scheduler_loop_seconds)


if __name__ == "__main__":
    configure_logging(settings.log_format)
    asyncio.run(run())
