"""Built-in OAuth authorization server for `/mcp` whose sign-in is an API key (ADR 0012).

Just enough OAuth 2.1 for chat clients that cannot send `X-API-Key` (claude.ai, ChatGPT):
authorization code with PKCE (S256), public clients registered by Client ID Metadata Documents
from allowlisted hosts, RFC 8707 `resource`, RFC 9207 `iss`, rotating refresh tokens, and
RFC 7009 revocation. Codes and tokens are opaque random strings stored only as `sha256:` hashes
and bound to the hash of the key the user entered, so every use re-checks that key: a token is
exactly as powerful as its key and stops working when the key expires or is revoked.
"""

import base64
import hashlib
import html
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.access import Identity, hash_key
from app.config import Settings
from app.oauth import InvalidToken
from app.repository import OAUTH_ACCESS_LIFETIME, OAuthCode, OAuthGrant

logger = logging.getLogger(__name__)

METADATA_PATH = "/.well-known/oauth-authorization-server"
AUTHORIZE_PATH = "/oauth/authorize"
TOKEN_PATH = "/oauth/token"
REVOKE_PATH = "/oauth/revoke"
CODE_PREFIX = "frc_"
ACCESS_PREFIX = "fra_"
REFRESH_PREFIX = "frr_"
TOKEN_BYTES = 32
MAX_URL_LENGTH = 2048
MAX_STATE_LENGTH = 1024
MAX_CLIENT_NAME_LENGTH = 200
MAX_FORM_BYTES = 16 * 1024
CIMD_TIMEOUT_SECONDS = 5.0
CIMD_MAX_BYTES = 64 * 1024
CIMD_CACHE_SECONDS = 3600.0
CIMD_FAILURE_CACHE_SECONDS = 60.0
CIMD_CACHE_ENTRIES = 256
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# RFC 7636: a verifier is 43-128 unreserved characters; an S256 challenge is 43 base64url ones.
VERIFIER_PATTERN = re.compile(r"[A-Za-z0-9._~-]{43,128}")
CHALLENGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class OAuthStore(Protocol):
    async def create_oauth_code(self, code_hash: str, code: OAuthCode) -> None: ...

    async def consume_oauth_code(self, code_hash: str) -> OAuthCode | None: ...

    async def create_oauth_grant(
        self, code: OAuthCode, access_hash: str, refresh_hash: str
    ) -> OAuthGrant: ...

    async def rotate_oauth_refresh_token(
        self, refresh_hash: str, client_id: str, access_hash: str, new_refresh_hash: str
    ) -> OAuthGrant | None: ...

    async def oauth_access_grant(self, access_hash: str) -> OAuthGrant | None: ...

    async def delete_oauth_grant(self, grant_id: int) -> None: ...

    async def revoke_oauth_token(self, token_hash: str, client_id: str) -> bool: ...


def create_http_client() -> httpx.AsyncClient:
    """The HTTP client for Client ID Metadata Documents (a test seam)."""
    return httpx.AsyncClient(timeout=CIMD_TIMEOUT_SECONDS, follow_redirects=False)


def generate_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(TOKEN_BYTES)


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def metadata(issuer: str) -> dict[str, Any]:
    """RFC 8414 authorization server metadata."""
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + AUTHORIZE_PATH,
        "token_endpoint": issuer + TOKEN_PATH,
        "revocation_endpoint": issuer + REVOKE_PATH,
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": issuer + "/agents#mcp",
    }


# --- Clients (Client ID Metadata Documents) ------------------------------------------------------


class ClientError(Exception):
    """The client or redirect URI is not acceptable; shown on a page, never sent to the client."""


@dataclass(frozen=True)
class Client:
    client_id: str
    name: str
    host: str
    redirect_uris: tuple[str, ...]


