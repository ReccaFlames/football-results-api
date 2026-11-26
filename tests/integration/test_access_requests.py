from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db_models import AccessRequestRow
from app.repository import (
    CLAIM_WINDOW,
    UNAPPROVED_RETENTION,
    AccessRequestPending,
    AccessRequestStateConflict,
    PostgresResultRepository,
)

SECRET_HASH = "sha256:" + "0" * 64


async def create(repository: PostgresResultRepository, email: str = "Ada@Example.com") -> int:
    stored = await repository.create_access_request(
        name="Ada",
        contact_email=email,
        intended_use="A fan site with Ekstraklasa tables",
        client_type="human",
        claim_secret_hash=SECRET_HASH,
        competitions=["ekstraklasa"],
        message="Thanks!",
    )
    return stored.id


async def age(
    sessions: async_sessionmaker[AsyncSession], request_id: int, **ages: timedelta
) -> None:
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        await session.execute(
            update(AccessRequestRow)
            .where(AccessRequestRow.id == request_id)
            .values({column: now - delta for column, delta in ages.items()})
        )


async def test_one_pending_request_per_email_ignoring_case(
    repository: PostgresResultRepository,
) -> None:
    first = await create(repository)
    stored = await repository.get_access_request(first)
    assert stored is not None
    assert (stored.status, stored.contact_email) == ("pending", "ada@example.com")
    assert stored.competitions == ("ekstraklasa",)

    with pytest.raises(AccessRequestPending):
        await create(repository, " ADA@example.com ")

    await repository.reject_access_request(first)
    assert await create(repository) != first


async def test_an_approved_key_is_claimed_exactly_once_and_erases_personal_data(
    repository: PostgresResultRepository,
) -> None:
    request_id = await create(repository)
    approved = await repository.approve_access_request(request_id, "req-1-ada", "fr_secret")
    assert approved is not None and approved.status == "approved"
    assert approved.claim_expires_at == approved.decided_at + CLAIM_WINDOW

    with pytest.raises(AccessRequestStateConflict):
        await repository.reject_access_request(request_id)

    assert await repository.claim_access_request(request_id) == "fr_secret"
    assert await repository.claim_access_request(request_id) is None
    claimed = await repository.get_access_request(request_id)
    assert claimed is not None and claimed.claimed_at is not None
    assert (claimed.status, claimed.key_name) == ("claimed", "req-1-ada")
    assert (claimed.name, claimed.contact_email, claimed.intended_use) == (None, None, None)
    assert (claimed.competitions, claimed.message) == (None, None)


async def test_pending_and_unknown_requests_cannot_be_claimed(
    repository: PostgresResultRepository,
) -> None:
    request_id = await create(repository)
    assert await repository.claim_access_request(request_id) is None
    assert await repository.claim_access_request(request_id + 1) is None
    assert await repository.approve_access_request(request_id + 1, "x", "y") is None


async def test_a_key_not_claimed_within_the_window_expires(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    request_id = await create(repository)
    await repository.approve_access_request(request_id, "req-1-ada", "fr_secret")
    await age(sessions, request_id, decided_at=CLAIM_WINDOW + timedelta(seconds=1))

    assert await repository.claim_access_request(request_id) is None
    expired = await repository.get_access_request(request_id)
    assert expired is not None and expired.status == "expired"
    assert expired.contact_email is None


async def test_purge_deletes_old_unapproved_requests_and_expires_unclaimed_keys(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    old = UNAPPROVED_RETENTION + timedelta(seconds=1)
    old_pending = await create(repository, "old-pending@example.com")
    old_rejected = await create(repository, "old-rejected@example.com")
    await repository.reject_access_request(old_rejected)
    recent = await create(repository, "recent@example.com")
    unclaimed = await create(repository, "unclaimed@example.com")
    await repository.approve_access_request(unclaimed, "req-4", "fr_unclaimed")
    claimable = await create(repository, "claimable@example.com")
    await repository.approve_access_request(claimable, "req-5", "fr_claimable")
    await age(sessions, old_pending, created_at=old)
    await age(sessions, old_rejected, created_at=old)
    await age(sessions, unclaimed, created_at=old, decided_at=CLAIM_WINDOW)

    assert await repository.purge_access_requests() == (2, 1)

    remaining = {item.id: item.status for item in await repository.list_access_requests()}
    assert remaining == {claimable: "approved", unclaimed: "expired", recent: "pending"}
    assert await repository.claim_access_request(claimable) == "fr_claimable"


async def test_list_filters_by_status_newest_first_with_a_keyset(
    repository: PostgresResultRepository,
) -> None:
    ids = [await create(repository, f"user{index}@example.com") for index in range(3)]
    await repository.reject_access_request(ids[1])

    pending = await repository.list_access_requests(("pending",))
    assert [item.id for item in pending] == [ids[2], ids[0]]
    page = await repository.list_access_requests(limit=1, before_id=ids[2])
    assert [item.id for item in page] == [ids[1]]
