"""HTTP caching rules for read endpoints: weak body-hash `ETag`s and `Cache-Control`."""

import hashlib
import math
from collections.abc import Iterable
from datetime import datetime

CACHEABLE_OPERATIONS = frozenset(
    {"list_competitions", "list_matches", "get_standings", "list_teams", "list_results"}
)
# Agent documentation routes (not in the OpenAPI spec), matched by route path template.
CACHEABLE_PAGES = frozenset(
    {
        "/llms.txt",
        "/llms-full.txt",
        "/agents",
        "/agents.md",
        "/problems",
        "/problems/{slug}",
        "/skills/football-results/SKILL.md",
        "/.well-known/oauth-protected-resource/mcp",
    }
)


def etag_for(body: bytes) -> str:
    return f'W/"{hashlib.sha256(body).hexdigest()[:32]}"'


def _opaque(tag: str) -> str:
    tag = tag.strip()
    return tag.removeprefix("W/")


def if_none_match(header: str | None, etag: str) -> bool:
    """Weak comparison (RFC 9110 §13.1.2): true when the client's copy is current."""
    if header is None:
        return False
    if header.strip() == "*":
        return True
    return any(_opaque(tag) == _opaque(etag) for tag in header.split(","))


def max_age_seconds(next_updates: Iterable[datetime | None], now: datetime, cap: int) -> int:
    """Seconds until the earliest scheduled update, between 0 and ``cap``."""
    upcoming = [moment for moment in next_updates if moment is not None]
    if not upcoming:
        return cap
    remaining = math.floor((min(upcoming) - now).total_seconds())
    return max(0, min(cap, remaining))


def cache_control(max_age: int) -> str:
    return f"public, max-age={max_age}" if max_age > 0 else "public, no-cache"


def private_cache_control(value: str) -> str:
    """The same rule for the caller's own cache only (private instance, ADR 0008)."""
    return value.replace("public", "private", 1)
