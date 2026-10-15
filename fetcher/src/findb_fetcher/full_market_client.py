"""Bounded Source governance protocol, independent of backend imports."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Any
from urllib.parse import urlencode

import httpx

from findb_fetcher.account_governor import source_permit
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_universe import canonical_bytes


class FullMarketProtocolError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FullMarketSourceClient:
    def __init__(
        self,
        config: FetcherConfig,
        *,
        client: httpx.Client | None = None,
        execution_profile: str = "full_market",
    ) -> None:
        if execution_profile not in {"pilot", "full_market"}:
            raise ValueError("Unsupported execution profile")
        self.execution_profile = execution_profile
        self.config = config
        self.client = client or httpx.Client(timeout=config.request_timeout_seconds)
        self.owns_client = client is None

    def __enter__(self) -> "FullMarketSourceClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def authorization(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expected: dict[str, Any] | None = None,
        required_dataset: str | None = None,
    ) -> Any:
        result = self.request(method, path, body, _authorization=True)
        if any(result.get(key) != value for key, value in (expected or {}).items()):
            raise FullMarketProtocolError("acquisition authorization identity mismatch")
        if required_dataset is not None and required_dataset not in result.get(
            "authorized_dataset_keys", result.get("admitted_dataset_keys", [])
        ):
            raise FullMarketProtocolError("acquisition dataset is not authorized")
        return result["_authorization"]

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        _authorization: bool = False,
    ) -> dict[str, Any]:
        encoded = canonical_bytes(body) if body is not None else None
        for attempt in range(self.config.max_attempts):
            source_permit(self.config.source_client_key)
            from findb_fetcher.http_boundary import extensions, prepare_transport

            url = self.config.source_api_url + "/api/v1/source/" + path
            origin_samples: list[tuple[float, float]] = []
            if _authorization:
                prepare_transport(self.client, url)

            def received(_status: int, _headers: httpx.Headers) -> None:
                if not origin_samples:
                    origin_samples.append((time.monotonic(), time.time()))

            sent_monotonic = time.monotonic()
            try:
                with self.client.stream(
                    method,
                    url,
                    extensions=extensions(headers=received) if _authorization else None,
                    content=encoded,
                    headers={
                        "X-API-Key": self.config.source_client_key,
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if _authorization and not origin_samples:
                        raise FullMarketProtocolError(
                            "Source transport lacks header origin evidence"
                        )
                    origin, receipt_utc = (
                        origin_samples[0] if origin_samples else (time.monotonic(), time.time())
                    )
                    if response.headers.get("Content-Encoding", "identity") != "identity":
                        raise FullMarketProtocolError("Source governance encoding invalid")
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 16 * 1024 * 1024:
                            raise FullMarketProtocolError(
                                "Source governance response exceeded bound"
                            )
                    if (
                        response.status_code in {429, 502, 503, 504}
                        and attempt + 1 < self.config.max_attempts
                    ):
                        time.sleep(self._retry_delay(response, attempt))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise FullMarketProtocolError(
                            f"Source governance returned HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise FullMarketProtocolError("Source governance response shape invalid")
                    if _authorization:
                        from findb_fetcher.acquisition import REMOTE_AGE_SECONDS, Authorization

                        try:
                            header_at = datetime.fromisoformat(
                                response.headers["X-FinDB-Acquisition-Checked-At"]
                            )
                            body_at = datetime.fromisoformat(result["checked_at"])
                            if header_at.tzinfo is None or body_at != header_at:
                                raise ValueError
                            checked_at = header_at.timestamp()
                            expires = result.get("lease_expires_at")
                            expires_at = (
                                datetime.fromisoformat(expires).timestamp() if expires else None
                            )
                            provider = result["provider"]
                            if (
                                result.get("acquisition_allowed") is not True
                                or result.get("max_age_seconds") != 5
                                or not -0.250 <= receipt_utc - checked_at < REMOTE_AGE_SECONDS
                                or origin - sent_monotonic >= REMOTE_AGE_SECONDS
                                or provider not in {"twelve_data", "finlab", "shioaji", "taifex"}
                            ):
                                raise ValueError
                        except (KeyError, TypeError, ValueError):
                            raise FullMarketProtocolError(
                                "current acquisition authorization denied or stale"
                            ) from None
                        result["_authorization"] = Authorization(
                            origin, receipt_utc, checked_at, expires_at, provider
                        )
                    return result
            except httpx.TransportError:
                if attempt + 1 == self.config.max_attempts:
                    raise FullMarketProtocolError("Source governance transport failed") from None
        raise FullMarketProtocolError("Source governance retries exhausted")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        fallback = min(float(2**attempt), self.config.max_retry_after_seconds)
        value = response.headers.get("Retry-After")
        if value is None:
            return fallback
        try:
            seconds = float(value)
        except ValueError:
            seconds = -1.0
        if isfinite(seconds) and seconds >= 0:
            return min(seconds, self.config.max_retry_after_seconds)
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return fallback
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return min(
            max(0.0, retry_at.timestamp() - time.time()),
            self.config.max_retry_after_seconds,
        )

    def universes(self, dataset: str, provider: str, as_of: str | None = None) -> dict[str, Any]:
        return self.request(
            "GET",
            "universes?"
            + urlencode(
                {
                    "dataset_key": dataset,
                    "execution_profile": self.execution_profile,
                    "provider": provider,
                    **({"as_of": as_of} if as_of else {}),
                }
            ),
        )

    def release(self, release_id: str) -> dict[str, Any]:
        return self.request("GET", f"universes/{release_id}")

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "universes", body)

    def plan(
        self, *, dataset: str, provider: str, trade_date: str, release_id: str
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "delivery-plans",
            {
                "version": 1,
                "dataset_key": dataset,
                "execution_profile": self.execution_profile,
                "provider": provider,
                "trade_date": trade_date,
                "release_id": release_id,
            },
        )

    def plans_for_date(self, *, dataset: str, trade_date: str) -> dict[str, Any]:
        return self.request(
            "GET",
            "delivery-plans?"
            + urlencode(
                {
                    "dataset_key": dataset,
                    "execution_profile": self.execution_profile,
                    "start_date": trade_date,
                    "end_date": trade_date,
                    "limit": 1,
                }
            ),
        )

    def evaluate(self, plan_id: str) -> dict[str, Any]:
        return self.request("GET", f"delivery-plans/{plan_id}")

    def outcome(
        self,
        plan_id: str,
        *,
        work_item_id: str,
        member_key: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "version": 1,
            "work_item_id": work_item_id,
            "member_key": member_key,
            "outcome": "no_data" if reason in {"no_trade", "halted"} else "blocked",
            "reason": reason,
        }
        if evidence is not None:
            body["evidence"] = evidence
        return self.request("POST", f"delivery-plans/{plan_id}/outcomes", body)
