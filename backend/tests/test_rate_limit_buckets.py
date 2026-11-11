"""In-process rate-limit state must stay bounded (#485)."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from app.api import deps
from app.api.deps import SlidingWindowLimiter


def test_one_off_keys_are_reclaimed_after_their_window():
    limiter = SlidingWindowLimiter(lambda: 1_000_000)
    for index in range(1000):
        assert limiter.hit(f"203.0.113.9:user-{index}", limit=5, window=300, now=0)
    assert len(limiter) == 1000

    # Reproduces the issue: one more account after every window expired used to
    # leave 1,001 entries. The periodic sweep now drops every expired identity.
    calls = 1000
    while calls % SlidingWindowLimiter.SWEEP_EVERY:
        limiter.hit(f"198.51.100.1:late-{calls}", limit=5, window=300, now=301)
        calls += 1
    assert len(limiter) == calls - 1000
    assert all(not key.startswith("203.0.113.9") for key in limiter._buckets)


def test_tracked_identities_never_exceed_capacity():
    limiter = SlidingWindowLimiter(lambda: 50)
    for index in range(10_000):
        limiter.hit(f"user-{index}", limit=5, window=300, now=float(index))
        assert len(limiter) <= 50
    # The least recently used identities were evicted first.
    assert set(limiter._buckets) == {f"user-{index}" for index in range(9_950, 10_000)}


def test_sliding_window_and_rejections_are_not_recorded():
    limiter = SlidingWindowLimiter(lambda: 10)
    assert [limiter.hit("k", limit=2, window=10, now=t) for t in (0, 1, 2, 3)] == [
        True,
        True,
        False,
        False,
    ]
    # Only the two accepted attempts count, so one slot frees at t=10.
    assert limiter.hit("k", limit=2, window=10, now=10) is True
    assert limiter.hit("k", limit=2, window=10, now=10.5) is False
    assert limiter.hit("k", limit=2, window=10, now=11) is True


def test_concurrent_hits_never_exceed_the_limit():
    limiter = SlidingWindowLimiter(lambda: 10)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: limiter.hit("k", limit=50, window=60), range(400)))
    assert results.count(True) == 50


def test_source_and_serve_limits_use_bounded_limiters(monkeypatch):
    monkeypatch.setattr(deps.settings, "RATE_LIMIT_MAX_TRACKED_KEYS", 3)
    for index in range(10):
        deps._enforce_source_rate_limit("client", f"10.0.0.{index}", limit=1, window_seconds=60)
        key = SimpleNamespace(key_id=f"key-{index}", rate_limit_requests=1, rate_limit_window=60)
        deps._enforce_serve_rate_limit(key, "10.0.0.1")
    assert len(deps._source_rate_limiter) == len(deps._serve_rate_limiter) == 3

    with pytest.raises(HTTPException) as source_limited:
        deps._enforce_source_rate_limit("client", "10.0.0.9", limit=1, window_seconds=60)
    assert (source_limited.value.status_code, source_limited.value.headers) == (
        429,
        {"Retry-After": "60"},
    )
    with pytest.raises(HTTPException) as serve_limited:
        deps._enforce_serve_rate_limit(key, "10.0.0.1")
    assert serve_limited.value.status_code == 429


async def _login(client: AsyncClient, ip: str, username: str) -> int:
    response = await client.post(
        "/api/v1/admin/auth/login",
        headers={"X-Forwarded-For": ip},
        json={"username": username, "password": "wrong password"},
    )
    return response.status_code


@pytest.mark.asyncio
async def test_rotating_usernames_hits_the_per_ip_login_total(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(deps.settings, "SOURCE_TRUST_PROXY_HEADERS", True)
    total = deps.settings.ADMIN_LOGIN_IP_RATE_LIMIT_REQUESTS

    statuses = [await _login(client, "203.0.113.50", f"guess-{i}") for i in range(total + 5)]
    assert statuses == [401] * total + [429] * 5
    # Rejected attempts create no per-username bucket, and other clients are unaffected.
    assert len(deps._admin_login_rate_limiter) == total
    assert await _login(client, "198.51.100.60", "guess-0") == 401
