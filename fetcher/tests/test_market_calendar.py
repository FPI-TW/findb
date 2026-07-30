from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest

from findb_fetcher.config import ConfigError, MarketCalendarConfig
from findb_fetcher.market_calendar import MarketCalendarError, PublishedCalendarClient


def _payload(
    *,
    market: str = "TW",
    year: int = 2026,
    revision: int = 3,
) -> dict[str, Any]:
    first = date(year, 1, 1)
    count = (date(year, 12, 31) - first).days + 1
    days = []
    for offset in range(count):
        current = first + timedelta(days=offset)
        status = "open" if current.weekday() < 5 else "closed"
        days.append(
            {
                "date": current.isoformat(),
                "day_status": status,
                "is_open": status == "open",
                "session_open": "09:00:00" if status == "open" else None,
                "session_close": "13:30:00" if status == "open" else None,
                "holiday_name": None,
                "description": None,
            }
        )
    return {
        "success": True,
        "data": {
            "market": market,
            "year": year,
            "revision": revision,
            "status": "published",
            "coverage_complete": True,
            "timezone": "Asia/Taipei",
            "expected_days": count,
            "actual_days": count,
            "days": days,
        },
    }


def _config() -> MarketCalendarConfig:
    return MarketCalendarConfig(
        mode="remote",
        serve_base_url="https://serve.example.test",
        api_key="calendar-key",
        request_timeout_seconds=2.0,
        cache_ttl_seconds=300.0,
    )


def test_remote_config_requires_dedicated_serve_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FETCHER_CALENDAR_MODE", "remote")
    monkeypatch.delenv("FINDB_SERVE_BASE_URL", raising=False)
    monkeypatch.delenv("FETCHER_CALENDAR_SERVE_API_KEY", raising=False)

    with pytest.raises(ConfigError, match="FINDB_SERVE_BASE_URL"):
        MarketCalendarConfig.from_env()

    monkeypatch.setenv("FINDB_SERVE_BASE_URL", "https://serve.example.test/")
    monkeypatch.setenv("FETCHER_CALENDAR_SERVE_API_KEY", "read-key")
    config = MarketCalendarConfig.from_env()
    assert config.serve_base_url == "https://serve.example.test"
    assert config.api_key == "read-key"


def test_client_authenticates_and_caches_complete_published_year() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_payload())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    calendar = PublishedCalendarClient(_config(), client=client, monotonic=lambda: 10.0)

    first = calendar.get_year("tw", 2026)
    second = calendar.get_year("TW", 2026)

    assert first is second
    assert first.revision == 3
    assert len(first.days) == 365
    assert calls[0].url.path == "/api/v1/serve/calendar/years/TW/2026"
    assert calls[0].headers["X-API-Key"] == "calendar-key"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["data"].update(status="draft"),
        lambda payload: payload["data"].update(coverage_complete=False),
        lambda payload: payload["data"].update(actual_days=364),
        lambda payload: payload["data"]["days"].pop(),
        lambda payload: payload["data"]["days"].__setitem__(1, payload["data"]["days"][0]),
        lambda payload: payload["data"]["days"][0].update(
            day_status="settlement_only", is_open=True
        ),
        lambda payload: payload["data"].update(market="US"),
        lambda payload: payload["data"].update(year=2025),
        lambda payload: payload["data"].update(revision=0),
    ],
)
def test_client_fails_closed_for_untrusted_calendar(mutate: Any) -> None:
    payload = _payload()
    mutate(payload)
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )

    with pytest.raises(MarketCalendarError):
        PublishedCalendarClient(_config(), client=client).get_year("TW", 2026)


def test_client_accepts_leap_year_and_settlement_only() -> None:
    payload = _payload(year=2028)
    payload["data"]["days"][42].update(
        day_status="settlement_only",
        is_open=False,
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )

    calendar = PublishedCalendarClient(_config(), client=client).get_year("TW", 2028)

    assert len(calendar.days) == 366
    assert calendar.days[42].day_status == "settlement_only"


def test_client_bounds_response_and_transport_failures() -> None:
    oversized = b"x" * (512 * 1024 + 1)
    large_client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=oversized))
    )
    with pytest.raises(MarketCalendarError, match="size limit"):
        PublishedCalendarClient(_config(), client=large_client).get_year("TW", 2026)

    failing_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("down", request=request))
        )
    )
    with pytest.raises(MarketCalendarError, match="request failed"):
        PublishedCalendarClient(_config(), client=failing_client).get_year("TW", 2026)
