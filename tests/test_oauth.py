"""OAuth for `/mcp` (ADR 0006): signing keys and the JWKS are generated locally, no network."""

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

import app.main as main_module
from app import access, oauth
from app.config import Settings
from app.main import app
from app.observability import KEYED_REQUESTS
from tests.test_mcp import call, isolated, post  # noqa: F401

ISSUER = "https://id.example.com/realms/football"
BASE = "http://localhost:8000"
AUDIENCE = f"{BASE}/mcp"
METADATA = f"{BASE}{oauth.METADATA_PATH}"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


class Issuer:
    """A fake authorization server: RSA signing keys and the endpoints the API fetches."""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.requests: list[str] = []
        self.add_key("k1")

    def add_key(self, kid: str) -> None:
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict[str, Any]:
        keys = []
        for kid, key in self.keys.items():
            jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
            keys.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
        return {"keys": keys}

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISSUER, "jwks_uri": JWKS_URL})
        if url == JWKS_URL:
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404)

    def token(self, kid: str = "k1", signer: str | None = None, **claims: Any) -> str:
        now = int(time.time())
        body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1", "iat": now, "exp": now + 300}
        body.update(claims)
        key = self.keys[signer or kid]
        return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def issuer(monkeypatch: pytest.MonkeyPatch) -> Issuer:
    fake = Issuer()
    monkeypatch.setattr(
        oauth,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)),
    )
    settings = main_module.settings
    monkeypatch.setattr(settings, "public_base_url", BASE)
    monkeypatch.setattr(settings, "oauth_issuer", ISSUER)
    monkeypatch.setattr(settings, "oauth_mode", "optional")
    monkeypatch.setattr(settings, "oauth_scopes", "mcp:tools")
    monkeypatch.setattr(main_module.mcp_endpoint, "_verifiers", {})
    return fake


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def subject_entry(name: str, subject: str, role: access.Role) -> access.FileKey:
    return access.FileKey(name, None, role, 7, None, subject=subject)


def test_a_valid_token_is_accepted(issuer: Issuer, client: TestClient) -> None:
    response = post(client, "tools/list", headers=bearer(issuer.token()))

    assert response.status_code == 200
    assert issuer.requests == [f"{ISSUER}/.well-known/openid-configuration", JWKS_URL]
    # The JWKS is cached: a second request fetches nothing.
    post(client, "tools/list", headers=bearer(issuer.token()))
    assert len(issuer.requests) == 2


@pytest.mark.parametrize(
    ("claims", "detail"),
    [
        ({"exp": int(time.time()) - 120}, "The token has expired"),
        ({"nbf": int(time.time()) + 600}, "The token is not valid yet"),
        ({"aud": "https://other.example.com/mcp"}, "audience"),
        ({"iss": "https://evil.example.com"}, "another issuer"),
    ],
)
def test_an_invalid_token_gets_401_with_the_metadata(
    issuer: Issuer, client: TestClient, claims: dict[str, Any], detail: str
) -> None:
    response = post(client, "tools/list", headers=bearer(issuer.token(**claims)))

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "/problems/unauthorized"
    assert detail in response.json()["detail"]
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith('Bearer error="invalid_token"')
    assert f'resource_metadata="{METADATA}"' in challenge


def test_unknown_kid_refetches_the_jwks_once(issuer: Issuer, client: TestClient) -> None:
    assert post(client, "tools/list", headers=bearer(issuer.token())).status_code == 200

    issuer.add_key("k2")
    rotated = post(client, "tools/list", headers=bearer(issuer.token("k2")))
    unknown = post(client, "tools/list", headers=bearer(issuer.token("k3", signer="k1")))

    assert rotated.status_code == 200
    assert unknown.status_code == 401
    assert "unknown key" in unknown.json()["detail"]
    # Initial fetch, one refetch for k2; k3 falls inside the refetch cooldown.
    assert issuer.requests.count(JWKS_URL) == 2


def test_symmetric_and_forged_tokens_are_rejected(issuer: Issuer, client: TestClient) -> None:
    hs256 = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "exp": int(time.time()) + 60},
        "a-shared-secret-of-thirty-two-bytes!",
        algorithm="HS256",
        headers={"kid": "k1"},
    )
    issuer.add_key("other")
    forged = issuer.token("k1", signer="other")

    for token in (hs256, forged, "not-a-jwt", ""):
        response = post(client, "tools/list", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401, token


def test_optional_mode_keeps_anonymous_and_key_access(issuer: Issuer, client: TestClient) -> None:
    assert post(client, "tools/list").status_code == 200
    keyed = post(client, "tools/list", headers={"X-API-Key": "test-api-key"})
    assert keyed.status_code == 200


def test_required_mode_challenges_anonymous_requests(
    issuer: Issuer, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.settings, "oauth_mode", "required")

    anonymous = post(client, "tools/list")
    wrong_key = post(client, "tools/list", headers={"X-API-Key": "wrong"})

    for response in (anonymous, wrong_key):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            f'Bearer resource_metadata="{METADATA}", scope="mcp:tools"'
        )
    assert post(client, "tools/list", headers={"X-API-Key": "test-api-key"}).status_code == 200
    assert post(client, "tools/list", headers=bearer(issuer.token())).status_code == 200
    # The REST API is unaffected by OAuth.
    assert client.get("/api/v1/competitions").status_code == 200


