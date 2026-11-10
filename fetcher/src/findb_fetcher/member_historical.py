"""Historical dates of a frozen Full release cohort, acquired member by member.

Twelve Data and FinLab historical items whose request froze each date's Full
release use the same provider adapters as Full daily: the same provider
symbols, instrument types, identity checks and (for FinLab) one five-table
download per date. A deterministic one-member failure becomes that member's
evidence; only shared or transient failures retry the date. Each member's
prepared body, receipt and outcome are journaled under the fenced owner, so a
reclaimed date replays them without acquiring or delivering again.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx

from findb_fetcher.client import (
    SourceAPIClient,
    SourceAPIResponseError,
    SourceAPITransportError,
)
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.execution_metadata import historical_feed
from findb_fetcher.factset_eod import completion_status
from findb_fetcher.full_market_errors import ReadinessBlockedError
from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.full_market_universe import canonical_bytes, checksum
from findb_fetcher.historical_backfill import (
    HistoricalRunResult,
    HistoricalWorkDeferredError,
    HistoricalWorkItem,
)
from findb_fetcher.provider_adapters import AcquisitionContext, ProviderAdapter, adapter_for
from findb_fetcher.provider_work import (
    ProviderWorkAcquisitionStopped,
    ProviderWorkSourceUnavailableError,
    active,
)
from findb_fetcher.raw_storage import (
    R2RawPayloadStore,
    RawObject,
    RawPayloadFormat,
    RawPayloadStore,
    RawStorageConfig,
    attach_raw_object,
)

FULL_RELEASE_COHORT = "full_release"
# Native acquisitions per member, and per shared date table, before giving up.
MEMBER_ATTEMPTS = 5
_JOURNAL = "member-historical:"


def full_release_cohort(item: HistoricalWorkItem) -> bool:
    """The item froze its date's Full release; otherwise the legacy executor runs."""
    snapshot = item.universe_snapshot
    return isinstance(snapshot, dict) and snapshot.get("cohort") == FULL_RELEASE_COHORT


