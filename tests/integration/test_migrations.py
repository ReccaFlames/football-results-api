import asyncio

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.support import run_alembic

APP_TABLES = {
    "competition",
    "competition_season",
    "match_result",
    "team",
    "team_alias",
    "scrape_run",
    "scheduler_heartbeat",
    "access_request",
}


def table_names(database_url: str) -> set[str]:
    async def load() -> set[str]:
        engine = create_async_engine(database_url, poolclass=NullPool)
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
        await engine.dispose()
        return names

    return asyncio.run(load())


def test_migrations_round_trip_and_match_models(migrated_database: str) -> None:
    run_alembic(migrated_database, "downgrade", "base")
    assert table_names(migrated_database) & APP_TABLES == set()

    run_alembic(migrated_database, "upgrade", "head")
    assert APP_TABLES <= table_names(migrated_database)

    run_alembic(migrated_database, "downgrade", "-1")
    run_alembic(migrated_database, "upgrade", "head")
    # Fails when app/db_models.py has changes that no migration covers.
    run_alembic(migrated_database, "check")


def test_stable_match_migration_backfills_existing_rows(migrated_database: str) -> None:
    async def insert_legacy_match() -> None:
        engine = create_async_engine(migrated_database)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO competition
                        (id, slug, name, country, format, created_at, updated_at)
                    VALUES (1, 'legacy-league', 'Legacy League', 'Poland', 'league', now(), now())
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO competition_season
                        (id, competition_id, season, source_url, provider, is_current,
                         created_at, updated_at)
                    VALUES (10, 1, '2026-2027',
                            'http://www.90minut.pl/liga/1/liga123.html',
                            '90minut', true, now(), now())
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO match_result
                        (id, competition_season_id, source_key, source_url, stage, round,
                         home_team, away_team, status, home_score, away_score, played_at,
                         created_at, updated_at)
                    VALUES (20, 10, 'legacy-source-key',
                            'http://www.90minut.pl/mecz.php?id_mecz=90210',
                            'Kolejka 1', 1, 'Łódź FC', 'Lech Poznań',
                            'finished', 1, 0, now(), now(), now())
                    """
                )
            )
        await engine.dispose()

    run_alembic(migrated_database, "downgrade", "base")
    run_alembic(migrated_database, "upgrade", "0006_configured_competitions")
    asyncio.run(insert_legacy_match())
    run_alembic(migrated_database, "upgrade", "head")

    async def read_backfilled_match() -> tuple:
        engine = create_async_engine(migrated_database)
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT match_result.id, match_result.provider_match_id,
                           match_result.source_key, match_result.home_team,
                           match_result.home_team_id, match_result.away_team_id,
                           home_team.slug AS home_slug, away_team.slug AS away_slug
                    FROM match_result
                    JOIN team AS home_team ON home_team.id = match_result.home_team_id
                    JOIN team AS away_team ON away_team.id = match_result.away_team_id
                    """
                )
            )
            value = result.one()
        await engine.dispose()
        return value

    match_row = asyncio.run(read_backfilled_match())
    assert match_row.id == 20
    assert match_row.provider_match_id == "90210"
    assert match_row.home_team == "Łódź FC"
    assert match_row.home_slug == "lodz-fc"
    assert match_row.away_slug == "lech-poznan"
    assert match_row.source_key == (
        f"2026-2027|1|{match_row.home_team_id}|{match_row.away_team_id}"
    )


def test_matches_skipped_migration_sets_existing_runs_to_zero(migrated_database: str) -> None:
    async def execute(statement: str) -> list:
        engine = create_async_engine(migrated_database)
        async with engine.begin() as connection:
            result = await connection.execute(text(statement))
            rows = list(result.all()) if result.returns_rows else []
        await engine.dispose()
        return rows

    run_alembic(migrated_database, "downgrade", "base")
    run_alembic(migrated_database, "upgrade", "0011_fallback_sources")
    asyncio.run(
        execute(
            """
            WITH league AS (
                INSERT INTO competition (slug, name, country, format, created_at, updated_at)
                VALUES ('legacy-league', 'Legacy League', 'Poland', 'league', now(), now())
                RETURNING id
            ), season AS (
                INSERT INTO competition_season
                    (competition_id, season, source_url, provider, is_current,
                     created_at, updated_at)
                SELECT id, '2026-2027', 'http://www.90minut.pl/liga/1/liga123.html',
                       '90minut', true, now(), now()
                FROM league
                RETURNING id
            )
            INSERT INTO scrape_run
                (competition_season_id, status, source_url, queued_at, matches_found)
            SELECT id, 'completed', 'http://www.90minut.pl/liga/1/liga123.html', now(), 5
            FROM season
            """
        )
    )
    run_alembic(migrated_database, "upgrade", "head")

    assert asyncio.run(execute("SELECT matches_found, matches_skipped FROM scrape_run")) == [(5, 0)]
