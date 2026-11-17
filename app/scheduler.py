import asyncio
import logging
import os
import socket
from datetime import UTC, datetime

from app.config import CompetitionFile, ConfigFileWatcher, settings
from app.database import session_factory
from app.models import Competition, SeasonSource
from app.repository import PostgresResultRepository
from app.scheduling import SchedulePolicy
from app.scraper import ScraperRegistry, create_scraper_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _create_scraper(config: CompetitionFile) -> ScraperRegistry:
    ninety_minut = config.providers.get("90minut")
    football_data = config.providers.get("football-data")
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


async def run() -> None:
    repository = PostgresResultRepository(session_factory)
    config_watcher = ConfigFileWatcher(settings.config_file)
    config, _ = config_watcher.load_if_changed()
    await repository.seed_competitions(config.to_competitions(), config.auto_rollover_slugs())
    config_watcher.mark_applied()
    scraper = _create_scraper(config)
    schedules: dict[str, SchedulePolicy] = config.schedules()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    started_at = datetime.now(UTC)
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
            await repository.heartbeat(worker_id, started_at)
            for competition in await repository.claim_season_checks(settings.scheduler_claim_limit):
                try:
                    await check_seasons(repository, scraper, config, competition)
                except Exception:
                    logger.exception("season check failed for %s", competition.slug)
            targets = await repository.claim_queued_scrape_runs(settings.scheduler_claim_limit)
            targets.extend(await repository.claim_due_seasons(settings.scheduler_claim_limit))
            for target in targets:
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
                    await repository.record_scrape_failure(
                        target.competition,
                        target.source,
                        error,
                        run_id=target.run_id,
                        policy=policy,
                    )
        except Exception:
            logger.exception("scheduler cycle failed; will retry")
        await asyncio.sleep(settings.scheduler_loop_seconds)


if __name__ == "__main__":
    asyncio.run(run())