class MemberHistoricalRunner:
    def __init__(
        self,
        provider: str,
        *,
        delivery: Any,
        raw_store: RawPayloadStore,
        http: httpx.Client | None = None,
        adapter: ProviderAdapter | None = None,
        limits: dict[str, Any] | None = None,
    ) -> None:
        from findb_fetcher.platform_limits import limits as platform_limits

        self.provider = provider
        self.delivery, self.raw_store = delivery, raw_store
        self.http = http or httpx.Client(timeout=30)
        self.owns_http = http is None
        self.adapter = adapter or adapter_for(provider)
        self.limits = limits or platform_limits(provider)

    @classmethod
    def from_env(cls, provider: str) -> MemberHistoricalRunner:
        config = FetcherConfig.from_env()
        return cls(
            provider,
            delivery=SourceAPIClient(config, ContractRegistry(config.contracts_dir)),
            raw_store=R2RawPayloadStore(RawStorageConfig.from_env()),
        )

    def close(self) -> None:
        self.adapter.close()
        if self.owns_http:
            self.http.close()
        close = getattr(self.delivery, "close", None)
        if callable(close):
            close()

    def run(self, item: HistoricalWorkItem) -> HistoricalRunResult:
        owner = active()
        snapshot = item.universe_snapshot
        if (
            owner is None
            or item.provider != self.provider
            or owner.work["stable_key"] != f"historical:{item.item_id}"
            or not full_release_cohort(item)
            or not isinstance(snapshot, dict)
            or snapshot.get("trade_date") != item.trade_date.isoformat()
            or not snapshot.get("members")
        ):
            raise ValueError("historical Full release cohort or owner is unavailable")
        feed = historical_feed(item.provider, item.dataset_key)
        contract = feed["contract"]
        members = sorted(snapshot["members"], key=lambda member: member["member_key"])
        owner.freeze(
            {
                "mode": "canonical",
                "contract": {
                    "schema_id": contract["schema_id"],
                    "schema_version": contract["current_version"],
                },
                "universe_release_id": snapshot["release_id"],
                "members": [
                    {
                        "key": member["member_key"],
                        "source_symbol": member["provider_symbol"],
                        "mapping_status": member["mapping_status"],
                    }
                    for member in members
                ],
            }
        )
        try:
            outcomes = [self._member(owner, item, feed, member, members) for member in members]
        finally:
            # Native tables never outlive the owned date.
            self.adapter.reset()
        evidence = {
            "version": 2,
            "schema_id": contract["schema_id"],
            "schema_version": contract["current_version"],
            "trade_date": item.trade_date.isoformat(),
            "item_id": str(item.item_id),
            "request_id": str(item.request_id),
            "members": outcomes,
        }
        return HistoricalRunResult(None, evidence, completion_status(evidence))

    def _context(
        self, item: HistoricalWorkItem, feed: dict, cohort: list[dict]
    ) -> AcquisitionContext:
        now = datetime.now(timezone.utc)
        bound = int(self.limits["max_response_bytes"])

        def observe(size: int, *, declared: int = 0) -> None:
            # Account usage is recorded by the acquisition boundary; enforce the bound.
            if max(size, declared) > bound:
                raise ReadinessBlockedError("provider response exceeds transport size bound")

        def snapshot(url: str, *, provider_response: bool = False) -> bytes:
            raise ReadinessBlockedError("historical acquisition reads no official snapshot")

        return AcquisitionContext(
            dataset_key=item.dataset_key,
            trade_date=item.trade_date,
            fetched_at=now,
            feed=feed,
            delivery={
                "slot_id": feed["delivery"]["slot_id"],
                "scheduled_for": now.isoformat(),
                "target_data_date": item.trade_date.isoformat(),
                "work_item_id": f"historical:{item.item_id}",
            },
            contract=feed["contract"],
            limits=self.limits,
            http=self.http,
            persist=lambda raw, symbol, **kwargs: self._persist(
                raw, item.dataset_key, symbol, **kwargs
            ),
            observe_response=observe,
            rate_limited=lambda: None,
            snapshot=snapshot,
            cohort=cohort,
        )

    def _persist(
        self, raw: bytes, dataset: str, symbol: str, *, payload_format: RawPayloadFormat = "json"
    ) -> RawObject:
        stored = self.raw_store.persist(
            raw,
            dataset_key=dataset,
            source_symbol=symbol.replace("|", ":"),
            provider=self.provider,
            payload_format=payload_format,
        )
        if stored.sha256 != checksum(raw) or stored.size_bytes != len(raw):
            raise ReadinessBlockedError("R2 raw persistence identity differs from acquired bytes")
        return stored

    def _member(
        self,
        owner,
        item: HistoricalWorkItem,
        feed: dict,
        member: dict,
        cohort: list[dict],
    ) -> dict[str, Any]:
        key = member["member_key"]
        saved = owner.journal.response(_JOURNAL + "outcome:" + key)
        if saved is not None:
            return json.loads(saved)
        job_key = "historical-member:" + key
        restored = owner.restore(job_key=job_key)
        if len(restored) > 1:
            raise ValueError("historical prepared member identity is ambiguous")
        if restored:
            outcome = self._deliver(owner, key, job_key, json.loads(restored[0]))
        else:
            outcome = self._acquire(owner, item, feed, member, cohort, key, job_key)
        owner.journal.response(_JOURNAL + "outcome:" + key, canonical_bytes(outcome))
        return outcome

    def _acquire(
        self,
        owner,
        item: HistoricalWorkItem,
        feed: dict,
        member: dict,
        cohort: list[dict],
        key: str,
        job_key: str,
    ) -> dict[str, Any]:
        # A shared native table spends one date budget, never one per member.
        operation = (
            _JOURNAL + "shared:" + item.trade_date.isoformat()
            if self.adapter.pending_shared_fetch(item.dataset_key, item.trade_date)
            else _JOURNAL + "member:" + key
        )
        try:
            owner.journal.reserve_attempt(operation, MEMBER_ATTEMPTS)
        except ValueError:
            if operation.startswith(_JOURNAL + "shared:"):
                raise
            # Transient member failures are exhausted; the date continues without it.
            return {"member_key": key, "outcome": "missing"}
        dispatch_before = owner.dispatch_observations
        try:
            result = self.adapter.acquire(member, self._context(item, feed, cohort))
        except (ProviderWorkAcquisitionStopped, ProviderWorkSourceUnavailableError):
            if owner.dispatch_observations == dispatch_before:
                owner.journal.release_wait_attempt(operation)
            raise
        except QuotaBlockedError as exc:
            if exc.quota_wait and not exc.dispatched:
                if owner.dispatch_observations == dispatch_before:
                    owner.journal.release_wait_attempt(operation)
            raise HistoricalWorkDeferredError("historical provider quota wait") from None
        except Exception as exc:
            deterministic = self.adapter.member_outcome(exc)
            if deterministic is not None:
                outcome, reason = deterministic
                return {"member_key": key, "outcome": outcome, "reason": reason}
            # Rate limits, outages, an unpublished date or an SDK failure retry the
            # date later; the reserved attempt bounds how often this operation runs.
            raise HistoricalWorkDeferredError("historical provider retry") from None
        if result.no_trade is not None:
            if result.raw_object is None:
                raise ReadinessBlockedError("no-trade evidence lacks its raw observation")
            return {
                "member_key": key,
                "outcome": "no_data",
                "raw": {"ref": result.raw_object.ref, "sha256": result.raw_object.sha256},
            }
        request, stored = result.request, result.raw_object
        if request is None or stored is None:
            raise ReadinessBlockedError("provider acquisition produced no request")
        day = item.trade_date.isoformat()
        request["payload"]["batch"].update(
            data_date=day,
            delivery_mode="backfill",
            coverage_start_date=day,
            coverage_end_date=day,
        )
        identity = checksum(
            canonical_bytes(
                {"item": str(item.item_id), "member": key, "rows": request["payload"]["data"]}
            )
        )
        request["request_key"] = f"historical:{identity}"
        request["idempotency_key"] = f"historical:{identity}"
        attach_raw_object(request, stored)
        if len(canonical_bytes(request)) >= 1024 * 1024:
            raise ReadinessBlockedError("prepared Source body exceeds byte bound")
        return self._deliver(owner, key, job_key, request)

    def _deliver(self, owner, key: str, job_key: str, request: dict) -> dict[str, Any]:
        try:
            prepared = self.delivery.prepare(request, job_key=job_key)
        except ProviderWorkAcquisitionStopped:
            raise HistoricalWorkDeferredError("historical acquisition is suspended") from None
        receipt = _saved(
            owner,
            "receipt:" + key,
            lambda: {"run_id": str(self.delivery.deliver(prepared).run_id)},
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                terminal = self.delivery.get_run_status(UUID(receipt["run_id"]), expected=prepared)
            except (SourceAPITransportError, httpx.TransportError):
                raise HistoricalWorkDeferredError("Source status unavailable") from None
            except SourceAPIResponseError as exc:
                if exc.status_code >= 500 or exc.status_code in {408, 429}:
                    raise HistoricalWorkDeferredError("Source status unavailable") from None
                raise
            if terminal.status not in {"queued", "pending", "processing", "retrying"}:
                break
            if time.monotonic() >= deadline or owner.failed.is_set():
                # Settlement drains accepted runs before releasing ownership.
                raise HistoricalWorkDeferredError("EOD normalization pending")
            time.sleep(2)
        if terminal.total_records != 1:
            raise ValueError("historical member terminal row count differs")
        if (
            terminal.status == "completed"
            and terminal.success_records == 1
            and terminal.failed_records == 0
        ):
            return {"member_key": key, "outcome": "data"}
        if terminal.failed_records == 1 and terminal.success_records == 0:
            return {"member_key": key, "outcome": "invalid", "reason": "row_structure_invalid"}
        raise ValueError("historical member normalization did not produce a row outcome")


def _saved(owner, key: str, action: Callable[[], dict]) -> dict:
    """A successful Source side effect is replayed; a failed one keeps its budget."""
    key = _JOURNAL + key
    saved = owner.journal.response(key)
    if saved is not None:
        return json.loads(saved)
    owner.journal.reserve_attempt(key, 3)
    dispatched_before = owner.dispatch_observations
    try:
        result = action()
    except ProviderWorkSourceUnavailableError:
        if owner.dispatch_observations == dispatched_before:
            owner.journal.release_wait_attempt(key)
        raise
    except (SourceAPITransportError, httpx.TransportError):
        raise HistoricalWorkDeferredError("Source delivery unavailable") from None
    except SourceAPIResponseError as exc:
        if exc.status_code >= 500 or exc.status_code in {408, 429}:
            raise HistoricalWorkDeferredError("Source delivery unavailable") from None
        raise
    saved = owner.journal.response(key, canonical_bytes(result))
    assert saved is not None
    return json.loads(saved)
