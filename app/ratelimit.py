"""In-process fixed-window rate limiting with IETF `RateLimit` header fields.

Header syntax follows draft-ietf-httpapi-ratelimit-headers-11:
`RateLimit-Policy: "read";q=120;w=60` and `RateLimit: "read";r=87;t=42`.
Counters live in this process only; several API workers would each enforce the full quota.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/metrics",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
        "/robots.txt",
        "/.well-known/oauth-protected-resource/mcp",
    }
)


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    quota: int
    window_seconds: int

    def header(self) -> str:
        return f'"{self.name}";q={self.quota};w={self.window_seconds}'


@dataclass(frozen=True)
class RateLimitDecision:
    policy: RateLimitPolicy
    allowed: bool
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        return {
            "RateLimit-Policy": self.policy.header(),
            "RateLimit": f'"{self.policy.name}";r={self.remaining};t={self.reset_seconds}',
        }


class FixedWindowRateLimiter:
    """Counts requests per (policy, client) in windows that start at the client's first request.

    At most ``max_keys`` windows are kept: expired windows are pruned first, then the oldest.
    """

    def __init__(self, max_keys: int = 10_000) -> None:
        self.max_keys = max_keys
        self._windows: dict[tuple[str, str], tuple[float, int]] = {}  # (expires, count)

    def hit(self, policy: RateLimitPolicy, client: str, now: float) -> RateLimitDecision:
        key = (policy.name, client)
        expires, count = self._windows.pop(key, (0.0, 0))
        if now >= expires:
            expires, count = now + policy.window_seconds, 0
        allowed = count < policy.quota
        if allowed:
            count += 1
        self._windows[key] = (expires, count)
        self._prune(now)
        reset = max(1, math.ceil(expires - now))
        return RateLimitDecision(policy, allowed, policy.quota - count, reset)

    def _prune(self, now: float) -> None:
        if len(self._windows) <= self.max_keys:
            return
        for key, (expires, _) in list(self._windows.items()):
            if now >= expires:
                del self._windows[key]
        while len(self._windows) > self.max_keys:
            del self._windows[next(iter(self._windows))]


def client_ip(
    peer: str | None,
    forwarded_for: Sequence[str],
    trusted_proxies: Sequence[IPv4Network | IPv6Network],
) -> str:
    """The client address; ``X-Forwarded-For`` is used only when the peer is a trusted proxy.

    The client is then the rightmost forwarded address that is not itself a trusted proxy.
    """
    if peer is None:
        return "unknown"
    if not trusted_proxies or not is_trusted_proxy(peer, trusted_proxies):
        return peer
    hops = [hop.strip() for header in forwarded_for for hop in header.split(",") if hop.strip()]
    for hop in reversed(hops):
        if _parse(hop) is None:
            return peer
        if not is_trusted_proxy(hop, trusted_proxies):
            return hop
    return hops[0] if hops else peer


def _parse(value: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(value)
    except ValueError:
        return None


def is_trusted_proxy(value: str, trusted_proxies: Sequence[IPv4Network | IPv6Network]) -> bool:
    address = _parse(value)
    return address is not None and any(address in network for network in trusted_proxies)


@dataclass(frozen=True)
class Client:
    """A rate-limited caller: its counter bucket, metrics label, and per-minute quotas."""

    bucket: str
    label: str
    read_per_minute: int
    write_per_minute: int

    def policy(self, method: str) -> RateLimitPolicy:
        if method in READ_METHODS:
            return RateLimitPolicy("read", self.read_per_minute, 60)
        return RateLimitPolicy("write", self.write_per_minute, 60)
