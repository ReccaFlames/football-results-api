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

    Every hit first drops the windows that have ended, so a client address stays in memory only
    while its window lasts (`/privacy` says so). At most ``max_keys`` windows are kept; beyond
    that the oldest are dropped.
    """

    def __init__(self, max_keys: int = 10_000) -> None:
        self.max_keys = max_keys
        # Grouped by window length; each group is in window-start order, so ended windows lead.
        self._windows: dict[int, dict[tuple[str, str], tuple[float, int]]] = {}

    def __len__(self) -> int:
        return sum(len(group) for group in self._windows.values())

    def hit(self, policy: RateLimitPolicy, client: str, now: float) -> RateLimitDecision:
        self._expire(now)
        group = self._windows.setdefault(policy.window_seconds, {})
        key = (policy.name, client)
        expires, count = group.get(key, (now + policy.window_seconds, 0))
        allowed = count < policy.quota
        if allowed:
            count += 1
        group[key] = (expires, count)  # an existing key keeps its place
        self._cap()
        reset = max(1, math.ceil(expires - now))
        return RateLimitDecision(policy, allowed, policy.quota - count, reset)

    def _expire(self, now: float) -> None:
        for group in self._windows.values():
            while group:
                key, (expires, _) = next(iter(group.items()))
                if now < expires:
                    break
                del group[key]

    def _cap(self) -> None:
        while len(self) > self.max_keys:
            starts = {
                length: next(iter(group.values()))[0] - length
                for length, group in self._windows.items()
                if group
            }
            oldest = self._windows[min(starts, key=starts.__getitem__)]
            del oldest[next(iter(oldest))]


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


# The window of the per-minute read and write quotas.
QUOTA_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Client:
    """A rate-limited caller: its counter bucket, metrics label, and per-minute quotas."""

    bucket: str
    label: str
    read_per_minute: int
    write_per_minute: int

    def policy(self, method: str) -> RateLimitPolicy:
        if method in READ_METHODS:
            return RateLimitPolicy("read", self.read_per_minute, QUOTA_WINDOW_SECONDS)
        return RateLimitPolicy("write", self.write_per_minute, QUOTA_WINDOW_SECONDS)


# Valid `create_access_request` calls per client IP (ADR 0010), counted by the handler so that
# invalid requests do not use it up.
ACCESS_REQUEST_POLICY = RateLimitPolicy("access-request", 3, 3600)
