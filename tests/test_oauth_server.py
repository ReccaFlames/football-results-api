"""The built-in authorization server (ADR 0012): CIMD documents are served by a mock transport and
codes and tokens live in an in-memory store, so nothing touches the network or a database."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
from app import access, oauth, oauth_server
from app.config import Settings, oauth_server_errors
from app.main import app
from app.repository import OAuthCode, OAuthGrant
from tests.test_api import FakeRepository
from tests.test_mcp import isolated, post  # noqa: F401

BASE = "http://localhost:8000"
RESOURCE = f"{BASE}/mcp"
CHATGPT = "https://chatgpt.com/oauth/client.json"
CHATGPT_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
CLAUDE = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 43
USER_KEY = "frk_chat-user-key"
DOCUMENTS: dict[str, dict[str, Any]] = {
    # The live ChatGPT document lists both methods; it uses the one the server advertises.
    CHATGPT: {
        "client_id": CHATGPT,
        "client_name": "ChatGPT",
        "redirect_uris": [CHATGPT_REDIRECT],
        "token_endpoint_auth_method": "private_key_jwt",
        "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    },
    CLAUDE: {
        "client_id": CLAUDE,
        "client_name": "Claude",
        "redirect_uris": [CLAUDE_REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": [
            "authorization_code",
            "refresh_token",
            "urn:ietf:params:oauth:grant-type:jwt-bearer",
        ],
    },
}


class OAuthRepository(FakeRepository):
    """`FakeRepository` plus the authorization server's codes, grants, and tokens."""

    def __init__(self) -> None:
        super().__init__()
        self.codes: dict[str, OAuthCode] = {}
        self.grants: dict[int, OAuthGrant] = {}
        # token hash -> [grant id, kind, rotated]
        self.tokens: dict[str, list[Any]] = {}

    async def create_oauth_code(self, code_hash: str, code: OAuthCode) -> None:
        self.codes[code_hash] = code

    async def consume_oauth_code(self, code_hash: str) -> OAuthCode | None:
        return self.codes.pop(code_hash, None)

    def _issue(self, grant_id: int, access_hash: str, refresh_hash: str) -> None:
        self.tokens[access_hash] = [grant_id, "access", False]
        self.tokens[refresh_hash] = [grant_id, "refresh", False]

    async def create_oauth_grant(
        self, code: OAuthCode, access_hash: str, refresh_hash: str
    ) -> OAuthGrant:
        grant = OAuthGrant(
            len(self.grants) + 1, code.client_id, code.client_name, code.key_hash, code.resource
        )
        self.grants[grant.id] = grant
        self._issue(grant.id, access_hash, refresh_hash)
        return grant

    async def rotate_oauth_refresh_token(
        self, refresh_hash: str, client_id: str, access_hash: str, new_refresh_hash: str
    ) -> OAuthGrant | None:
        token = self.tokens.get(refresh_hash)
        if token is None or token[1] != "refresh":
            return None
        grant = self.grants.get(token[0])
        if grant is None or grant.client_id != client_id:
            return None
        if token[2]:
            await self.delete_oauth_grant(grant.id)
            return None
        token[2] = True
        self._issue(grant.id, access_hash, new_refresh_hash)
        return grant

    async def oauth_access_grant(self, access_hash: str) -> OAuthGrant | None:
        token = self.tokens.get(access_hash)
        return self.grants.get(token[0]) if token and token[1] == "access" else None

    async def delete_oauth_grant(self, grant_id: int) -> None:
        self.grants.pop(grant_id, None)
        self.tokens = {key: value for key, value in self.tokens.items() if value[0] != grant_id}

    async def revoke_oauth_token(self, token_hash: str, client_id: str) -> bool:
        token = self.tokens.get(token_hash)
        grant = self.grants.get(token[0]) if token else None
        if grant is None or grant.client_id != client_id:
            return False
        await self.delete_oauth_grant(grant.id)
        return True


class ClientHosts:
    """Serves the CIMD documents and records every fetched URL."""

    def __init__(self) -> None:
        self.documents = {url: dict(document) for url, document in DOCUMENTS.items()}
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.documents:
            return httpx.Response(200, json=self.documents[url])
        return httpx.Response(404)


