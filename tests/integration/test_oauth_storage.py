"""The built-in authorization server's storage (ADR 0012) on PostgreSQL."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import access, oauth, oauth_server
from app import main as main_module
from app.db_models import OAuthCodeRow, OAuthGrantRow, OAuthTokenRow
from app.ratelimit import FixedWindowRateLimiter
from app.repository import OAUTH_REFRESH_LIFETIME, OAuthCode, PostgresResultRepository
from tests.test_oauth_server import (
    BASE,
    CHATGPT,
    USER_KEY,
    VERIFIER,
    ClientHosts,
    authorize_params,
    user_entry,
)

CODE = OAuthCode(
    client_id=CHATGPT,
    client_name="ChatGPT",
    redirect_uri="https://chatgpt.com/connector_platform_oauth_redirect",
    code_challenge="c" * 43,
    resource=f"{BASE}/mcp",
    key_hash=access.hash_key(USER_KEY),
)


async def count(sessions: async_sessionmaker[AsyncSession], table: type) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table)) or 0


async def expire(sessions: async_sessionmaker[AsyncSession], table: type, **where: object) -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with sessions.begin() as session:
        statement = update(table).values(expires_at=past)
        for column, value in where.items():
            statement = statement.where(getattr(table, column) == value)
        await session.execute(statement)


async def test_a_code_is_consumed_once_and_not_after_expiry(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.create_oauth_code("sha256:a", CODE)
    assert await repository.consume_oauth_code("sha256:a") == CODE
    assert await repository.consume_oauth_code("sha256:a") is None

    await repository.create_oauth_code("sha256:b", CODE)
    await expire(sessions, OAuthCodeRow)
    assert await repository.consume_oauth_code("sha256:b") is None
    assert await count(sessions, OAuthCodeRow) == 0


async def test_refresh_rotates_and_reuse_deletes_the_grant(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    grant = await repository.create_oauth_grant(CODE, "sha256:access1", "sha256:refresh1")
    assert await repository.oauth_access_grant("sha256:access1") == grant
    assert await repository.oauth_access_grant("sha256:refresh1") is None

    other_client = await repository.rotate_oauth_refresh_token(
        "sha256:refresh1", "https://claude.ai/x", "sha256:access2", "sha256:refresh2"
    )
    assert other_client is None
    rotated = await repository.rotate_oauth_refresh_token(
        "sha256:refresh1", CHATGPT, "sha256:access2", "sha256:refresh2"
    )
    assert rotated == grant
    again = await repository.rotate_oauth_refresh_token(
        "sha256:refresh2", CHATGPT, "sha256:access3", "sha256:refresh3"
    )
    assert again == grant

    # Replaying the first refresh token after two rotations still revokes the grant.
    reused = await repository.rotate_oauth_refresh_token(
        "sha256:refresh1", CHATGPT, "sha256:access4", "sha256:refresh4"
    )
    assert reused is None
    assert await count(sessions, OAuthGrantRow) == 0
    assert await count(sessions, OAuthTokenRow) == 0


async def test_expired_tokens_and_grants_stop_working_and_are_purged(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    kept = await repository.create_oauth_grant(CODE, "sha256:kept-a", "sha256:kept-r")
    old = await repository.create_oauth_grant(CODE, "sha256:old-a", "sha256:old-r")
    await repository.create_oauth_code("sha256:code", CODE)
    await expire(sessions, OAuthTokenRow, token_hash="sha256:kept-a")
    await expire(sessions, OAuthGrantRow, id=old.id)
    await expire(sessions, OAuthCodeRow)

    assert await repository.oauth_access_grant("sha256:kept-a") is None
    assert await repository.oauth_access_grant("sha256:old-a") is None
    assert (
        await repository.rotate_oauth_refresh_token("sha256:old-r", CHATGPT, "sha256:x", "sha256:y")
        is None
    )

    assert await repository.purge_oauth() == (1, 1, 1)
    async with sessions() as session:
        remaining = set(await session.scalars(select(OAuthTokenRow.token_hash)))
        grant = await session.get(OAuthGrantRow, kept.id)
    assert remaining == {"sha256:kept-r"}
    assert grant is not None
    assert grant.expires_at - grant.created_at == OAUTH_REFRESH_LIFETIME


async def test_revocation_needs_the_client_that_got_the_token(
    repository: PostgresResultRepository, sessions: async_sessionmaker[AsyncSession]
) -> None:
    await repository.create_oauth_grant(CODE, "sha256:a", "sha256:r")

    assert await repository.revoke_oauth_token("sha256:a", "https://claude.ai/x") is False
    assert await repository.revoke_oauth_token("sha256:unknown", CHATGPT) is False
    assert await repository.revoke_oauth_token("sha256:a", CHATGPT) is True
    assert await count(sessions, OAuthTokenRow) == 0


@pytest.fixture
async def api(
    repository: PostgresResultRepository, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    hosts = ClientHosts()
    monkeypatch.setattr(
        oauth_server,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(hosts.handle)),
    )
    settings = main_module.settings
    for name, value in {
        "public_base_url": BASE,
        "oauth_builtin": True,
        "oauth_issuer": None,
        "oauth_client_hosts": "chatgpt.com",
        "api_keys": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(main_module.authorization_server, "registry", oauth_server.ClientRegistry())
    monkeypatch.setattr(main_module, "store", repository)
    monkeypatch.setattr(main_module, "rate_limiter", FixedWindowRateLimiter())
    monkeypatch.setattr(main_module, "file_keys", (user_entry(),))
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


async def test_a_sign_in_through_the_api_is_stored_as_hashes_only(
    api: httpx.AsyncClient, sessions: async_sessionmaker[AsyncSession]
) -> None:
    signed_in = await api.post(
        oauth_server.AUTHORIZE_PATH,
        data={**authorize_params(), "api_key": USER_KEY, "decision": "allow"},
    )
    code = httpx.URL(signed_in.headers["location"]).params["code"]
    issued = await api.post(
        oauth_server.TOKEN_PATH,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "client_id": CHATGPT,
        },
    )
    assert issued.status_code == 200, issued.text
    tokens = issued.json()

    identity = await main_module.authorization_server.identify_token(tokens["access_token"])
    assert identity.name == "chat-user"
    async with sessions() as session:
        stored = set(await session.scalars(select(OAuthTokenRow.token_hash)))
        grant = await session.scalar(select(OAuthGrantRow))
    assert stored == {
        access.hash_key(tokens["access_token"]),
        access.hash_key(tokens["refresh_token"]),
    }
    assert grant is not None and grant.key_hash == access.hash_key(USER_KEY)

    refreshed = await api.post(
        oauth_server.TOKEN_PATH,
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": CHATGPT,
        },
    )
    assert refreshed.status_code == 200
    with pytest.raises(oauth.InvalidToken):
        await main_module.authorization_server.identify_token("fra_unknown")
