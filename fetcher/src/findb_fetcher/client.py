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

import httpx

from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry, ContractValidationError

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})


class SourceAPIResponseError(RuntimeError):
    """The Source API returned a response that the client cannot accept."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Source API returned HTTP {response.status_code}: {response.text[:500]}")


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    """An immutable request snapshot reused byte-for-byte across attempts."""

    body: bytes
    idempotency_key: str


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
        schema_id = snapshot.get("schema_id")
        schema_version = snapshot.get("schema_version")
        idempotency_key = snapshot.get("idempotency_key")
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
        return PreparedDelivery(body=body, idempotency_key=idempotency_key)

    def deliver(self, delivery: PreparedDelivery) -> dict[str, Any]:
        url = f"{self._config.source_api_url.rstrip('/')}/api/v1/source/ingest"
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self._config.source_client_key,
            "Idempotency-Key": delivery.idempotency_key,
        }

        for attempt in range(1, self._config.max_attempts + 1):
            try:
                response = self._client.post(url, content=delivery.body, headers=headers)
            except httpx.TransportError:
                if attempt == self._config.max_attempts:
                    raise
                self._sleep(self._backoff_seconds(attempt))
                continue

            if 200 <= response.status_code < 300:
                try:
                    payload = response.json()
                except json.JSONDecodeError as exc:
                    raise SourceAPIResponseError(response) from exc
                if not isinstance(payload, dict):
                    raise SourceAPIResponseError(response)
                return payload

            if (
                response.status_code not in _RETRYABLE_STATUS_CODES
                or attempt == self._config.max_attempts
            ):
                raise SourceAPIResponseError(response)
            self._sleep(self._retry_delay(response, attempt))

        raise AssertionError("retry loop ended unexpectedly")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SourceAPIClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

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
