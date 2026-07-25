from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from findb_fetcher.client import (
    DeliveryReceipt,
    SourceAPIClient,
    SourceAPIDeadlineExceeded,
    SourceAPIProtocolError,
    SourceAPIResponseError,
    SourceAPITransportError,
)
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


ATTEMPT_ID = "01982886-b452-7c71-b166-cb74cfd1d00e"
RUN_ID = "01982886-b452-7c71-b166-cb74cfd1d00f"


def _receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": True,
        "attempt_id": ATTEMPT_ID,
        "run_id": RUN_ID,
        "status": "queued",
        "schema_id": "market_eod",
        "schema_version": 1,
        "message": "accepted",
    }
    payload.update(overrides)
    return payload


def _run_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": RUN_ID,
        "dataset_key": "tw_market_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "status": "processing",
        "started_at": "2026-07-24T00:00:00Z",
        "completed_at": None,
        "total_records": 1,
        "success_records": 0,
        "failed_records": 0,
        "error_message": None,
        "failure_code": None,
        "attempt_count": 1,
        "max_attempts": 5,
        "next_retry_at": None,
    }
    payload.update(overrides)
    return payload


def test_retry_reuses_identical_body_and_idempotency_key(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    attempts: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append((request.content, request.headers["Idempotency-Key"]))
        if len(attempts) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )
    prepared = client.prepare(market_request)

    assert client.deliver(prepared) == DeliveryReceipt(
        attempt_id=UUID(ATTEMPT_ID),
        run_id=UUID(RUN_ID),
        status="queued",
        schema_id="market_eod",
        schema_version=1,
    )
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
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    assert client.deliver(client.prepare(market_request)).status == "queued"
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

    assert error.value.status_code == 409
    assert error.value.code is None
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
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(max_retry_after_seconds=5.0),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )

    assert client.deliver(client.prepare(market_request)).status == "queued"
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
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(max_retry_after_seconds=5.0),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )

    assert client.deliver(client.prepare(market_request)).status == "queued"
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
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    assert client.deliver(client.prepare(market_request)).status == "queued"
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

    assert error.value.status_code == status_code
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
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(max_retry_after_seconds=30.0),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        now=lambda: now,
    )

    assert client.deliver(client.prepare(market_request)).status == "queued"
    assert attempts == 2
    assert sleeps == [12.0]


def test_run_status_is_typed_and_identity_checked(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/source/runs/{RUN_ID}"
        assert request.headers["X-API-Key"] == "fetcher-source-key"
        return httpx.Response(200, json=_run_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    status = client.get_run_status(
        UUID(RUN_ID),
        expected=client.prepare(market_request),
    )

    assert status.run_id == UUID(RUN_ID)
    assert status.dataset_key == "tw_market_eod"
    assert status.started_at == datetime(2026, 7, 24, tzinfo=timezone.utc)
    assert status.total_records == 1


def test_post_and_get_request_identity_encoding(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    encodings: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        encodings.append(request.headers["Accept-Encoding"])
        if request.method == "POST":
            return httpx.Response(202, json=_receipt_payload(), request=request)
        return httpx.Response(200, json=_run_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    prepared = client.prepare(market_request)
    receipt = client.deliver(prepared)
    client.get_run_status(receipt.run_id, expected=prepared)

    assert encodings == ["identity", "identity"]


def test_run_status_retries_only_declared_transient_status(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(502, request=request)
        return httpx.Response(200, json=_run_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )
    status = client.get_run_status(
        UUID(RUN_ID),
        expected=client.prepare(market_request),
    )
    assert status.status == "processing"
    assert attempts == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("success", False),
        ("success", 1),
        ("attempt_id", "not-a-uuid"),
        ("run_id", "not-a-uuid"),
        ("status", "mystery"),
        ("schema_id", "other"),
        ("schema_version", 2),
    ],
)
def test_invalid_delivery_receipt_is_rejected(
    contracts_dir: Path,
    market_request: dict[str, Any],
    field: str,
    value: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json=_receipt_payload(**{field: value}), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SourceAPIProtocolError):
        client.deliver(client.prepare(market_request))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", ATTEMPT_ID),
        ("dataset_key", "other"),
        ("schema_id", "other"),
        ("schema_version", 2),
        ("status", "mystery"),
        ("total_records", -1),
        ("success_records", True),
        ("failed_records", -1),
        ("attempt_count", -1),
        ("max_attempts", 0),
        ("started_at", "2026-07-24T00:00:00"),
        ("completed_at", "not-a-timestamp"),
        ("next_retry_at", 123),
        ("failure_code", "not stable"),
    ],
)
def test_invalid_run_status_is_rejected(
    contracts_dir: Path,
    market_request: dict[str, Any],
    field: str,
    value: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_run_payload(**{field: value}), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SourceAPIProtocolError):
        client.get_run_status(
            UUID(RUN_ID),
            expected=client.prepare(market_request),
        )


def test_inconsistent_record_counts_are_rejected(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_run_payload(total_records=1, success_records=1, failed_records=1),
            request=request,
        )

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIProtocolError):
        client.get_run_status(UUID(RUN_ID), expected=client.prepare(market_request))


