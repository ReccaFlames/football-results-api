import asyncio

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.support import run_alembic

APP_TABLES = {
    "competition",
    "competition_season",
    "match_result",
    "scrape_run",
    "scheduler_heartbeat",
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