def client_host(client_id: str, allowed_hosts: frozenset[str]) -> str:
    """The host of a valid CIMD client ID (an https URL with a path on an allowed host)."""
    if len(client_id) > MAX_URL_LENGTH:
        raise ClientError("The client_id is too long")
    try:
        parts = urlsplit(client_id)
        port = parts.port
    except ValueError as error:
        raise ClientError("The client_id is not a valid URL") from error
    host = parts.hostname or ""
    segments = parts.path.split("/")
    if (
        parts.scheme != "https"
        or not host
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.path in {"", "/"}
        or "." in segments
        or ".." in segments
    ):
        raise ClientError(
            "The client_id must be an https URL with a path (a Client ID Metadata Document)"
        )
    if host not in allowed_hosts:
        raise ClientError(
            f"Clients from {host} are not allowed on this instance. The operator can add {host} "
            "to FOOTBALL_RESULTS_OAUTH_CLIENT_HOSTS."
        )
    return host


def _valid_redirect(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    if len(uri) > MAX_URL_LENGTH or not parts.hostname or parts.fragment:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)


def parse_client_document(client_id: str, host: str, document: Any) -> Client:
    """Validate a Client ID Metadata Document for a public client."""
    if not isinstance(document, dict):
        raise ClientError("The client metadata document is not a JSON object")
    if document.get("client_id") != client_id:
        raise ClientError("The client metadata document names another client_id")
    redirect_uris = document.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris:
        raise ClientError("The client metadata document lists no redirect_uris")
    valid = tuple(uri for uri in redirect_uris if isinstance(uri, str) and _valid_redirect(uri))
    if not valid:
        raise ClientError("The client metadata document has no usable redirect URI")
    # A client may list several methods (ChatGPT); it then uses one this server advertises.
    methods = document.get("token_endpoint_auth_methods_supported")
    method = document.get("token_endpoint_auth_method")
    public = (
        (isinstance(methods, list) and "none" in methods)
        or method == "none"
        or (methods is None and method is None)
    )
    if not public or "client_secret" in document:
        raise ClientError("The client is not a public client (token_endpoint_auth_method none)")
    for field, required in (("grant_types", "authorization_code"), ("response_types", "code")):
        values = document.get(field)
        if values is not None and (not isinstance(values, list) or required not in values):
            raise ClientError(f"The client metadata document's {field} omit {required}")
    name = document.get("client_name")
    if not isinstance(name, str) or not name.strip():
        name = host
    return Client(client_id, " ".join(name.split())[:MAX_CLIENT_NAME_LENGTH], host, valid)


