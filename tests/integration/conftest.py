import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.repository import PostgresResultRepository
from tests.integration.support import run_alembic

TABLES = (
    "team_alias, team, competition, competition_season, match_result, scrape_run, "
    "scheduler_heartbeat, access_request"
)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("FOOTBALL_RESULTS_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("FOOTBALL_RESULTS_TEST_DATABASE_URL must be set in CI")
        pytest.skip("FOOTBALL_RESULTS_TEST_DATABASE_URL is not set")
    # These tests drop and truncate tables; never point them at a real database.
    if "test" not in (make_url(url).database or ""):
        pytest.fail("The integration test database name must contain 'test'")
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    run_alembic(database_url, "upgrade", "head")
    return database_url


@pytest.fixture
async def sessions(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def repository(sessions: async_sessionmaker[AsyncSession]) -> PostgresResultRepository:
    return PostgresResultRepository(sessions)
