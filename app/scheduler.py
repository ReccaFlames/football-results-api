import asyncio
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from app import access
from app.config import CompetitionFile, ConfigFileWatcher, check_demo_mode, settings
from app.database import session_factory
from app.logs import configure_logging
from app.models import Competition, SeasonSource
from app.repository import (
    CLAIM_WINDOW,
    UNAPPROVED_RETENTION,
    PostgresResultRepository,
    RefreshTarget,
    ScheduledRefresh,
)
from app.scheduling import SchedulePolicy
from app.scraper import (
    OPENLIGADB_MIN_REQUEST_INTERVAL_SECONDS,
    OPENLIGADB_POSTPONED_AFTER_HOURS,
    THESPORTSDB_MIN_REQUEST_INTERVAL_SECONDS,
    THESPORTSDB_WINDOW_DAYS_AHEAD,
    THESPORTSDB_WINDOW_DAYS_BACK,
    ScraperRegistry,
    create_scraper_registry,
)

logger = logging.getLogger(__name__)

# Retention deletes by age in days, so an hourly pass is precise enough and cheap.
ACCESS_REQUEST_PURGE_INTERVAL = timedelta(hours=1)


def _create_scraper(config: CompetitionFile) -> ScraperRegistry:
    ninety_minut = config.providers.get("90minut")
    football_data = config.providers.get("football-data")
    espn = config.providers.get("espn")
    thesportsdb = config.providers.get("thesportsdb")
    openligadb = config.providers.get("openligadb")
    window_days_back, window_days_ahead = (
        (THESPORTSDB_WINDOW_DAYS_BACK, THESPORTSDB_WINDOW_DAYS_AHEAD)
        if thesportsdb is None
        else thesportsdb.window()
    )
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
        thesportsdb_api_key=settings.thesportsdb_api_key.get_secret_value(),
        thesportsdb_min_interval_seconds=settings.thesportsdb_min_request_interval_seconds
        if settings.thesportsdb_min_request_interval_seconds is not None
        else (
            THESPORTSDB_MIN_REQUEST_INTERVAL_SECONDS
            if thesportsdb is None
            else thesportsdb.min_request_interval_seconds
        ),
        thesportsdb_window_days_back=window_days_back,
        thesportsdb_window_days_ahead=window_days_ahead,
        openligadb_min_interval_seconds=settings.openligadb_min_request_interval_seconds
        if settings.openligadb_min_request_interval_seconds is not None
        else (
            OPENLIGADB_MIN_REQUEST_INTERVAL_SECONDS
            if openligadb is None
            else openligadb.min_request_interval_seconds
        ),
        openligadb_postponed_after_hours=OPENLIGADB_POSTPONED_AFTER_HOURS
        if openligadb is None
        else openligadb.postponed_after(),
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
    fallbacks = await discover_fallback_seasons(scraper, configured.fallback_provider, competition)
    result = await repository.apply_season_check(
        competition.slug,
        discovered,
        datetime.now(UTC) + next_check_in,
        policy,
        fallbacks=fallbacks,
    )
    if result.added_seasons:
        logger.info("discovered %s seasons %s", competition.slug, result.added_seasons)
    if result.fallback_seasons:
        logger.info(
            "added fallback sources to %s seasons %s", competition.slug, result.fallback_seasons
        )
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


async def discover_fallback_seasons(
    scraper: ScraperRegistry, provider_name: str | None, competition: Competition
) -> list[SeasonSource]:
    """Seasons the fallback provider knows, so automatically discovered seasons get a fallback."""
    if provider_name is None or not scraper.available(provider_name):
        return []
    provider = scraper.provider(provider_name)
    if not provider.capabilities.season_discovery:
        return []
    known = {
        season: fallback
        for season, source in competition.seasons.items()
        if (fallback := source.fallback()) is not None
    }
    if not known:
        return []
    try:
        found = await provider.discover_seasons(competition.model_copy(update={"seasons": known}))
    except Exception:
        logger.exception("fallback season discovery failed for %s", competition.slug)
        return []
    return [source for source in found if source.provider == provider_name]