@pytest.fixture
def hosts(monkeypatch: pytest.MonkeyPatch) -> ClientHosts:
    fake = ClientHosts()
    monkeypatch.setattr(
        oauth_server,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)),
    )
    return fake


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch, hosts: ClientHosts) -> OAuthRepository:
    settings = main_module.settings
    monkeypatch.setattr(settings, "public_base_url", BASE)
    monkeypatch.setattr(settings, "oauth_builtin", True)
    monkeypatch.setattr(settings, "oauth_issuer", None)
    monkeypatch.setattr(settings, "oauth_mode", "optional")
    monkeypatch.setattr(settings, "oauth_client_hosts", "claude.ai,chatgpt.com")
    monkeypatch.setattr(main_module.authorization_server, "registry", oauth_server.ClientRegistry())
    store = OAuthRepository()
    monkeypatch.setattr(main_module, "store", store)
    return store


@pytest.fixture
def client(repository: OAuthRepository, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as client:
        # After the lifespan, which loads the (unset) keys file.
        monkeypatch.setattr(main_module, "file_keys", (user_entry(),))
        yield client


def user_entry(expires_in: timedelta = timedelta(days=30)) -> access.FileKey:
    digest = bytes.fromhex(access.hash_key(USER_KEY).removeprefix(access.HASH_PREFIX))
    now = datetime.now(UTC)
    return access.FileKey(
        "chat-user", digest, "client", None, None, issued_at=now, expires_at=now + expires_in
    )


def authorize_params(client_id: str = CHATGPT, **overrides: str | None) -> dict[str, str]:
    params: dict[str, str | None] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CHATGPT_REDIRECT if client_id == CHATGPT else CLAUDE_REDIRECT,
        "state": "xyz",
        "code_challenge": oauth_server.s256(VERIFIER),
        "code_challenge_method": "S256",
        "resource": RESOURCE,
        **overrides,
    }
    return {key: value for key, value in params.items() if value is not None}


def redirect_query(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 303, response.text
    return {
        key: values[0]
        for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()
    }


def sign_in(client: TestClient, client_id: str = CHATGPT, key: str = USER_KEY) -> dict[str, str]:
    response = client.post(
        oauth_server.AUTHORIZE_PATH,
        data={**authorize_params(client_id), "api_key": key, "decision": "allow"},
    )
    return redirect_query(response)


def exchange(client: TestClient, code: str, client_id: str = CHATGPT, **extra: str) -> Any:
    return client.post(
        oauth_server.TOKEN_PATH,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": VERIFIER,
            "client_id": client_id,
            "redirect_uri": CHATGPT_REDIRECT if client_id == CHATGPT else CLAUDE_REDIRECT,
            "resource": RESOURCE,
            **extra,
        },
    )


def refresh(client: TestClient, token: str, client_id: str = CHATGPT) -> Any:
    return client.post(
        oauth_server.TOKEN_PATH,
        data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id},
    )


def tokens(client: TestClient, client_id: str = CHATGPT) -> dict[str, Any]:
    response = exchange(client, sign_in(client, client_id)["code"], client_id)
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_metadata_names_the_own_issuer_only_when_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = client.get(oauth_server.METADATA_PATH)
    resource = client.get(oauth.METADATA_PATH).json()

    assert metadata.status_code == 200
    body = metadata.json()
    # ChatGPT requires the issuer, the PRM's authorization server, and `iss` to match exactly.
    assert body["issuer"] == BASE == resource["authorization_servers"][0]
    assert resource["resource"] == RESOURCE and "scopes_supported" not in resource
    assert body["token_endpoint"] == f"{BASE}/oauth/token"
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert body["token_endpoint_auth_methods_supported"] == ["none"]
    assert body["client_id_metadata_document_supported"] is True
    assert body["authorization_response_iss_parameter_supported"] is True
    assert "scopes_supported" not in body and "registration_endpoint" not in body

    monkeypatch.setattr(main_module.settings, "oauth_builtin", False)
    assert client.get(oauth_server.METADATA_PATH).status_code == 404
    assert client.get(oauth_server.AUTHORIZE_PATH).status_code == 404
    assert client.post(oauth_server.TOKEN_PATH).status_code == 404


