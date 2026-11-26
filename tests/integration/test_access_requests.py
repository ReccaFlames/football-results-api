import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import access
from app import config as config_module
from app import main as main_module
from app.db_models import AccessRequestRow
from app.ratelimit import FixedWindowRateLimiter
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


ACCESS_REQUEST = {
    "name": "Ada Lovelace",
    "contact_email": "ada@example.com",
    "intended_use": "Weekly tables on a fan site",
    "client_type": "human",
}


@pytest.fixture
async def api(
    repository: PostgresResultRepository,
    migrated_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """The API and `python -m app.config` on the test database."""
    monkeypatch.setattr(main_module, "store", repository)
    monkeypatch.setattr(main_module, "rate_limiter", FixedWindowRateLimiter())
    monkeypatch.setattr(config_module.settings, "database_url", migrated_database)
    monkeypatch.setattr(config_module.settings, "demo_mode", False)
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def config_cli(*argv: str) -> int:
    # The config CLI runs its own event loop, so it needs a thread of its own here.
    return await asyncio.to_thread(config_module.cli, ["access-requests", *argv])


async def test_the_operator_approves_and_the_requester_claims_the_key_once(
    api: httpx.AsyncClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    created = (await api.post("/api/v1/access-requests", json=ACCESS_REQUEST)).json()
    keys = tmp_path / "keys.yaml"

    assert await config_cli("list", "--status", "pending") == 0
    _, row = capsys.readouterr().out.splitlines()
    assert row.split()[:4] == [str(created["id"]), "pending", "human", "Ada"]
    assert "ada@example.com" in row

    assert await config_cli("approve", str(created["id"]), "--file", str(keys)) == 0
    key_name = f"req-{created['id']}-ada-lovelace"
    assert f"'{key_name}'" in capsys.readouterr().out

    secret = {"Claim-Secret": created["claim_secret"]}
    claimed = (await api.get(created["status_url"], headers=secret)).json()
    assert (claimed["status"], claimed["key_name"]) == ("claimed", key_name)
    identity = access.resolve(claimed["api_key"], (), access.load_keys(keys))
    assert identity is not None and (identity.name, identity.role) == (key_name, "client")
    again = (await api.get(created["status_url"], headers=secret)).json()
    assert again["api_key"] is None


async def test_the_requester_sees_an_expired_claim_and_a_purged_request_as_unknown(
    api: httpx.AsyncClient,
    repository: PostgresResultRepository,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    late = (await api.post("/api/v1/access-requests", json=ACCESS_REQUEST)).json()
    old = (
        await api.post(
            "/api/v1/access-requests", json=ACCESS_REQUEST | {"contact_email": "old@example.com"}
        )
    ).json()
    await repository.approve_access_request(late["id"], "req-1-ada", "fr_secret")
    await age(sessions, late["id"], decided_at=CLAIM_WINDOW)
    await age(sessions, old["id"], created_at=UNAPPROVED_RETENTION)

    expired = await api.get(late["status_url"], headers={"Claim-Secret": late["claim_secret"]})
    assert expired.status_code == 200
    assert (expired.json()["status"], expired.json()["api_key"]) == ("expired", None)

    assert await repository.purge_access_requests() == (1, 0)
    purged = await api.get(old["status_url"], headers={"Claim-Secret": old["claim_secret"]})
    assert purged.status_code == 404
    assert purged.json()["type"].endswith("/problems/unknown-access-request")


async def test_the_operator_rejects_and_a_decided_request_gets_no_key(
    api: httpx.AsyncClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    created = (await api.post("/api/v1/access-requests", json=ACCESS_REQUEST)).json()
    keys = tmp_path / "keys.yaml"

    assert await config_cli("reject", str(created["id"])) == 0
    for argv in (
        ("reject", str(created["id"])),
        ("approve", str(created["id"]), "--file", str(keys)),
    ):
        with pytest.raises(SystemExit) as exit_info:
            await config_cli(*argv)
        assert exit_info.value.code == 2
    assert "is rejected" in capsys.readouterr().err
    assert not keys.exists()

    status = await api.get(created["status_url"], headers={"Claim-Secret": created["claim_secret"]})
    assert (status.json()["status"], status.json()["api_key"]) == ("rejected", None)
