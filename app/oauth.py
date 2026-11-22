"""Optional OAuth resource server for `/mcp` (ADR 0006).

The API never issues tokens. An operator points `FOOTBALL_RESULTS_OAUTH_ISSUER` at an external
authorization server; `/mcp` then publishes RFC 9728 protected resource metadata and accepts JWT
access tokens signed with one of the issuer's JWKS keys (asymmetric algorithms only) whose `iss`,
`aud`, `exp`, and `nbf` are valid. Tokens are never forwarded: callers get an identity instead.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import jwt
from jwt import PyJWK
from mcp.server.auth.provider import AccessToken, TokenVerifier

from app.config import Settings

logger = logging.getLogger(__name__)

METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)
LEEWAY_SECONDS = 30
JWKS_MAX_AGE_SECONDS = 3600
# An unknown `kid` triggers at most one JWKS refetch per this many seconds.
JWKS_REFETCH_COOLDOWN_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 10.0

Mode = Literal["optional", "required"]


@dataclass(frozen=True)
class OAuthConfig:
    issuer: str
    audience: str
    resource: str
    mode: Mode
    scopes: tuple[str, ...]
    jwks_url: str | None
    metadata_url: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "OAuthConfig | None":
        if not settings.oauth_issuer or not settings.public_base_url:
            return None
        resource = f"{settings.public_base_url}/mcp"
        return cls(
            issuer=settings.oauth_issuer,
            audience=settings.oauth_audience or resource,
            resource=resource,
            mode=settings.oauth_mode,
            scopes=tuple(item.strip() for item in settings.oauth_scopes.split(",") if item.strip()),
            jwks_url=settings.oauth_jwks_url,
            metadata_url=f"{settings.public_base_url}{METADATA_PATH}",
        )

    def metadata(self, documentation: str | None = None) -> dict[str, Any]:
        """RFC 9728 protected resource metadata."""
        body: dict[str, Any] = {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
            "resource_name": "Football Results MCP server",
        }
        if self.scopes:
            body["scopes_supported"] = list(self.scopes)
        if documentation:
            body["resource_documentation"] = documentation
        return body

    def challenge(self, error: str | None = None, description: str | None = None) -> str:
        """A `WWW-Authenticate` value pointing clients at the metadata (RFC 6750, RFC 9728)."""
        parts = []
        if error:
            parts.append(f'error="{error}"')
            if description:
                parts.append(f'error_description="{_quoted(description)}"')
        parts.append(f'resource_metadata="{self.metadata_url}"')
        if self.scopes and not error:
            parts.append(f'scope="{" ".join(self.scopes)}"')
        return "Bearer " + ", ".join(parts)


def _quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', "'")


class InvalidToken(Exception):
    """The bearer token is not acceptable; the message is safe to show to the client."""


class IssuerUnavailable(Exception):
    """The issuer's metadata or JWKS cannot be fetched, so no token can be checked."""


def create_http_client() -> httpx.AsyncClient:
    """The HTTP client for discovery and JWKS requests (a test seam)."""
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)