@pytest.mark.parametrize("client_id", [CHATGPT, CLAUDE])
def test_a_chat_client_signs_in_with_an_api_key_and_calls_mcp(
    client: TestClient, hosts: ClientHosts, client_id: str
) -> None:
    page = client.get(oauth_server.AUTHORIZE_PATH, params=authorize_params(client_id))

    assert page.status_code == 200
    name, host = ("ChatGPT", "chatgpt.com") if client_id == CHATGPT else ("Claude", "claude.ai")
    assert f"Connect {name} to Football Results" in page.text and host in page.text
    assert "<script" not in page.text and 'type="password"' in page.text
    policy = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in policy and f"form-action 'self' https://{host}" in policy
    assert page.headers["cache-control"] == "no-store"
    assert hosts.requests == [client_id]

    wrong = client.post(
        oauth_server.AUTHORIZE_PATH,
        data={**authorize_params(client_id), "api_key": "frk_wrong", "decision": "allow"},
    )
    assert wrong.status_code == 400 and "not valid or has expired" in wrong.text

    query = sign_in(client, client_id)
    assert query["state"] == "xyz" and query["iss"] == BASE
    assert query["code"].startswith("frc_")

    response = exchange(client, query["code"], client_id)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    assert body["access_token"].startswith("fra_") and body["refresh_token"].startswith("frr_")

    called = post(client, "tools/list", headers=bearer(body["access_token"]))
    assert called.status_code == 200
    # The token acts as the key: same identity, so the key's expiry is announced.
    assert "api-key-expires" in called.headers
    # The client document is cached: the whole flow fetched it once.
    assert hosts.requests == [client_id]


def test_a_code_works_once_and_only_with_its_verifier(client: TestClient) -> None:
    code = sign_in(client)["code"]
    assert exchange(client, code, code_verifier="w" * 43).json()["error"] == "invalid_grant"
    # The failed attempt used the code up.
    assert exchange(client, code).json()["error"] == "invalid_grant"

    code = sign_in(client)["code"]
    assert exchange(client, code, client_id=CLAUDE).json()["error"] == "invalid_grant"
    code = sign_in(client)["code"]
    assert exchange(client, code).status_code == 200
    assert exchange(client, code).json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge": "short"}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"resource": "https://other.example.com/mcp"}, "invalid_target"),
    ],
)
def test_request_errors_go_back_to_the_client_with_iss(
    client: TestClient, overrides: dict[str, str | None], error: str
) -> None:
    response = client.get(oauth_server.AUTHORIZE_PATH, params=authorize_params(**overrides))

    query = redirect_query(response)
    assert response.headers["location"].startswith(CHATGPT_REDIRECT + "?")
    assert query["error"] == error and query["state"] == "xyz" and query["iss"] == BASE


def test_cancel_returns_access_denied(client: TestClient) -> None:
    response = client.post(
        oauth_server.AUTHORIZE_PATH, data={**authorize_params(), "decision": "deny"}
    )

    assert redirect_query(response)["error"] == "access_denied"


@pytest.mark.parametrize(
    ("client_id", "redirect_uri", "message"),
    [
        (
            "https://evil.example.com/client.json",
            CHATGPT_REDIRECT,
            "FOOTBALL_RESULTS_OAUTH_CLIENT_HOSTS",
        ),
        ("http://chatgpt.com/oauth/client.json", CHATGPT_REDIRECT, "https URL with a path"),
        ("https://chatgpt.com/", CHATGPT_REDIRECT, "https URL with a path"),
        (CHATGPT, "https://evil.example.com/callback", "redirect_uri is not registered"),
        ("https://chatgpt.com/missing.json", CHATGPT_REDIRECT, "HTTP 404"),
    ],
)
def test_an_unacceptable_client_or_redirect_gets_a_page_not_a_redirect(
    client: TestClient, hosts: ClientHosts, client_id: str, redirect_uri: str, message: str
) -> None:
    response = client.get(
        oauth_server.AUTHORIZE_PATH,
        params=authorize_params(client_id, redirect_uri=redirect_uri),
    )

    assert response.status_code == 400
    assert "location" not in response.headers
    assert message in response.text
    # Only allowlisted hosts are ever fetched.
    assert all(urlsplit(url).hostname == "chatgpt.com" for url in hosts.requests)


