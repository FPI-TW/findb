"""HTTP delivery to FinDB Source API with bounded, identity-safe retries."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any
from uuid import UUID

import httpx

from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry, ContractValidationError

_MAX_RESPONSE_BYTES = 64 * 1024
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_KNOWN_STATUSES = frozenset(
    {
        "queued",
        "pending",
        "processing",
        "retrying",
        "completed",
        "completed_with_errors",
        "failed",
    }
)
_TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})
_ALLOWED_ERROR_CODES = frozenset(
    {
        "CURRENCY_REQUIRED",
        "DATABASE_UNAVAILABLE",
        "DATASET_ACCESS_DENIED",
        "DATASET_CONTRACT_NOT_CONFIGURED",
        "DATASET_INACTIVE",
        "DATASET_NOT_FOUND",
        "IDEMPOTENCY_PAYLOAD_MISMATCH",
        "INGRESS_SCHEMA_INVALID",
        "INGRESS_SCHEMA_NOT_ALLOWED",
        "INGRESS_SCHEMA_UNSUPPORTED",
        "INTERNAL_ERROR",
        "SOURCE_IDENTITY_MISMATCH",
    }
)
_ALLOWED_RUN_FAILURE_CODES = frozenset(
    {
        "DATASET_CONTRACT_INVALID",
        "DATASET_INACTIVE",
        "DATASET_NOT_FOUND",
        "NORMALIZER_NOT_CONFIGURED",
        "RAW_PAYLOAD_MISSING",
        "RETRY_EXHAUSTED",
    }
)


class SourceAPIResponseError(RuntimeError):
    """The Source API rejected a request with a safe, bounded error."""

    def __init__(self, status_code: int, code: str | None = None) -> None:
        self.status_code = status_code
        self.code = code
        detail = f"Source API returned HTTP {status_code}"
        if code is not None:
            detail = f"{detail} ({code})"
        super().__init__(detail)


class SourceAPIProtocolError(RuntimeError):
    """The Source API returned a response that violated its public contract."""


class SourceAPIDeadlineExceeded(TimeoutError):  # noqa: N818 - public CLI contract
    """The caller's end-to-end Source API deadline elapsed."""