class JwksCache:
    """The issuer's signing keys: fetched on first use, kept for an hour, and refetched once per
    cooldown when a token names an unknown `kid` (key rotation)."""

    def __init__(
        self,
        issuer: str,
        jwks_url: str | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = issuer
        self.jwks_url = jwks_url
        self.clock = clock
        self._keys: dict[str, SigningKey] = {}  # by kid; "" for a key without one
        self._fetched_at: float | None = None
        self._refetched_at: float | None = None
        self._lock = asyncio.Lock()

    async def key(self, kid: str | None) -> "SigningKey":
        async with self._lock:
            now = self.clock()
            if self._fetched_at is None or now - self._fetched_at > JWKS_MAX_AGE_SECONDS:
                await self._fetch()
            elif not self._has(kid) and (
                self._refetched_at is None
                or now - self._refetched_at >= JWKS_REFETCH_COOLDOWN_SECONDS
            ):
                self._refetched_at = now
                await self._fetch()
            return self._find(kid)

    def _has(self, kid: str | None) -> bool:
        return (kid in self._keys) if kid else len(self._keys) == 1

    def _find(self, kid: str | None) -> "SigningKey":
        if kid is None:
            if len(self._keys) == 1:
                return next(iter(self._keys.values()))
            raise InvalidToken("The token has no key ID (kid)")
        if kid not in self._keys:
            raise InvalidToken("The token is signed with an unknown key")
        return self._keys[kid]

    async def _fetch(self) -> None:
        try:
            async with create_http_client() as http:
                url = self.jwks_url or await self._discover(http)
                response = await http.get(url, headers={"Accept": "application/json"})
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise IssuerUnavailable(f"cannot fetch the issuer's JWKS: {error}") from error
        raw_keys = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(raw_keys, list):
            raise IssuerUnavailable("the issuer's JWKS has no keys list")
        keys: dict[str, SigningKey] = {}
        for raw in raw_keys:
            # Symmetric (`oct`) and encryption keys can never verify an access token here.
            if not isinstance(raw, dict) or raw.get("kty") == "oct" or raw.get("use") == "enc":
                continue
            try:
                key = PyJWK.from_dict(raw)
            except jwt.PyJWTError:
                continue
            keys[str(raw.get("kid") or "")] = SigningKey(key, raw.get("alg"))
        self._keys = keys
        self._fetched_at = self.clock()

    async def _discover(self, http: httpx.AsyncClient) -> str:
        """`jwks_uri` from OpenID Connect discovery, else RFC 8414 metadata."""
        issuer = self.issuer.rstrip("/")
        scheme, _, rest = issuer.partition("://")
        host, _, path = rest.partition("/")
        candidates = (
            f"{issuer}/.well-known/openid-configuration",
            f"{scheme}://{host}/.well-known/oauth-authorization-server"
            + (f"/{path}" if path else ""),
        )
        for url in candidates:
            response = await http.get(url, headers={"Accept": "application/json"})
            if response.status_code != 200:
                continue
            metadata = response.json()
            if metadata.get("issuer") != self.issuer:
                raise ValueError(f"{url} names issuer {metadata.get('issuer')!r}")
            if isinstance(metadata.get("jwks_uri"), str):
                return metadata["jwks_uri"]
        raise ValueError("no authorization server metadata with a jwks_uri")


@dataclass(frozen=True)
class SigningKey:
    jwk: PyJWK
    # The `alg` the JWKS declares for this key, if any; a token must then use exactly it.
    algorithm: str | None


class JwtVerifier(TokenVerifier):
    """Validates JWT access tokens for this resource (signature, `iss`, `aud`, `exp`, `nbf`)."""

    def __init__(self, config: OAuthConfig, jwks: JwksCache | None = None) -> None:
        self.config = config
        self.jwks = jwks or JwksCache(config.issuer, config.jwks_url)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            return await self.check(token)
        except InvalidToken:
            return None

    async def check(self, token: str) -> AccessToken:
        """The validated token; raises `InvalidToken` or `IssuerUnavailable`."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as error:
            raise InvalidToken("The token is not a JWT") from error
        algorithm = header.get("alg")
        if algorithm not in ASYMMETRIC_ALGORITHMS:
            raise InvalidToken("The token algorithm is not allowed")
        key = await self.jwks.key(header.get("kid"))
        if key.algorithm is not None and key.algorithm != algorithm:
            raise InvalidToken("The token algorithm does not match its key")
        try:
            claims = jwt.decode(
                token,
                key.jwk.key,
                algorithms=[algorithm],
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as error:
            raise InvalidToken("The token has expired") from error
        except jwt.ImmatureSignatureError as error:
            raise InvalidToken("The token is not valid yet") from error
        except jwt.InvalidAudienceError as error:
            raise InvalidToken("The token was not issued for this server (audience)") from error
        except jwt.InvalidIssuerError as error:
            raise InvalidToken("The token was issued by another issuer") from error
        except jwt.MissingRequiredClaimError as error:
            raise InvalidToken(f"The token has no {error.claim} claim") from error
        except jwt.PyJWTError as error:
            raise InvalidToken("The token signature or claims are invalid") from error
        scope = claims.get("scope")
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            scopes=scope.split() if isinstance(scope, str) else [],
            expires_at=int(claims["exp"]),
            resource=self.config.audience,
            subject=str(claims["sub"]),
            claims={"iss": claims["iss"]},
        )


def bearer_token(authorization: str | None) -> str | None:
    """The token of an `Authorization: Bearer` header, `""` for a malformed one, else None."""
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return credentials.strip()