async def refresh_target(
    repository: PostgresResultRepository,
    scraper: ScraperRegistry,
    target: RefreshTarget,
    policy: SchedulePolicy,
    fallback_provider: str | None = None,
) -> None:
    """Refresh one claimed season from its primary source, else from its fallback source.

    Only scheduled checks use the fallback, once the primary has failed
    ``policy.fallback_after_failures`` times in a row (ADR 0002). ``fallback_provider`` is the
    configured fallback provider of the competition; other stored fallbacks are ignored.
    """
    competition, source = target.competition, target.source
    name = f"{competition.slug} {source.season}"
    try:
        matches = await scraper.scrape(competition, source)
        await repository.replace_competition(
            competition, source, matches, run_id=target.run_id, policy=policy
        )
        logger.info("refreshed %s (%d fixtures)", name, len(matches))
        return
    except Exception as error:
        logger.exception("refresh failed for %s", name)
        # Keep going: one unrecorded failure must not strand the other claimed jobs.
        try:
            failures = await repository.record_scrape_failure(
                competition, source, error, run_id=target.run_id, policy=policy
            )
        except Exception:
            logger.exception("could not record the failed refresh of %s", name)
            return
    fallback = source.fallback()
    if (
        not target.scheduled
        or fallback is None
        or fallback.provider != fallback_provider
        or failures < policy.fallback_after_failures
        or not scraper.available(fallback.provider)
    ):
        return
    logger.warning(
        "refreshing %s from fallback source %s after %d primary failures",
        name,
        fallback.provider,
        failures,
    )
    try:
        matches = await scraper.scrape(competition, fallback)
        await repository.replace_competition(
            competition,
            source,
            matches,
            policy=policy,
            fallback=fallback,
            windowed=scraper.provider(fallback.provider).capabilities.windowed,
        )
        logger.info("refreshed %s from fallback source (%d fixtures)", name, len(matches))
    except Exception as error:
        logger.exception("fallback refresh failed for %s", name)
        try:
            await repository.record_scrape_failure(
                competition, source, error, policy=policy, fallback=fallback
            )
        except Exception:
            logger.exception("could not record the failed fallback refresh of %s", name)


def log_fallback_warnings(config: CompetitionFile) -> None:
    for warning in config.fallback_warnings(
        settings.football_data_api_token, settings.thesportsdb_api_key.get_secret_value()
    ):
        logger.warning(warning)


def check_demo(config: CompetitionFile) -> None:
    """Enforce demo mode (ADR 0007) before a configuration is seeded."""
    if settings.demo_mode:
        check_demo_mode(config, access.load_keys(settings.api_keys_file))


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


async def purge_access_requests(
    repository: PostgresResultRepository, last_purged_at: datetime | None, now: datetime
) -> datetime | None:
    """Apply the access-request retention (ADR 0010) at most every `ACCESS_REQUEST_PURGE_INTERVAL`;
    returns when it last succeeded. Logs only counts, never personal data."""
    if last_purged_at is not None and now - last_purged_at < ACCESS_REQUEST_PURGE_INTERVAL:
        return last_purged_at
    try:
        deleted, expired = await repository.purge_access_requests()
    except Exception:
        logger.exception("access request purge failed; will retry")
        return last_purged_at
    if deleted or expired:
        logger.info(
            "access requests: deleted %d not approved within %d days, expired %d keys not "
            "claimed within %d days",
            deleted,
            UNAPPROVED_RETENTION.days,
            expired,
            CLAIM_WINDOW.days,
        )
    return now


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
    check_demo(config)
    await repository.seed_competitions(config.to_competitions(), config.auto_rollover_slugs())
    config_watcher.mark_applied()
    log_fallback_warnings(config)
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
    last_purged_at: datetime | None = None
    while True:
        try:
            config, changed = config_watcher.load_if_changed()
            if changed:
                # A rejected file is not applied; the scheduler keeps the previous one and retries.
                check_demo(config)
                await repository.seed_competitions(
                    config.to_competitions(), config.auto_rollover_slugs()
                )
                config_watcher.mark_applied()
                log_fallback_warnings(config)
                scraper = _create_scraper(config)
                schedules = config.schedules()
            await recover_abandoned_runs(repository)
            last_purged_at = await purge_access_requests(
                repository, last_purged_at, datetime.now(UTC)
            )
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
            fallback_providers = config.fallback_providers()
            for target in targets:
                logger.info(
                    "refreshing %s %s (job %s)",
                    target.competition.slug,
                    target.source.season,
                    target.run_id,
                )
                await refresh_target(
                    repository,
                    scraper,
                    target,
                    schedules.get(target.competition.slug, SchedulePolicy()),
                    fallback_providers.get(target.competition.slug),
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