class SourceAPITransportError(ConnectionError):
    """Source API transport retries were exhausted."""


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    """An immutable request snapshot reused byte-for-byte across attempts."""

    body: bytes
    idempotency_key: str
    dataset_key: str
    schema_id: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Validated acknowledgement for one accepted Source delivery."""

    attempt_id: UUID
    run_id: UUID
    status: str
    schema_id: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class RunStatus:
    """Validated, bounded normalization status for a delivered run."""

    run_id: UUID
    dataset_key: str
    schema_id: str
    schema_version: int
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    total_records: int
    success_records: int
    failed_records: int
    failure_code: str | None
    attempt_count: int
    max_attempts: int
    next_retry_at: datetime | None


class SourceAPIClient:
    def __init__(
        self,
        config: FetcherConfig,
        contracts: ContractRegistry,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._config = config
        self._contracts = contracts
        self._client = client or httpx.Client(timeout=config.request_timeout_seconds)
        self._owns_client = client is None
        self._sleep = sleep
        self._now = now

    def prepare(self, request: Mapping[str, Any]) -> PreparedDelivery:
        snapshot = dict(request)
        dataset_key = snapshot.get("dataset_key")
        schema_id = snapshot.get("schema_id")
        schema_version = snapshot.get("schema_version")
        idempotency_key = snapshot.get("idempotency_key")
        if not isinstance(dataset_key, str) or not dataset_key:
            raise ContractValidationError("dataset_key must be a non-empty string")
        if not isinstance(schema_id, str):
            raise ContractValidationError("schema_id must be a string")
        if type(schema_version) is not int:
            raise ContractValidationError("schema_version must be an integer")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ContractValidationError("idempotency_key must be a non-empty string")

        self._contracts.validate(schema_id, schema_version, snapshot)
        body = json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return PreparedDelivery(
            body=body,
            idempotency_key=idempotency_key,
            dataset_key=dataset_key,
            schema_id=schema_id,
            schema_version=schema_version,
        )

    def deliver(
        self,
        delivery: PreparedDelivery,
        *,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> DeliveryReceipt:
        """Deliver one immutable request and validate the acknowledgement identity."""
        payload = self._request_json(
            "POST",
            "/api/v1/source/ingest",
            delivery=delivery,
            deadline=deadline,
            monotonic=monotonic,
        )
        if payload.get("success") is not True:
            raise SourceAPIProtocolError("Source API acknowledgement was invalid")
        attempt_id = _required_uuid(payload, "attempt_id")
        run_id = _required_uuid(payload, "run_id")
        status = _required_status(payload)
        schema_id = _required_string(payload, "schema_id")
        schema_version = _required_integer(payload, "schema_version")
        if schema_id != delivery.schema_id or schema_version != delivery.schema_version:
            raise SourceAPIProtocolError("Source API acknowledgement identity mismatch")
        receipt = DeliveryReceipt(
            attempt_id=attempt_id,
            run_id=run_id,
            status=status,
            schema_id=schema_id,
            schema_version=schema_version,
        )
        _check_deadline(deadline, monotonic)
        return receipt

    def get_run_status(
        self,
        run_id: UUID,
        *,
        expected: PreparedDelivery,
        deadline: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> RunStatus:
        """Return status only when the run and immutable delivery identity match."""
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        payload = self._request_json(
            "GET",
            f"/api/v1/source/runs/{run_id}",
            deadline=deadline,
            monotonic=monotonic,
        )
        response_run_id = _required_uuid(payload, "run_id")
        dataset_key = _required_string(payload, "dataset_key")
        schema_id = _required_string(payload, "schema_id")
        schema_version = _required_integer(payload, "schema_version")
        status = _required_status(payload)
        if (
            response_run_id != run_id
            or dataset_key != expected.dataset_key
            or schema_id != expected.schema_id
            or schema_version != expected.schema_version
        ):
            raise SourceAPIProtocolError("Source API run identity mismatch")

        total_records = _non_negative_integer(payload, "total_records")
        success_records = _non_negative_integer(payload, "success_records")
        failed_records = _non_negative_integer(payload, "failed_records")
        if success_records + failed_records > total_records:
            raise SourceAPIProtocolError("Source API run record counts were invalid")

        attempt_count = _non_negative_integer(payload, "attempt_count")
        max_attempts = _non_negative_integer(payload, "max_attempts")
        if max_attempts < 1 or attempt_count > max_attempts:
            raise SourceAPIProtocolError("Source API run attempt counts were invalid")

        started_at = _optional_datetime(payload, "started_at")
        completed_at = _optional_datetime(payload, "completed_at")
        next_retry_at = _optional_datetime(payload, "next_retry_at")
        if status in _TERMINAL_STATUSES and completed_at is None:
            raise SourceAPIProtocolError("Terminal Source API run omitted completed_at")

        failure_code = payload.get("failure_code")
        if failure_code is not None and failure_code not in _ALLOWED_RUN_FAILURE_CODES:
            raise SourceAPIProtocolError("Source API run failure code was invalid")

        run_status = RunStatus(
            run_id=response_run_id,
            dataset_key=dataset_key,
            schema_id=schema_id,
            schema_version=schema_version,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            total_records=total_records,
            success_records=success_records,
            failed_records=failed_records,
            failure_code=failure_code,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            next_retry_at=next_retry_at,
        )
        _check_deadline(deadline, monotonic)
        return run_status

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SourceAPIClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        delivery: PreparedDelivery | None = None,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> dict[str, Any]:
        url = f"{self._config.source_api_url.rstrip('/')}{path}"
        headers = {
            "Accept-Encoding": "identity",
            "X-API-Key": self._config.source_client_key,
        }
        content: bytes | None = None
        if delivery is not None:
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Idempotency-Key": delivery.idempotency_key,
                }
            )
            content = delivery.body

        for attempt in range(1, self._config.max_attempts + 1):
            timeout = self._request_timeout(deadline, monotonic)
            request = self._client.build_request(
                method,
                url,
                content=content,
                headers=headers,
                timeout=timeout,
            )
            try:
                response = self._client.send(request, stream=True)
            except httpx.DecodingError:
                raise SourceAPIProtocolError("Source API response encoding was invalid") from None
            except httpx.TransportError:
                if attempt == self._config.max_attempts:
                    raise SourceAPITransportError(
                        "Source API transport retries exhausted"
                    ) from None
                self._sleep_with_deadline(
                    self._backoff_seconds(attempt), deadline=deadline, monotonic=monotonic
                )
                continue

            try:
                retry_delay = self._retry_delay(response, attempt)
                body = self._read_bounded(
                    response,
                    deadline=deadline,
                    monotonic=monotonic,
                )
            except httpx.TransportError:
                if attempt == self._config.max_attempts:
                    raise SourceAPITransportError(
                        "Source API transport retries exhausted"
                    ) from None
                self._sleep_with_deadline(
                    self._backoff_seconds(attempt),
                    deadline=deadline,
                    monotonic=monotonic,
                )
                continue
            finally:
                response.close()

            if 200 <= response.status_code < 300:
                payload = _decode_object(body)
                _check_deadline(deadline, monotonic)
                return payload

            if (
                response.status_code not in _RETRYABLE_STATUS_CODES
                or attempt == self._config.max_attempts
            ):
                raise SourceAPIResponseError(
                    response.status_code,
                    _safe_error_code(body),
                )
            self._sleep_with_deadline(retry_delay, deadline=deadline, monotonic=monotonic)

        raise AssertionError("retry loop ended unexpectedly")

    def _read_bounded(
        self,
        response: httpx.Response,
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> bytes:
        _check_deadline(deadline, monotonic)
        content_encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise SourceAPIProtocolError("Source API response encoding was not allowed")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise SourceAPIProtocolError("Source API Content-Length was invalid") from exc
            if declared_length < 0 or declared_length > _MAX_RESPONSE_BYTES:
                raise SourceAPIProtocolError("Source API response exceeded size limit")

        body = bytearray()
        try:
            # Injected transports may return an already-buffered response. The
            # encoding gate above guarantees those bytes have identity semantics;
            # live network responses remain unconsumed and always use iter_raw().
            chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
            for chunk in chunks:
                _check_deadline(deadline, monotonic)
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise SourceAPIProtocolError("Source API response exceeded size limit")
                _check_deadline(deadline, monotonic)
        except httpx.StreamError as exc:
            raise SourceAPIProtocolError("Source API response stream failed") from exc
        _check_deadline(deadline, monotonic)
        return bytes(body)

    def _request_timeout(
        self,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> float:
        if deadline is None:
            return self._config.request_timeout_seconds
        remaining = _remaining(deadline, monotonic)
        return min(self._config.request_timeout_seconds, remaining)

    def _sleep_with_deadline(
        self,
        delay: float,
        *,
        deadline: float | None,
        monotonic: Callable[[], float],
    ) -> None:
        if deadline is None:
            self._sleep(delay)
            return
        remaining = _remaining(deadline, monotonic)
        self._sleep(min(delay, remaining))
        _check_deadline(deadline, monotonic)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return self._backoff_seconds(attempt)
        parsed = self._parse_retry_after(retry_after)
        if parsed is None:
            return self._backoff_seconds(attempt)
        return min(parsed, self._config.max_retry_after_seconds)

    def _parse_retry_after(self, value: str) -> float | None:
        try:
            seconds = float(value)
        except ValueError:
            pass
        else:
            if isfinite(seconds):
                return max(0.0, seconds)
            return None
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - self._now()).total_seconds())

    def _backoff_seconds(self, attempt: int) -> float:
        return min(float(2 ** (attempt - 1)), self._config.max_retry_after_seconds)


def _decode_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceAPIProtocolError("Source API response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SourceAPIProtocolError("Source API response was not a JSON object")
    return payload


def _safe_error_code(body: bytes) -> str | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) and code in _ALLOWED_ERROR_CODES else None


def _required_uuid(payload: Mapping[str, Any], field: str) -> UUID:
    value = payload.get(field)
    if not isinstance(value, str):
        raise SourceAPIProtocolError(f"Source API {field} was invalid")
    try:
        return UUID(value)
    except ValueError as exc:
        raise SourceAPIProtocolError(f"Source API {field} was invalid") from exc


def _required_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise SourceAPIProtocolError(f"Source API {field} was invalid")
    return value


def _required_integer(payload: Mapping[str, Any], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int:
        raise SourceAPIProtocolError(f"Source API {field} was invalid")
    return value


def _non_negative_integer(payload: Mapping[str, Any], field: str) -> int:
    value = _required_integer(payload, field)
    if value < 0:
        raise SourceAPIProtocolError(f"Source API {field} was invalid")
    return value


def _required_status(payload: Mapping[str, Any]) -> str:
    status = payload.get("status")
    if not isinstance(status, str) or status not in _KNOWN_STATUSES:
        raise SourceAPIProtocolError("Source API status was invalid")
    return status


def _optional_datetime(payload: Mapping[str, Any], field: str) -> datetime | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceAPIProtocolError(f"Source API {field} was invalid")
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SourceAPIProtocolError(f"Source API {field} was invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SourceAPIProtocolError(f"Source API {field} must include a timezone")
    return parsed


def _remaining(deadline: float, monotonic: Callable[[], float]) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise SourceAPIDeadlineExceeded("Source API deadline exceeded")
    return remaining


def _check_deadline(
    deadline: float | None,
    monotonic: Callable[[], float],
) -> None:
    if deadline is not None:
        _remaining(deadline, monotonic)
