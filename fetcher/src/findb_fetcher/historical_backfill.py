"""Credential-safe consumer for the provider-neutral historical control API.

This module has no backend imports and no database access.  A provider runtime
receives one leased date, delivers its normal versioned contract/raw payload,
then reports only safe outcome metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol
from uuid import UUID

import httpx

from findb_fetcher.config import FetcherConfig


class HistoricalControlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HistoricalWorkItem:
    item_id: UUID
    request_id: UUID
    request_key: str
    provider: str
    dataset_key: str
    market: str
    trade_date: date
    lease_token: UUID


class HistoricalProviderRunner(Protocol):
    def run(self, item: HistoricalWorkItem) -> UUID: ...


class HistoricalControlClient:
    def __init__(self, config: FetcherConfig, *, client: httpx.Client | None = None) -> None:
        self._config, self._client = (
            config,
            client or httpx.Client(timeout=config.request_timeout_seconds),
        )
        self._owns_client = client is None

    def __enter__(self) -> "HistoricalControlClient":
        return self

    def __exit__(self, *_: object) -> None:
        if self._owns_client:
            self._client.close()

    def claim(self) -> HistoricalWorkItem | None:
        payload = self._request("POST", "/api/v1/source/historical-backfills/claim")
        if payload.get("success") is not True:
            raise HistoricalControlError("invalid historical control response")
        if payload.get("item_id") is None:
            return None
        try:
            return HistoricalWorkItem(
                item_id=UUID(str(payload["item_id"])),
                request_id=UUID(str(payload["request_id"])),
                request_key=_text(payload, "request_key"),
                provider=_text(payload, "provider"),
                dataset_key=_text(payload, "dataset_key"),
                market=_text(payload, "market"),
                trade_date=date.fromisoformat(_text(payload, "trade_date")),
                lease_token=UUID(_text(payload, "lease_token")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HistoricalControlError("invalid historical work identity") from exc

    def complete(self, item: HistoricalWorkItem, run_id: UUID) -> None:
        self._request(
            "POST",
            f"/api/v1/source/historical-backfills/{item.item_id}/complete",
            {
                "status": "completed",
                "run_id": str(run_id),
                "lease_token": str(item.lease_token),
            },
        )

    def fail(self, item: HistoricalWorkItem, code: str) -> None:
        # Never transmit provider diagnostics: the control plane needs only a safe code.
        self._request(
            "POST",
            f"/api/v1/source/historical-backfills/{item.item_id}/complete",
            {
                "status": "failed",
                "lease_token": str(item.lease_token),
                "failure_code": code[:50],
                "failure_message": "provider historical acquisition failed",
            },
        )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            response = self._client.request(
                method,
                f"{self._config.source_api_url}{path}",
                json=body,
                headers={"X-API-Key": self._config.source_client_key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise HistoricalControlError("historical control transport failed") from exc
        if response.status_code >= 400:
            raise HistoricalControlError(
                f"historical control rejected work ({response.status_code})"
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise HistoricalControlError("historical control response was invalid") from exc
        if not isinstance(value, dict):
            raise HistoricalControlError("historical control response was invalid")
        return value


def run_once(control: HistoricalControlClient, runner: HistoricalProviderRunner) -> bool:
    """Run one serial date; a failed date ends its request server-side."""
    item = control.claim()
    if item is None:
        return False
    try:
        run_id = runner.run(item)
        if not isinstance(run_id, UUID):
            raise HistoricalControlError("historical runner did not return a Source run")
    except Exception:  # Provider diagnostics must not escape into FinDB.
        control.fail(item, "PROVIDER_ACQUISITION_FAILED")
        return True
    control.complete(item, run_id)
    return True


def _text(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise HistoricalControlError("invalid historical work identity")
    return value
