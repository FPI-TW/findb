"""Bounded Source governance protocol, independent of backend imports."""

from __future__ import annotations

import json
import time
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
    def __init__(self, config: FetcherConfig, *, client: httpx.Client | None = None) -> None:
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

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = canonical_bytes(body) if body is not None else None
        for attempt in range(self.config.max_attempts):
            source_permit(self.config.source_client_key)
            try:
                with self.client.stream(
                    method,
                    self.config.source_api_url + "/api/v1/source/" + path,
                    content=encoded,
                    headers={
                        "X-API-Key": self.config.source_client_key,
                        "Content-Type": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
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
                        time.sleep(min(2**attempt, self.config.max_retry_after_seconds))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise FullMarketProtocolError(
                            f"Source governance returned HTTP {response.status_code}",
                            status_code=response.status_code,
                        )
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise FullMarketProtocolError("Source governance response shape invalid")
                    return result
            except httpx.TransportError:
                if attempt + 1 == self.config.max_attempts:
                    raise FullMarketProtocolError("Source governance transport failed") from None
        raise FullMarketProtocolError("Source governance retries exhausted")

    def universes(self, dataset: str, provider: str, as_of: str | None = None) -> dict[str, Any]:
        return self.request(
            "GET",
            "universes?"
            + urlencode(
                {
                    "dataset_key": dataset,
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