class ClientRegistry:
    """Fetches and caches Client ID Metadata Documents; failures are cached briefly so a page
    reload does not refetch at once."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._cache: dict[str, tuple[float, Client | ClientError]] = {}

    async def client(self, client_id: str, allowed_hosts: frozenset[str]) -> Client:
        host = client_host(client_id, allowed_hosts)
        now = self.clock()
        cached = self._cache.get(client_id)
        if cached is not None and now < cached[0]:
            if isinstance(cached[1], ClientError):
                raise cached[1]
            return cached[1]
        value: Client | ClientError
        try:
            value = parse_client_document(client_id, host, await self._fetch(client_id))
            lifetime = CIMD_CACHE_SECONDS
        except ClientError as error:
            value, lifetime = error, CIMD_FAILURE_CACHE_SECONDS
        if len(self._cache) >= CIMD_CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[client_id] = (now + lifetime, value)
        if isinstance(value, ClientError):
            raise value
        return value

    @staticmethod
    async def _fetch(client_id: str) -> Any:
        try:
            async with (
                create_http_client() as http,
                http.stream("GET", client_id, headers={"Accept": "application/json"}) as response,
            ):
                if response.status_code != 200:
                    raise ClientError(
                        f"The client metadata document returned HTTP {response.status_code}"
                    )
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > CIMD_MAX_BYTES:
                        raise ClientError("The client metadata document is too large")
        except httpx.HTTPError as error:
            logger.warning("client metadata document %s not fetched: %s", client_id, error)
            raise ClientError("The client metadata document cannot be fetched") from error
        try:
            return json.loads(body)
        except ValueError as error:
            raise ClientError("The client metadata document is not JSON") from error


def redirect_allowed(requested: str, registered: tuple[str, ...]) -> bool:
    """Exact match, except that a loopback redirect may use any port (RFC 8252 §7.3)."""
    if requested in registered:
        return True
    if not _valid_redirect(requested):
        return False
    wanted = urlsplit(requested)
    if wanted.scheme != "http" or wanted.hostname not in LOOPBACK_HOSTS:
        return False
    for uri in registered:
        parts = urlsplit(uri)
        if (parts.scheme, parts.hostname, parts.path, parts.query) == (
            wanted.scheme,
            wanted.hostname,
            wanted.path,
            wanted.query,
        ):
            return True
    return False


# --- Authorization endpoint ----------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationRequest:
    client: Client
    redirect_uri: str
    state: str | None
    code_challenge: str
    resource: str


class RedirectError(Exception):
    """An authorization error that is returned to the client at its redirect URI."""

    def __init__(self, error: str, description: str, redirect_uri: str, state: str | None):
        super().__init__(description)
        self.error, self.description = error, description
        self.redirect_uri, self.state = redirect_uri, state


def _single_values(items: list[tuple[str, str]]) -> dict[str, str]:
    """Parameters must not repeat (RFC 6749 §3.1); a repeated one counts as missing."""
    values: dict[str, str] = {}
    repeated: set[str] = set()
    for key, value in items:
        if key in values:
            repeated.add(key)
        values[key] = value
    return {key: value for key, value in values.items() if key not in repeated}


def _same_resource(requested: str, resource: str) -> bool:
    return requested.rstrip("/") == resource.rstrip("/")


def with_query(uri: str, params: Mapping[str, str | None]) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value is not None)
    return urlunsplit(parts._replace(query=urlencode(query)))


async def _form(request: Request) -> list[tuple[str, str]]:
    """An `application/x-www-form-urlencoded` body (python-multipart is not a dependency)."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("The body must be application/x-www-form-urlencoded")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise ValueError("The body is too large")
    try:
        return parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=50)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("The body is not a valid form") from error