@pytest.mark.parametrize("status", ["completed", "completed_with_errors", "failed"])
def test_terminal_status_requires_completed_at(
    contracts_dir: Path,
    market_request: dict[str, Any],
    status: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_run_payload(status=status, completed_at=None),
            request=request,
        )

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIProtocolError):
        client.get_run_status(UUID(RUN_ID), expected=client.prepare(market_request))


@pytest.mark.parametrize("payload", [b"not-json", b"[]"])
def test_malformed_or_non_object_response_is_rejected(
    contracts_dir: Path,
    market_request: dict[str, Any],
    payload: bytes,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=payload, request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIProtocolError):
        client.deliver(client.prepare(market_request))


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": str(64 * 1024 + 1)},
        {"Content-Length": "invalid"},
    ],
)
def test_oversized_or_invalid_content_length_is_rejected(
    contracts_dir: Path,
    market_request: dict[str, Any],
    headers: dict[str, str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=b"{}", headers=headers, request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIProtocolError):
        client.deliver(client.prepare(market_request))


def test_streamed_response_size_is_bounded(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b"x" * (64 * 1024)
            yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, stream=OversizedStream(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIProtocolError):
        client.deliver(client.prepare(market_request))


def test_corrupt_gzip_response_is_rejected_without_decoding(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            content=b"not-a-gzip-stream",
            headers={"Content-Encoding": "gzip"},
            request=request,
        )

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SourceAPIProtocolError) as error:
        client.deliver(client.prepare(market_request))
    assert str(error.value) == "Source API response encoding was invalid"


def test_high_compression_response_is_rejected_before_stream_read(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    compressed = gzip.compress(b"x" * (1024 * 1024))
    stream_read = False

    class CompressedStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal stream_read
            stream_read = True
            yield compressed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            202,
            stream=CompressedStream(),
            headers={"Content-Encoding": "gzip"},
            request=request,
        )

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SourceAPIProtocolError) as error:
        client.deliver(client.prepare(market_request))
    assert str(error.value) == "Source API response encoding was not allowed"
    assert stream_read is False


def test_midstream_transport_failure_retries_identical_delivery(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    attempts: list[tuple[bytes, str]] = []

    class FailingStream(httpx.SyncByteStream):
        def __init__(self, request: httpx.Request) -> None:
            self._request = request

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b'{"success":'
            raise httpx.ReadError("connection lost", request=self._request)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append((request.content, request.headers["Idempotency-Key"]))
        if len(attempts) == 1:
            return httpx.Response(202, stream=FailingStream(request), request=request)
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )
    prepared = client.prepare(market_request)

    assert client.deliver(prepared).run_id == UUID(RUN_ID)
    assert attempts == [
        (prepared.body, prepared.idempotency_key),
        (prepared.body, prepared.idempotency_key),
    ]