def test_refresh_rotates_and_reuse_revokes_the_grant(client: TestClient) -> None:
    first = tokens(client)

    rotated = refresh(client, first["refresh_token"])
    assert rotated.status_code == 200
    second = rotated.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert post(client, "tools/list", headers=bearer(second["access_token"])).status_code == 200

    reused = refresh(client, first["refresh_token"])
    assert reused.json()["error"] == "invalid_grant"
    # Reuse means the token leaked: everything of the grant stops working.
    assert refresh(client, second["refresh_token"]).json()["error"] == "invalid_grant"
    assert post(client, "tools/list", headers=bearer(second["access_token"])).status_code == 401


def test_a_revoked_key_ends_the_sign_in_at_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = tokens(client)
    monkeypatch.setattr(main_module, "file_keys", (user_entry(timedelta(seconds=-1)),))

    response = post(client, "tools/list", headers=bearer(issued["access_token"]))
    assert response.status_code == 401
    assert "revoked or has expired" in response.json()["detail"]
    assert 'error="invalid_token"' in response.headers["www-authenticate"]
    assert refresh(client, issued["refresh_token"]).json()["error"] == "invalid_grant"

    monkeypatch.setattr(main_module, "file_keys", (user_entry(),))
    # The refresh attempt deleted the grant, so a renewed key does not revive it.
    assert refresh(client, issued["refresh_token"]).json()["error"] == "invalid_grant"


def test_revocation_deletes_the_grant(client: TestClient) -> None:
    issued = tokens(client)

    other = client.post(
        oauth_server.REVOKE_PATH, data={"token": issued["refresh_token"], "client_id": CLAUDE}
    )
    assert other.status_code == 200
    assert post(client, "tools/list", headers=bearer(issued["access_token"])).status_code == 200

    revoked = client.post(
        oauth_server.REVOKE_PATH, data={"token": issued["refresh_token"], "client_id": CHATGPT}
    )
    assert revoked.status_code == 200
    assert post(client, "tools/list", headers=bearer(issued["access_token"])).status_code == 401


def test_the_token_endpoint_accepts_only_public_clients(client: TestClient) -> None:
    code = sign_in(client)["code"]

    secret = exchange(client, code, client_secret="s3cret")
    basic = client.post(
        oauth_server.TOKEN_PATH,
        data={"grant_type": "authorization_code", "code": code},
        headers={"Authorization": "Basic Y2xpZW50OnNlY3JldA=="},
    )
    json_body = client.post(oauth_server.TOKEN_PATH, json={"grant_type": "refresh_token"})

    assert secret.status_code == 401 and secret.json()["error"] == "invalid_client"
    assert basic.status_code == 401 and "www-authenticate" in basic.headers
    assert json_body.status_code == 400 and json_body.json()["error"] == "invalid_request"
    assert refresh(client, "frr_unknown").json()["error"] == "invalid_grant"


def test_a_private_instance_serves_sign_in_without_a_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.settings, "anonymous_access", False)

    assert client.get(oauth_server.METADATA_PATH).status_code == 200
    assert client.get(oauth.METADATA_PATH).status_code == 200
    unsigned = post(client, "tools/list")
    assert unsigned.status_code == 401
    assert (
        f'resource_metadata="{BASE}{oauth.METADATA_PATH}"' in (unsigned.headers["www-authenticate"])
    )
    issued = tokens(client)
    assert post(client, "tools/list", headers=bearer(issued["access_token"])).status_code == 200
    # Tokens of an external issuer or other strings are rejected without a lookup.
    assert post(client, "tools/list", headers=bearer("eyJhbGciOi.x.y")).status_code == 401