class AuthorizationServer:
    """Endpoints of the built-in authorization server; settings and keys are read per request."""

    def __init__(
        self,
        settings: Settings,
        store: Callable[[], OAuthStore],
        resolve_key: Callable[[str], Identity | None],
        resolve_key_hash: Callable[[str], Identity | None],
        registry: ClientRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.resolve_key = resolve_key
        self.resolve_key_hash = resolve_key_hash
        self.registry = registry or ClientRegistry()

    @property
    def enabled(self) -> bool:
        return self.settings.oauth_builtin and bool(self.settings.public_base_url)

    @property
    def issuer(self) -> str:
        return self.settings.public_base_url or ""

    @property
    def resource(self) -> str:
        return f"{self.issuer}/mcp"

    def metadata(self) -> dict[str, Any]:
        return metadata(self.issuer)

    async def _authorization_request(self, params: dict[str, str]) -> AuthorizationRequest:
        """Raises `ClientError` (shown as a page) or `RedirectError` (sent to the client)."""
        client_id = params.get("client_id")
        if not client_id:
            raise ClientError("The request has no client_id")
        client = await self.registry.client(client_id, self.settings.oauth_client_host_set)
        redirect_uri = params.get("redirect_uri")
        if redirect_uri is None and len(client.redirect_uris) == 1:
            redirect_uri = client.redirect_uris[0]
        if redirect_uri is None or not redirect_allowed(redirect_uri, client.redirect_uris):
            raise ClientError(f"The redirect_uri is not registered for {client.name}")
        state = params.get("state")
        if state is not None and len(state) > MAX_STATE_LENGTH:
            raise RedirectError("invalid_request", "state is too long", redirect_uri, None)

        def fail(error: str, description: str) -> RedirectError:
            return RedirectError(error, description, redirect_uri, state)

        if params.get("response_type") != "code":
            raise fail("unsupported_response_type", "Only response_type=code is supported")
        challenge = params.get("code_challenge")
        if not challenge or params.get("code_challenge_method") != "S256":
            raise fail("invalid_request", "PKCE with code_challenge_method=S256 is required")
        if not CHALLENGE_PATTERN.fullmatch(challenge):
            raise fail("invalid_request", "code_challenge is not an S256 challenge")
        resource = params.get("resource", self.resource)
        if not _same_resource(resource, self.resource):
            raise fail("invalid_target", f"The only resource is {self.resource}")
        return AuthorizationRequest(client, redirect_uri, state, challenge, self.resource)

    def _error_redirect(self, error: RedirectError) -> Response:
        location = with_query(
            error.redirect_uri,
            {
                "error": error.error,
                "error_description": error.description,
                "state": error.state,
                "iss": self.issuer,
            },
        )
        return RedirectResponse(location, status_code=303, headers=NO_STORE)

    async def authorize(self, request: Request) -> Response:
        """`GET` shows the consent page; `POST` checks the key and redirects with a code."""
        if request.method == "GET":
            params = _single_values(request.query_params.multi_items())
        else:
            try:
                params = _single_values(await _form(request))
            except ValueError as error:
                return _page(_error_body(str(error)), 400)
        try:
            authorization = await self._authorization_request(params)
        except ClientError as error:
            return _page(_error_body(str(error)), 400)
        except RedirectError as error:
            return self._error_redirect(error)
        if request.method == "GET":
            return _consent(authorization, None, 200)
        if params.get("decision") != "allow":
            return self._error_redirect(
                RedirectError(
                    "access_denied",
                    "The user cancelled the sign-in",
                    authorization.redirect_uri,
                    authorization.state,
                )
            )
        key = params.get("api_key", "").strip()
        identity = self.resolve_key(key) if key else None
        if identity is None:
            return _consent(authorization, "This key is not valid or has expired.", 400)
        code = generate_token(CODE_PREFIX)
        await self.store().create_oauth_code(
            hash_key(code),
            OAuthCode(
                client_id=authorization.client.client_id,
                client_name=authorization.client.name,
                redirect_uri=authorization.redirect_uri,
                code_challenge=authorization.code_challenge,
                resource=authorization.resource,
                key_hash=hash_key(key),
            ),
        )
        logger.info("OAuth: %s signed in with key %s", authorization.client.host, identity.name)
        location = with_query(
            authorization.redirect_uri,
            {"code": code, "state": authorization.state, "iss": self.issuer},
        )
        return RedirectResponse(
            location, status_code=303, headers=_page_headers(authorization.redirect_uri)
        )

    # --- Token and revocation endpoints ----------------------------------------------------------

    async def token(self, request: Request) -> Response:
        if request.headers.get("authorization"):
            return _token_error(
                "invalid_client", "Only public clients are supported", 401, challenge=True
            )
        try:
            params = _single_values(await _form(request))
        except ValueError as error:
            return _token_error("invalid_request", str(error))
        if "client_secret" in params or "client_assertion" in params:
            return _token_error("invalid_client", "Only public clients are supported", 401)
        client_id = params.get("client_id")
        if not client_id:
            return _token_error("invalid_request", "client_id is required")
        resource = params.get("resource")
        if resource is not None and not _same_resource(resource, self.resource):
            return _token_error("invalid_target", f"The only resource is {self.resource}")
        grant_type = params.get("grant_type")
        if grant_type == "authorization_code":
            return await self._exchange_code(params, client_id)
        if grant_type == "refresh_token":
            return await self._refresh(params, client_id)
        return _token_error("unsupported_grant_type", "Use authorization_code or refresh_token")

    async def _exchange_code(self, params: dict[str, str], client_id: str) -> Response:
        code, verifier = params.get("code"), params.get("code_verifier")
        if not code or not verifier:
            return _token_error("invalid_request", "code and code_verifier are required")
        stored = await self.store().consume_oauth_code(hash_key(code))
        invalid = _token_error("invalid_grant", "The code is invalid, expired, or already used")
        if stored is None or stored.client_id != client_id:
            return invalid
        if params.get("redirect_uri", stored.redirect_uri) != stored.redirect_uri:
            return _token_error("invalid_grant", "redirect_uri does not match the authorization")
        if not VERIFIER_PATTERN.fullmatch(verifier) or not secrets.compare_digest(
            s256(verifier), stored.code_challenge
        ):
            return _token_error("invalid_grant", "code_verifier does not match the challenge")
        if self.resolve_key_hash(stored.key_hash) is None:
            return _token_error("invalid_grant", "The API key was revoked or has expired")
        access_token, refresh_token = generate_token(ACCESS_PREFIX), generate_token(REFRESH_PREFIX)
        await self.store().create_oauth_grant(
            stored, hash_key(access_token), hash_key(refresh_token)
        )
        return _tokens(access_token, refresh_token)

    async def _refresh(self, params: dict[str, str], client_id: str) -> Response:
        refresh_token = params.get("refresh_token")
        if not refresh_token:
            return _token_error("invalid_request", "refresh_token is required")
        access_token, new_refresh = generate_token(ACCESS_PREFIX), generate_token(REFRESH_PREFIX)
        grant = await self.store().rotate_oauth_refresh_token(
            hash_key(refresh_token), client_id, hash_key(access_token), hash_key(new_refresh)
        )
        if grant is None:
            return _token_error("invalid_grant", "The refresh token is invalid or expired")
        if self.resolve_key_hash(grant.key_hash) is None:
            await self.store().delete_oauth_grant(grant.id)
            return _token_error("invalid_grant", "The API key was revoked or has expired")
        return _tokens(access_token, new_refresh)

    async def revoke(self, request: Request) -> Response:
        """RFC 7009: 200 whether or not the token existed."""
        try:
            params = _single_values(await _form(request))
        except ValueError as error:
            return _token_error("invalid_request", str(error))
        token, client_id = params.get("token"), params.get("client_id")
        if not token or not client_id:
            return _token_error("invalid_request", "token and client_id are required")
        await self.store().revoke_oauth_token(hash_key(token), client_id)
        return Response(status_code=200, headers={**NO_STORE, "Access-Control-Allow-Origin": "*"})

    async def identify_token(self, token: str) -> Identity:
        """The identity behind a `/mcp` access token; raises `InvalidToken`. Database errors
        propagate (the caller maps them to `IssuerUnavailable`)."""
        if not token.startswith(ACCESS_PREFIX):
            raise InvalidToken("The token is not valid")
        grant = await self.store().oauth_access_grant(hash_key(token))
        if grant is None or grant.resource != self.resource:
            raise InvalidToken("The token is invalid or has expired")
        identity = self.resolve_key_hash(grant.key_hash)
        if identity is None:
            raise InvalidToken("The API key behind this sign-in was revoked or has expired")
        return identity


def _tokens(access_token: str, refresh_token: str) -> JSONResponse:
    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": int(OAUTH_ACCESS_LIFETIME.total_seconds()),
            "refresh_token": refresh_token,
        },
        headers={**NO_STORE, "Access-Control-Allow-Origin": "*"},
    )