def test_midstream_transport_failure_retries_get(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    attempts = 0

    class FailingStream(httpx.SyncByteStream):
        def __init__(self, request: httpx.Request) -> None:
            self._request = request

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b'{"run_id":'
            raise httpx.ReadTimeout("read timed out", request=self._request)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, stream=FailingStream(request), request=request)
        return httpx.Response(200, json=_run_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    status = client.get_run_status(
        UUID(RUN_ID),
        expected=client.prepare(market_request),
    )
    assert status.status == "processing"
    assert attempts == 2


def test_midstream_transport_failure_exhaustion_is_safe(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    attempts = 0

    class FailingStream(httpx.SyncByteStream):
        def __init__(self, request: httpx.Request) -> None:
            self._request = request

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b"{"
            raise httpx.ReadError(
                "credential-like-secret",
                request=self._request,
            )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(202, stream=FailingStream(request), request=request)

    client = SourceAPIClient(
        _config(max_attempts=3),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(SourceAPITransportError) as error:
        client.deliver(client.prepare(market_request))
    assert str(error.value) == "Source API transport retries exhausted"
    assert "credential-like-secret" not in str(error.value)
    assert attempts == 3


def test_stream_deadline_is_absolute_and_prevents_success(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    current = 10.0
    attempts = 0

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            nonlocal current
            current = 10.5
            yield json.dumps(_receipt_payload()).encode()
            current = 11.1
            yield b" "

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(202, stream=SlowStream(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SourceAPIDeadlineExceeded):
        client.deliver(
            client.prepare(market_request),
            deadline=11.0,
            monotonic=lambda: current,
        )
    assert attempts == 1


@pytest.mark.parametrize("status_code", [401, 403, 409])
def test_rejection_exposes_only_allowlisted_code_and_not_body(
    contracts_dir: Path,
    market_request: dict[str, Any],
    status_code: int,
) -> None:
    secret = "backend-secret-detail"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "success": False,
                "error": {
                    "code": "IDEMPOTENCY_PAYLOAD_MISMATCH",
                    "message": secret,
                },
            },
            request=request,
        )

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIResponseError) as error:
        client.deliver(client.prepare(market_request))

    assert error.value.status_code == status_code
    assert error.value.code == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert secret not in str(error.value)
    assert not hasattr(error.value, "response")


def test_unrecognized_error_code_is_not_exposed(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "BACKEND_SECRET_CODE", "message": "secret"}},
            request=request,
        )

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIResponseError) as error:
        client.deliver(client.prepare(market_request))
    assert error.value.code is None
    assert str(error.value) == "Source API returned HTTP 400"


def test_credential_like_run_failure_code_is_not_exposed(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    credential_like_code = "SOURCE_CLIENT_KEY_PRODUCTION"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_run_payload(failure_code=credential_like_code),
            request=request,
        )

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIProtocolError) as error:
        client.get_run_status(
            UUID(RUN_ID),
            expected=client.prepare(market_request),
        )
    assert credential_like_code not in str(error.value)


def test_deadline_caps_timeout_and_prevents_another_request(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    current = 10.0
    requests: list[httpx.Request] = []
    sleeps: list[float] = []

    def monotonic() -> float:
        return current

    def sleep(seconds: float) -> None:
        nonlocal current
        sleeps.append(seconds)
        current += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, request=request)

    client = SourceAPIClient(
        _config(request_timeout_seconds=30.0),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )

    with pytest.raises(SourceAPIDeadlineExceeded):
        client.deliver(
            client.prepare(market_request),
            deadline=10.5,
            monotonic=monotonic,
        )

    assert len(requests) == 1
    assert requests[0].extensions["timeout"]["read"] == 0.5
    assert sleeps == [0.5]


def test_expired_deadline_sends_no_request(
    contracts_dir: Path, market_request: dict[str, Any]
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(202, json=_receipt_payload(), request=request)

    client = SourceAPIClient(
        _config(),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SourceAPIDeadlineExceeded):
        client.deliver(
            client.prepare(market_request),
            deadline=10.0,
            monotonic=lambda: 10.0,
        )
    assert requests == 0
