"""Actual Source throttling stays bounded without blocking frozen-plan drain."""

from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest

from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient


def test_source_429_recovers_frozen_plan_without_acquisition_authority(monkeypatch):
    monkeypatch.setenv("FULL_MARKET_ENABLED", "false")
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")
    clock = [0.0]
    calls = []
    monkeypatch.setattr("findb_fetcher.full_market_client.time.time", lambda: clock[0])
    monkeypatch.setattr(
        "findb_fetcher.full_market_client.time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    def respond(request):
        assert request.url.path == "/api/v1/source/delivery-plans/frozen"
        assert request.headers["X-API-Key"] == "secret"
        calls.append(clock[0])
        if clock[0] < 60:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"plan_id": "frozen", "status": "complete"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = FullMarketSourceClient(
            FetcherConfig("https://source.example", "secret", max_retry_after_seconds=30),
            client=http,
        )
        assert client.evaluate("frozen")["status"] == "complete"
    assert calls == [0, 30, 60]


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        ("60", 5),
        (format_datetime(datetime.fromtimestamp(1060, timezone.utc), usegmt=True), 5),
        (format_datetime(datetime.fromtimestamp(999, timezone.utc), usegmt=True), 0),
        ("0", 0),
        (None, 1),
        ("malformed", 1),
        ("-10", 1),
        ("NaN", 1),
        ("Infinity", 1),
        ("-Infinity", 1),
    ],
)
def test_source_retry_after_parsing_and_bounds(monkeypatch, retry_after, expected_delay):
    sleeps = []
    calls = []
    monkeypatch.setattr("findb_fetcher.full_market_client.time.time", lambda: 1000)
    monkeypatch.setattr("findb_fetcher.full_market_client.time.sleep", sleeps.append)

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            headers = {} if retry_after is None else {"Retry-After": retry_after}
            return httpx.Response(429, headers=headers)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = FullMarketSourceClient(
            FetcherConfig("https://source.example", "secret", max_retry_after_seconds=5),
            client=http,
        )
        assert client.request("GET", "delivery-plans") == {"ok": True}
    assert sleeps == [expected_delay]
    assert len(calls) == 2


@pytest.mark.parametrize(("max_attempts", "sleeps_expected"), [(1, []), (4, [1, 2, 2])])
def test_source_retry_exhaustion_preserves_attempt_bound(
    monkeypatch, max_attempts, sleeps_expected
):
    sleeps = []
    calls = []
    monkeypatch.setattr("findb_fetcher.full_market_client.time.sleep", sleeps.append)

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "invalid"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client = FullMarketSourceClient(
            FetcherConfig(
                "https://source.example",
                "secret",
                max_attempts=max_attempts,
                max_retry_after_seconds=2,
            ),
            client=http,
        )
        with pytest.raises(FullMarketProtocolError) as error:
            client.request("GET", "delivery-plans")
    assert error.value.status_code == 429
    assert len(calls) == max_attempts
    assert sleeps == sleeps_expected
