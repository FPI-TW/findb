from __future__ import annotations

from uuid import UUID

import httpx

from findb_fetcher.config import FetcherConfig
from findb_fetcher.historical_backfill import HistoricalControlClient, run_once


def _config() -> FetcherConfig:
    return FetcherConfig(source_api_url="https://source.example.test", source_client_key="safe-key")


def test_claim_and_complete_use_provider_neutral_control_contract() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/claim"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "item_id": "01982886-b452-7c71-b166-cb74cfd1d000",
                    "request_id": "01982886-b452-7c71-b166-cb74cfd1d001",
                    "request_key": "request-001",
                    "provider": "shioaji",
                    "dataset_key": "tw_equity_minute",
                    "market": "TW",
                    "trade_date": "2026-08-31",
                    "lease_token": "01982886-b452-7c71-b166-cb74cfd1d003",
                },
            )
        return httpx.Response(200, json={"success": True})

    class Runner:
        def run(self, _item: object) -> UUID:
            return UUID("01982886-b452-7c71-b166-cb74cfd1d002")

    control = HistoricalControlClient(
        _config(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert run_once(control, Runner()) is True
    assert [call.url.path for call in calls] == [
        "/api/v1/source/historical-backfills/claim",
        "/api/v1/source/historical-backfills/01982886-b452-7c71-b166-cb74cfd1d000/complete",
    ]
    assert b"safe-key" not in calls[1].content
    assert b"lease_token" in calls[1].content


def test_failed_provider_reports_only_safe_failure_code() -> None:
    responses = iter(
        [
            {
                "success": True,
                "item_id": "01982886-b452-7c71-b166-cb74cfd1d000",
                "request_id": "01982886-b452-7c71-b166-cb74cfd1d001",
                "request_key": "request-001",
                "provider": "shioaji",
                "dataset_key": "tw_equity_minute",
                "market": "TW",
                "trade_date": "2026-08-31",
                "lease_token": "01982886-b452-7c71-b166-cb74cfd1d003",
            },
            {"success": True},
        ]
    )
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json=next(responses))

    class Runner:
        def run(self, _item: object) -> UUID:
            raise RuntimeError("credential=must-not-leak")

    control = HistoricalControlClient(
        _config(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert run_once(control, Runner()) is True
    assert b"credential" not in bodies[-1]
    assert b"PROVIDER_ACQUISITION_FAILED" in bodies[-1]
    assert b"lease_token" in bodies[-1]


def test_runner_without_source_run_fails_instead_of_completing() -> None:
    responses = iter(
        [
            {
                "success": True,
                "item_id": "01982886-b452-7c71-b166-cb74cfd1d000",
                "request_id": "01982886-b452-7c71-b166-cb74cfd1d001",
                "request_key": "request-001",
                "provider": "shioaji",
                "dataset_key": "tw_equity_minute",
                "market": "TW",
                "trade_date": "2026-08-31",
                "lease_token": "01982886-b452-7c71-b166-cb74cfd1d003",
            },
            {"success": True},
        ]
    )
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json=next(responses))

    class Runner:
        def run(self, _item: object) -> UUID | None:
            return None

    control = HistoricalControlClient(
        _config(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert run_once(control, Runner()) is True  # type: ignore[arg-type]
    assert b'"status":"failed"' in bodies[-1]
