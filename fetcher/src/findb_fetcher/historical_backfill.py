"""Credential-safe consumer for the provider-neutral historical control API.

This module has no backend imports and no database access.  A provider runtime
receives one leased date, delivers its normal versioned contract/raw payload,
then reports only safe outcome metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

import httpx

from findb_fetcher.client import SourceAPIResponseError, permanent_source_failure
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.provider_work import ProviderWorkAcquisitionStopped


class HistoricalControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float = 0,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable


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
    lease_expires_at: datetime | None = None
    universe_snapshot: dict[str, Any] | None = None


class HistoricalWorkDeferredError(RuntimeError):
    """Retain the lease/checkpoint for safe reclaim without failing its request."""


class HistoricalPreparedManualError(RuntimeError):
    """Permanent prepared delivery rejection; retain bytes and explicit manual proof."""


@dataclass(frozen=True, slots=True)
class HistoricalRunResult:
    run_id: UUID | None
    result_evidence: dict[str, Any]
    status: str = "completed"


class HistoricalProviderRunner(Protocol):
    def run(self, item: HistoricalWorkItem) -> UUID | HistoricalRunResult: ...


class HistoricalControlClient:
    def __init__(
        self,
        config: FetcherConfig,
        *,
        client: httpx.Client | None = None,
        provider: str | None = None,
    ) -> None:
        self.provider = provider
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
                lease_expires_at=datetime.fromisoformat(payload["lease_expires_at"])
                if payload.get("lease_expires_at")
                else None,
                universe_snapshot=payload.get("universe_snapshot"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HistoricalControlError("invalid historical work identity") from exc

    def complete(self, item: HistoricalWorkItem, run_id: UUID | HistoricalRunResult) -> None:
        result = run_id if isinstance(run_id, HistoricalRunResult) else None
        body: dict[str, Any] = {
            "status": result.status if result else "completed",
            "run_id": str(result.run_id)
            if result and result.run_id
            else None
            if result
            else str(run_id),
            "lease_token": str(item.lease_token),
        }
        if result:
            body["result_evidence"] = result.result_evidence
        self._request(
            "POST",
            f"/api/v1/source/historical-backfills/{item.item_id}/complete",
            body,
        )

    def authorize(self, item: HistoricalWorkItem):
        config = (
            replace(self._config, max_attempts=1) if item.provider == "taifex" else self._config
        )
        with FullMarketSourceClient(config, client=self._client) as source:
            try:
                return source.authorization(
                    "POST",
                    f"historical-backfills/{item.item_id}/authorization",
                    {"lease_token": str(item.lease_token)},
                    expected={
                        "item_id": str(item.item_id),
                        "request_id": str(item.request_id),
                        "provider": item.provider,
                        "dataset_key": item.dataset_key,
                    },
                )
            except FullMarketProtocolError as exc:
                if item.provider != "taifex" or not exc.retryable:
                    raise
                raise HistoricalControlError(
                    "historical authorization temporarily unavailable",
                    status_code=exc.status_code,
                    retry_after=exc.retry_after,
                    retryable=True,
                ) from None

    def fail(self, item: HistoricalWorkItem, code: str) -> None:
        # Never transmit provider diagnostics: the control plane needs only a safe code.
        import json

        from findb_fetcher.provider_work import active

        owner = active()
        error = (
            owner.journal.latest_source_error()
            if owner and code == "PREPARED_DELIVERY_MANUAL_REVIEW"
            else None
        )
        self._request(
            "POST",
            f"/api/v1/source/historical-backfills/{item.item_id}/complete",
            {
                "status": "failed",
                "lease_token": str(item.lease_token),
                "failure_code": code[:50],
                "failure_message": json.dumps(error, sort_keys=True)
                if error
                else "provider historical acquisition failed",
            },
        )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        from findb_fetcher.provider_work import active

        owner = active()
        headers = {"X-API-Key": self._config.source_client_key, "Accept": "application/json"}
        if owner:
            headers.update(
                {
                    "X-FinDB-Work-Id": owner.work["work_id"],
                    "X-FinDB-Work-Generation": str(owner.work["generation"]),
                    "X-FinDB-Work-Token": owner.work["token"],
                }
            )
        while True:
            try:
                response = self._client.request(
                    method,
                    f"{self._config.source_api_url}{path}",
                    json=body,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise HistoricalControlError("historical control transport failed") from exc
            if owner and response.status_code == 409:
                try:
                    error = response.json()
                    if isinstance(error, dict) and error.get("detail") == {
                        "code": "PROVIDER_WORK_PREPARED_DRAINING"
                    }:
                        owner.drain_prepared()
                        continue
                    draining = isinstance(error, dict) and error.get("detail") == {
                        "code": "PROVIDER_WORK_DRAINING"
                    }
                except ValueError:
                    draining = False
                if draining:
                    from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

                    if owner.failed.is_set():
                        raise ProviderWorkRecoveryBlockedError(
                            "provider work heartbeat is unavailable"
                        )
                    import time

                    time.sleep(2)
                    continue
            break
        if response.status_code >= 400:
            raise HistoricalControlError(
                f"historical control rejected work ({response.status_code})",
                status_code=response.status_code,
                retry_after=FullMarketSourceClient(self._config, client=self._client)._retry_delay(
                    response, 0, bounded=False
                ),
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise HistoricalControlError("historical control response was invalid") from exc
        if not isinstance(value, dict):
            raise HistoricalControlError("historical control response was invalid")
        if owner and method == "POST" and path.endswith("/complete"):
            # Source commits historical proof, settlement and item status in
            # one transaction before acknowledging this completion.
            owner.settled = True
        return value


def run_once(control: HistoricalControlClient, runner: HistoricalProviderRunner) -> bool:
    from findb_fetcher.provider_work import ProviderWorkWaitingError, claimed_work, enabled

    enabled()
    if not control.provider:
        raise HistoricalControlError("historical provider identity is required")
    try:
        with (
            FullMarketSourceClient(control._config) as source,
            claimed_work(source, control.provider, accepted_kinds=["historical", "rerun"]) as owner,
        ):
            if owner is None:
                return False
            if owner.work["kind"] == "rerun":
                owner.request("execute-rerun")
                owner.settle("completed")
                return True
            worked = _run_once(control, runner)
            if not worked:
                owner.settle("cancelled", proof={"reason": "historical_request_stopped"})
            return worked
    except ProviderWorkWaitingError:
        return False


def _run_once(control: HistoricalControlClient, runner: HistoricalProviderRunner) -> bool:
    """Run one serial date; a failed date ends its request server-side."""
    from findb_fetcher.provider_work import require_owner

    if control.provider is None:
        raise HistoricalControlError("historical provider identity is required")
    owner = require_owner(control.provider)
    item = control.claim()
    if item is None:
        return False
    try:
        if owner.work.get("scope_stopped"):
            receipts = owner.drain_prepared()
            if receipts:
                control.complete(item, receipts[-1].run_id)
            else:
                owner.settle("cancelled", proof={"reason": "historical_request_stopped"})
            return True
        if owner.work.get("drain_only") and owner.work["state"] == "open":
            owner.drain_prepared()
            owner.settle("retry", failure_code="PROVIDER_ACCOUNT_COOLDOWN", delay=60)
            return True
        if owner.work["state"] in {"closed", "draining"}:
            receipts = owner.drain_prepared()
            if item.provider not in {"taifex", "factset"} or not owner.work.get("manifest"):
                if receipts:
                    try:
                        control.complete(item, receipts[-1].run_id)
                        return True
                    except HistoricalControlError as exc:
                        if exc.status_code not in {409, 422}:
                            raise
                owner.settle("retry", failure_code="RECOVERED_CLOSED_ROUND", delay=60)
                return True
        from findb_fetcher.acquisition import authorization_scope

        with authorization_scope(
            lambda: control.authorize(item),
            dispatch_attempts=1 if getattr(item, "provider", None) == "taifex" else 3,
        ):
            run_id = runner.run(item)
        if not isinstance(run_id, (UUID, HistoricalRunResult)) or (
            isinstance(run_id, HistoricalRunResult) and item.provider not in {"taifex", "factset"}
        ):
            raise HistoricalControlError("historical runner did not return a Source run")
    except ProviderWorkAcquisitionStopped:
        try:
            control.fail(item, "ACQUISITION_SUSPENDED")
        except SourceAPIResponseError as exc:
            if permanent_source_failure(exc):
                control.fail(item, "PREPARED_DELIVERY_MANUAL_REVIEW")
                return True
            if exc.status_code != 429 and exc.status_code < 500:
                raise
            owner.settle("retry", failure_code="ACQUISITION_SUSPENDED", delay=60)
        return True
    except HistoricalWorkDeferredError:
        owner.settle("retry", failure_code="DEFERRED", delay=60)
        return True
    except HistoricalPreparedManualError:
        control.fail(item, "PREPARED_DELIVERY_MANUAL_REVIEW")
        return True
    except SourceAPIResponseError as exc:
        if permanent_source_failure(exc):
            control.fail(item, "PREPARED_DELIVERY_MANUAL_REVIEW")
            return True
        if exc.status_code == 429 or exc.status_code >= 500:
            owner.settle(
                "retry",
                failure_code="SOURCE_TEMPORARILY_UNAVAILABLE",
                delay=max(60, int(exc.retry_after)),
            )
            return True
        raise
    except Exception:  # Provider diagnostics must not escape into FinDB.
        control.fail(item, "PROVIDER_ACQUISITION_FAILED")
        return True
    try:
        finish = getattr(runner, "complete", None)
        if isinstance(run_id, HistoricalRunResult) and callable(finish):
            finish(control, item, run_id)
        else:
            control.complete(item, run_id)
    except HistoricalWorkDeferredError:
        return True
    except ValueError:
        control.fail(item, "COMPLETION_RETRY_EXHAUSTED")
    except HistoricalControlError as exc:
        if exc.status_code == 422:
            control.fail(item, "COMPLETION_EVIDENCE_INVALID")
        else:
            return True
    return True


def _text(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise HistoricalControlError("invalid historical work identity")
    return value
