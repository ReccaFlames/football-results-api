import asyncio
import logging
import os
import socket
from datetime import UTC, datetime

from app.competitions import COMPETITIONS
from app.config import settings
from app.database import session_factory
from app.repository import PostgresResultRepository
from app.scraper import create_scraper_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def run() -> None:
    repository = PostgresResultRepository(session_factory)
    scraper = create_scraper_registry(
        settings.request_timeout_seconds,
        settings.user_agent,
        settings.football_data_api_token,
        settings.football_data_min_request_interval_seconds,
        settings.ninety_minut_min_request_interval_seconds,
        settings.ninety_minut_detail_cache_max_entries,
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    started_at = datetime.now(UTC)
    while True:
        try:
            await repository.seed_competitions(COMPETITIONS)
            await repository.heartbeat(worker_id, started_at)
            targets = await repository.claim_queued_scrape_runs(settings.scheduler_claim_limit)
            targets.extend(await repository.claim_due_seasons(settings.scheduler_claim_limit))
            for target in targets:
                try:
                    matches = await scraper.scrape(target.competition, target.source)
                    await repository.replace_competition(
                        target.competition, target.source, matches, run_id=target.run_id
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
                        target.competition, target.source, error, run_id=target.run_id
                    )
        except Exception:
            logger.exception("scheduler cycle failed; will retry")
        await asyncio.sleep(settings.scheduler_loop_seconds)


if __name__ == "__main__":
    asyncio.run(run())
