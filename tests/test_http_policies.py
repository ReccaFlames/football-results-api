from datetime import UTC, datetime, timedelta
from ipaddress import ip_network

import pytest
from pydantic import ValidationError

from app.caching import cache_control, etag_for, if_none_match, max_age_seconds
from app.config import Settings
from app.ratelimit import FixedWindowRateLimiter, RateLimitPolicy, client_ip

POLICY = RateLimitPolicy("read", 2, 60)


def test_fixed_window_counts_per_client_and_resets() -> None:
    limiter = FixedWindowRateLimiter()
    assert limiter.hit(POLICY, "a", 0).remaining == 1
    assert limiter.hit(POLICY, "b", 10).remaining == 1
    second = limiter.hit(POLICY, "a", 20.5)
    assert (second.allowed, second.remaining, second.reset_seconds) == (True, 0, 40)
    denied = limiter.hit(POLICY, "a", 59.2)
    assert (denied.allowed, denied.remaining, denied.reset_seconds) == (False, 0, 1)
    assert limiter.hit(POLICY, "a", 60).remaining == 1


def test_limiter_keeps_at_most_max_keys() -> None:
    limiter = FixedWindowRateLimiter(max_keys=2)
    for client in ["a", "b", "c"]:
        limiter.hit(POLICY, client, 0)
    assert len(limiter._windows) == 2
    assert limiter.hit(POLICY, "a", 1).remaining == 1


def test_client_ip_trusts_forwarded_for_only_from_trusted_proxies() -> None:
    proxies = (ip_network("10.0.0.0/8"),)
    chain = ["198.51.100.1, 203.0.113.7", "10.0.0.5"]
    assert client_ip("203.0.113.9", chain, proxies) == "203.0.113.9"
    assert client_ip("10.0.0.2", chain, ()) == "10.0.0.2"
    assert client_ip("10.0.0.2", chain, proxies) == "203.0.113.7"
    assert client_ip("10.0.0.2", ["not-an-ip"], proxies) == "10.0.0.2"
    assert client_ip("10.0.0.2", [], proxies) == "10.0.0.2"
    assert client_ip(None, chain, proxies) == "unknown"


def test_trusted_proxies_setting_is_validated() -> None:
    assert Settings(trusted_proxies="10.0.0.0/8, ::1").trusted_proxies
    with pytest.raises(ValidationError):
        Settings(trusted_proxies="10.0.0.0/33")


def test_if_none_match_uses_weak_comparison() -> None:
    etag = etag_for(b"{}")
    assert etag.startswith('W/"') and etag != etag_for(b"[]")
    assert if_none_match(etag, etag)
    assert if_none_match(f'"x", {etag[2:]}', etag)
    assert if_none_match("*", etag)
    assert not if_none_match(None, etag)
    assert not if_none_match('W/"x"', etag)


def test_max_age_is_capped_by_the_next_update() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    assert max_age_seconds([None], now, 60) == 60
    assert max_age_seconds([now + timedelta(seconds=30), None], now, 60) == 30
    assert max_age_seconds([now + timedelta(hours=1)], now, 60) == 60
    assert max_age_seconds([now - timedelta(seconds=1)], now, 60) == 0
    assert cache_control(30) == "public, max-age=30"
    assert cache_control(0) == "public, no-cache"