def test_agent_docs_and_privacy_describe_the_sign_in(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = client.get("/agents.md").text
    assert f"add `{RESOURCE}` as a custom connector and sign in with an API key" in agents
    assert f"`{BASE}/.well-known/oauth-authorization-server`" in agents
    assert "(`chatgpt.com`, `claude.ai`)" in agents
    assert "sign-in page asks for an API key" in client.get("/llms.txt").text

    privacy = client.get("/privacy.md").text
    assert "## Chat-client sign-in" in privacy
    assert "codes expire after 5 minutes and access tokens after one hour" in privacy
    assert "not refreshed within 30 days" in privacy

    monkeypatch.setattr(main_module.settings, "oauth_builtin", False)
    assert "Chat-client sign-in" not in client.get("/privacy.md").text
    assert "custom connector" not in client.get("/agents.md").text


def test_client_documents_must_describe_a_public_client() -> None:
    parse = oauth_server.parse_client_document
    chatgpt = parse(CHATGPT, "chatgpt.com", DOCUMENTS[CHATGPT])
    assert chatgpt.redirect_uris == (CHATGPT_REDIRECT,) and chatgpt.name == "ChatGPT"
    assert parse(CLAUDE, "claude.ai", DOCUMENTS[CLAUDE]).name == "Claude"

    confidential = {**DOCUMENTS[CLAUDE], "token_endpoint_auth_method": "private_key_jwt"}
    for document, message in [
        (confidential, "public client"),
        ({**DOCUMENTS[CLAUDE], "client_id": CHATGPT}, "another client_id"),
        ({**DOCUMENTS[CLAUDE], "redirect_uris": ["javascript:alert(1)"]}, "usable redirect"),
        ({**DOCUMENTS[CLAUDE], "grant_types": ["client_credentials"]}, "authorization_code"),
        ([], "JSON object"),
    ]:
        with pytest.raises(oauth_server.ClientError, match=message):
            parse(CLAUDE, "claude.ai", document)


def test_loopback_redirects_may_use_any_port() -> None:
    registered = ("http://127.0.0.1/callback", "https://app.example.com/cb")

    assert oauth_server.redirect_allowed("http://127.0.0.1:53682/callback", registered)
    assert oauth_server.redirect_allowed("https://app.example.com/cb", registered)
    assert not oauth_server.redirect_allowed("http://127.0.0.1:53682/other", registered)
    assert not oauth_server.redirect_allowed("https://app.example.com:8443/cb", registered)
    assert not oauth_server.redirect_allowed("http://app.example.com/cb", registered)


def test_settings_keep_the_built_in_server_separate_and_safe() -> None:
    base = {"oauth_builtin": True, "public_base_url": "https://football.example.com"}
    with pytest.raises(ValidationError, match="unset FOOTBALL_RESULTS_OAUTH_ISSUER"):
        Settings(**base, oauth_issuer="https://id.example.com")
    with pytest.raises(ValidationError, match="requires FOOTBALL_RESULTS_PUBLIC_BASE_URL"):
        Settings(oauth_builtin=True, public_base_url=None)
    with pytest.raises(ValidationError, match="lowercase host name"):
        Settings(**base, oauth_client_hosts="https://claude.ai")

    keys = (user_entry(),)
    assert oauth_server_errors(Settings(**base, api_keys=""), keys) == []
    assert oauth_server_errors(Settings(demo_mode=True), ()) == []
    errors = oauth_server_errors(
        Settings(
            oauth_builtin=True,
            public_base_url="http://football.example.com/api",
            demo_mode=True,
            api_keys="",
            oauth_client_hosts="",
        ),
        (),
    )
    assert [re.split(r"[ ;]", item)[0] for item in errors] == [
        "FOOTBALL_RESULTS_DEMO_MODE",
        "no",
        "FOOTBALL_RESULTS_OAUTH_CLIENT_HOSTS",
        "FOOTBALL_RESULTS_PUBLIC_BASE_URL",
        "FOOTBALL_RESULTS_PUBLIC_BASE_URL",
    ]