def _token_error(
    error: str, description: str, status: int = 400, *, challenge: bool = False
) -> JSONResponse:
    """An RFC 6749 §5.2 error (not a problem document: OAuth clients parse this shape)."""
    headers = {**NO_STORE, "Access-Control-Allow-Origin": "*"}
    if challenge:
        headers["WWW-Authenticate"] = 'Basic realm="oauth"'
    return JSONResponse(
        {"error": error, "error_description": description}, status_code=status, headers=headers
    )


# --- Pages ---------------------------------------------------------------------------------------

_STYLE = """
body{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1b1f24}
main{max-width:30rem;margin:3rem auto;padding:2rem;background:#fff;border-radius:12px;
box-shadow:0 1px 3px rgba(0,0,0,.12)}
h1{font-size:1.35rem;margin-top:0}p{line-height:1.5}code{word-break:break-all}
.warn{background:#fff4d6;border-left:4px solid #d69e00;padding:.6rem .8rem}
.error{background:#fde8e8;border-left:4px solid #c62828;padding:.6rem .8rem}
label{display:block;font-weight:600;margin:1rem 0 .3rem}
input[type=password]{width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem;
border:1px solid #9aa4b2;border-radius:6px}
.actions{display:flex;gap:.6rem;margin-top:1.2rem}
button{padding:.6rem 1.2rem;font-size:1rem;border-radius:6px;border:1px solid #1d4ed8;
cursor:pointer}
.primary{background:#1d4ed8;color:#fff}.secondary{background:#fff;color:#1d4ed8}
@media (prefers-color-scheme:dark){body{background:#0f1216;color:#e6e9ee}
main{background:#181c22}.warn{background:#3a3010}.error{background:#3d1717}
.secondary{background:#181c22}}
"""