def test_unreachable_issuer_is_a_503(
    issuer: Issuer, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(issuer, "handle", lambda request: httpx.Response(500))

    response = post(client, "tools/list", headers=bearer(issuer.token()))

    assert response.status_code == 503
    assert response.json()["type"] == "/problems/service-unavailable"
    assert response.headers["retry-after"] == "30"


def test_a_mapped_admin_subject_can_request_a_refresh(
    issuer: Issuer, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.settings, "api_keys", "")
    monkeypatch.setattr(
        main_module, "file_keys", (subject_entry("operator", "admin-sub", "admin"),)
    )
    token = issuer.token(sub="admin-sub")

    listed = post(client, "tools/list", headers=bearer(token))
    job, _ = call(client, "request_refresh", bearer(token), competition="ekstraklasa")

    assert listed.headers["ratelimit-policy"] == '"read";q=7;w=60'
    assert job["job_id"] and job["status"] == "queued"


def test_an_unknown_subject_is_an_oauth_client(
    issuer: Issuer, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "file_keys", (subject_entry("ops", "admin-sub", "admin"),))
    before = KEYED_REQUESTS.labels("oauth", "read")._value.get()

    _, forbidden = call(
        client, "request_refresh", bearer(issuer.token(sub="someone")), competition="ekstraklasa"
    )

    assert forbidden["type"] == "/problems/forbidden"
    assert "'oauth'" in forbidden["detail"]
    assert KEYED_REQUESTS.labels("oauth", "read")._value.get() == before + 1


def test_a_valid_api_key_wins_over_the_token(
    issuer: Issuer, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "file_keys", (subject_entry("ops", "user-1", "client"),))
    headers = {**bearer(issuer.token()), "X-API-Key": "test-api-key"}

    job, _ = call(client, "request_refresh", headers, competition="ekstraklasa")

    assert job["status"] == "queued"


def test_without_oauth_the_bearer_header_is_ignored(client: TestClient) -> None:
    response = post(client, "tools/list", headers=bearer("anything"))

    assert response.status_code == 200
    assert client.get(oauth.METADATA_PATH).status_code == 404


def test_the_metadata_route(issuer: Issuer, client: TestClient) -> None:
    response = client.get(oauth.METADATA_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "resource": AUDIENCE,
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
        "resource_name": "Football Results MCP server",
        "scopes_supported": ["mcp:tools"],
        "resource_documentation": f"{BASE}/agents#mcp",
    }
    assert response.headers["access-control-allow-origin"] == "*"
    assert "ratelimit-policy" not in response.headers
    assert oauth.METADATA_PATH not in app.openapi()["paths"]
    etag = response.headers["etag"]
    assert client.get(oauth.METADATA_PATH, headers={"If-None-Match": etag}).status_code == 304


def test_agent_docs_describe_oauth_only_when_configured(
    issuer: Issuer, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents = client.get("/agents.md").text
    assert METADATA in agents and ISSUER in agents
    assert METADATA in client.get("/llms.txt").text

    monkeypatch.setattr(main_module.settings, "oauth_issuer", None)
    assert oauth.METADATA_PATH not in client.get("/agents.md").text


def test_settings_require_a_public_url_for_the_issuer() -> None:
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        Settings(oauth_issuer=ISSUER, public_base_url=None)
    with pytest.raises(ValueError, match="absolute"):
        Settings(oauth_issuer="id.example.com", public_base_url=BASE)

    settings = Settings(oauth_issuer=ISSUER, public_base_url=BASE)
    assert settings.oauth_token_audience == AUDIENCE
    custom = Settings(oauth_issuer=ISSUER, public_base_url=BASE, oauth_audience="api://football")
    assert custom.oauth_token_audience == "api://football"


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([{"name": "a", "role": "admin"}], "exactly one of hash or subject"),
        (
            [{"name": "a", "subject": "s", "hash": access.hash_key("k")}],
            "exactly one of hash or subject",
        ),
        ([{"name": "a", "subject": "s"}, {"name": "b", "subject": "s"}], "same subject"),
        ([{"name": "oauth", "subject": "s"}], "reserved"),
    ],
)
def test_keys_file_subject_entries_are_validated(
    entries: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        access.KeysFile.model_validate({"keys": entries})
