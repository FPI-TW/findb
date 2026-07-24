from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from findb_fetcher.client import SourceAPIClient, SourceAPIResponseError
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry


def _config(**overrides: object) -> FetcherConfig:
    values: dict[str, object] = {
        "source_api_url": "https://source.example.test",
        "source_client_key": "fetcher-source-key",
        "max_attempts": 3,
        "max_retry_after_seconds": 5.0,
    }
    values.update(overrides)
    return FetcherConfig(**values)  # type: ignore[arg-type]


def test_retry_reuses_identical_body_and_idempotency_key(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    attempts: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append((request.content, request.headers["Idempotency-Key"]))
        if len(attempts) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(202, json={"status": "queued"}, request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )
    prepared = client.prepare(market_request)

    assert client.deliver(prepared) == {"status": "queued"}
    assert attempts == [
        (prepared.body, prepared.idempotency_key),
        (prepared.body, prepared.idempotency_key),
    ]


def test_transport_failure_is_retried(contracts_dir: Path, market_request: dict[str, Any]) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary connection failure", request=request)
        return httpx.Response(202, json={"status": "queued"}, request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    assert client.deliver(client.prepare(market_request)) == {"status": "queued"}
    assert attempts == 2


def test_conflict_is_not_retried(contracts_dir: Path, market_request: dict[str, Any]) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(409, json={"error": "conflict"}, request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(SourceAPIResponseError) as error:
        client.deliver(client.prepare(market_request))

    assert error.value.response.status_code == 409
    assert attempts == 1


def test_retry_after_is_honored_and_bounded(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "60"}, request=request)
        return httpx.Response(202, json={"status": "queued"}, request=request)

    client = SourceAPIClient(
        _config(max_retry_after_seconds=5.0),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )

    assert client.deliver(client.prepare(market_request)) == {"status": "queued"}
    assert attempts == 2
    assert sleeps == [5.0]


@pytest.mark.parametrize("retry_after", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_retry_after_uses_bounded_backoff(
    contracts_dir: Path,
    market_request: dict[str, Any],
    retry_after: str,
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": retry_after}, request=request)
        return httpx.Response(202, json={"status": "queued"}, request=request)

    client = SourceAPIClient(
        _config(max_retry_after_seconds=5.0),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )

    assert client.deliver(client.prepare(market_request)) == {"status": "queued"}
    assert attempts == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize("status_code", [429, 502, 503, 504])
def test_only_declared_transient_http_statuses_are_retried(
    contracts_dir: Path,
    market_request: dict[str, Any],
    status_code: int,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(status_code, request=request)
        return httpx.Response(202, json={"status": "queued"}, request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    assert client.deliver(client.prepare(market_request)) == {"status": "queued"}
    assert attempts == 2


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422, 500])
def test_ordinary_client_errors_and_http_500_are_not_retried(
    contracts_dir: Path,
    market_request: dict[str, Any],
    status_code: int,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, json={"error": "rejected"}, request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(SourceAPIResponseError) as error:
        client.deliver(client.prepare(market_request))

    assert error.value.response.status_code == status_code
    assert attempts == 1


def test_http_date_retry_after_uses_injected_clock(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    now = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)
    retry_at = format_datetime(now + timedelta(seconds=12), usegmt=True)
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": retry_at}, request=request)
        return httpx.Response(202, json={"status": "queued"}, request=request)

    client = SourceAPIClient(
        _config(max_retry_after_seconds=30.0),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        now=lambda: now,
    )

    assert client.deliver(client.prepare(market_request)) == {"status": "queued"}
    assert attempts == 2
    assert sleeps == [12.0]