def _page_headers(redirect_uri: str | None = None) -> dict[str, str]:
    form_action = "'self'"
    if redirect_uri:
        parts = urlsplit(redirect_uri)
        # Browsers apply form-action to the redirect after the form is posted.
        form_action += f" {parts.scheme}://{parts.netloc}"
    return {
        **NO_STORE,
        "Content-Security-Policy": (
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"form-action {form_action}; frame-ancestors 'none'; base-uri 'none'"
        ),
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }


def _page(body: str, status: int, redirect_uri: str | None = None) -> HTMLResponse:
    document = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>Sign in - Football Results</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
    return HTMLResponse(document, status_code=status, headers=_page_headers(redirect_uri))


def _error_body(message: str) -> str:
    return (
        "<h1>This sign-in cannot continue</h1>"
        f'<p class="error">{html.escape(message)}</p>'
        "<p>Close this window and start the connection again from your app.</p>"
    )


def _consent(authorization: AuthorizationRequest, error: str | None, status: int) -> Response:
    client = authorization.client
    name, host = html.escape(client.name), html.escape(client.host)
    hidden = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": authorization.redirect_uri,
        "state": authorization.state,
        "code_challenge": authorization.code_challenge,
        "code_challenge_method": "S256",
        "resource": authorization.resource,
    }
    inputs = "".join(
        f'<input type="hidden" name="{key}" value="{html.escape(value)}">'
        for key, value in hidden.items()
        if value is not None
    )
    message = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = (
        f"<h1>Connect {name} to Football Results</h1>"
        f"<p><strong>{name}</strong> (<code>{host}</code>) wants to use this instance's MCP "
        "server with your API key.</p>"
        '<p class="warn">Only continue if you started this connection yourself.</p>'
        f"{message}"
        f'<form method="post" action="{AUTHORIZE_PATH}">{inputs}'
        '<label for="api_key">Your API key</label>'
        '<input type="password" id="api_key" name="api_key" autocomplete="off" '
        'spellcheck="false" autofocus>'
        f"<p>{host} gets a token, not your key. The token can do what your key can do and stops "
        "working when your key expires or is revoked.</p>"
        '<div class="actions">'
        '<button class="primary" type="submit" name="decision" value="allow">Connect</button>'
        '<button class="secondary" type="submit" name="decision" value="deny">Cancel</button>'
        "</div></form>"
    )
    return _page(body, status, authorization.redirect_uri)
