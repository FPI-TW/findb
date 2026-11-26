"""One frozen current cohort, one native query and per-member EOD-v2 delivery.

Each delivered member's run is read back behind the next deliveries, within a
bounded window of unread runs, so normalization overlaps delivery instead of
holding every member until its own run is terminal.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Callable
from uuid import UUID

import httpx

from findb_fetcher.client import SourceAPIResponseError, SourceAPITransportError
from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.full_market_universe import canonical_bytes, checksum
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError
from findb_fetcher.provider_process import ProviderProcessError
from findb_fetcher.provider_wait import ProviderPublicationPendingError
from findb_fetcher.provider_work import (
    ProviderWorkAcquisitionStopped,
    ProviderWorkSourceUnavailableError,
    active,
)
from findb_fetcher.providers.factset import (
    FactSetGateway,
    FactSetOperationalError,
    FactSetRowError,
    contract_row,
    price_scope_complete,
)
from findb_fetcher.raw_storage import RawObject, RawStorageUploadError, attach_raw_object
from findb_fetcher.run_confirmation import UNCONFIRMED_WINDOW, terminal_run


class FactSetEOD:
    def __init__(
        self, gateway: FactSetGateway, delivery, raw_store, *, on_receipt: Callable | None = None
    ) -> None:
        self.gateway, self.delivery, self.raw_store = gateway, delivery, raw_store
        self.on_receipt = on_receipt

    def _saved(
        self,
        key: str,
        action: Callable[[], dict],
        *,
        target_valid: Callable[[dict], bool] | None = None,
    ) -> dict:
        """A successful side effect is replayed; a failed operation keeps its budget."""
        owner = active()
        if owner is None or owner.provider != "factset":
            raise ValueError("FactSet requires fenced provider work")
        key = "factset-eod:" + key
        saved = owner.journal.response(key)
        if saved is not None:
            return json.loads(saved)
        owner.journal.reserve_attempt(key, 3)
        dispatched_before = getattr(owner, "dispatch_observations", 0)
        try:
            result = action()
        except ProviderWorkSourceUnavailableError:
            if owner.dispatch_observations == dispatched_before:
                owner.journal.release_wait_attempt(key)
            raise
        except (Exception, ProviderWorkAcquisitionStopped) as exc:
            if (
                isinstance(exc, QuotaBlockedError)
                and exc.quota_wait
                and not exc.dispatched
                and getattr(owner, "dispatch_observations", 0) == dispatched_before
            ):
                owner.journal.release_wait_attempt(key)
            if isinstance(exc, ProviderWorkAcquisitionStopped):
                if getattr(owner, "dispatch_observations", 0) == dispatched_before:
                    owner.journal.release_wait_attempt(key)
                raise HistoricalWorkDeferredError("FactSet acquisition is suspended") from None
            retryable = (
                isinstance(
                    exc, (SourceAPITransportError, httpx.TransportError, ProviderProcessError)
                )
                or isinstance(exc, (FactSetOperationalError, RawStorageUploadError))
                and exc.retryable
                or isinstance(exc, SourceAPIResponseError)
                and (exc.status_code >= 500 or exc.status_code in {408, 429})
                or isinstance(exc, QuotaBlockedError)
            )
            if retryable:
                raise HistoricalWorkDeferredError(
                    "FactSet operation requires FIFO tail retry"
                ) from None
            raise
        encoded = canonical_bytes(result)
        if target_valid is not None and not target_valid(result):
            owner.journal.observe_response(key, encoded, origin="provider_not_ready")
            owner.journal.release_wait_attempt(key)
            raise ProviderPublicationPendingError("factset_no_source_row")
        saved = owner.journal.response(key, encoded)
        assert saved is not None
        return json.loads(saved)

    def run(
        self,
        *,
        day: date,
        members: list[dict[str, Any]],
        dataset: str,
        deliveries: dict[str, dict],
        historical: bool,
        defer_missing: bool = False,
    ) -> dict:
        owner = active()
        if owner is None or owner.provider != "factset" or not owner.work.get("manifest"):
            raise ValueError("FactSet requires a frozen whole-work manifest")
        expected = {row["key"] for row in owner.work["manifest"]["members"]}
        if {member["member_key"] for member in members} != expected or not members:
            raise ValueError("FactSet acquisition differs from the frozen member scope")
        listing_ids = tuple(
            sorted({m["provider_symbol"] for m in members if m["mapping_status"] == "mapped"})
        )

        # Freeze observed time together with native bytes. A retry cannot change
        # a prepared body merely because the process clock or source has moved.
        def acquire():
            raw = self.gateway.prices(day, listing_ids)
            return {"native": json.loads(raw), "fetched_at": datetime.now(timezone.utc).isoformat()}

        snapshot = (
            self._saved(
                "snapshot",
                acquire,
                target_valid=(lambda value: price_scope_complete(value.get("native"), listing_ids))
                if defer_missing
                else None,
            )
            if listing_ids
            else None
        )
        stored = None
        indexed: dict[str, list[dict]] = {}
        if snapshot is not None:
            native = snapshot.get("native")
            if (
                not isinstance(native, dict)
                or not isinstance(native.get("rows"), list)
                or any(not isinstance(row, dict) for row in native["rows"])
            ):
                raise FactSetRowError("FACTSET_PRICE_RESPONSE_INVALID")
            raw = canonical_bytes(native)

            def persist():
                value = self.raw_store.persist(
                    raw, dataset_key=dataset, source_symbol="frozen-cohort", provider="factset"
                )
                if value.sha256 != checksum(raw) or value.size_bytes != len(raw):
                    raise ValueError("FactSet native persistence identity differs")
                return {"ref": value.ref, "sha256": value.sha256, "size_bytes": value.size_bytes}

            stored = RawObject(**self._saved("raw", persist))
            for row in native["rows"]:
                indexed.setdefault(row.get("fsym_id", ""), []).append(row)
        results: dict[str, dict[str, Any]] = {}
        accepted: dict[str, tuple[Any, UUID]] = {}
        for member in members:
            key = member["member_key"]
            if member["mapping_status"] != "mapped":
                results[key] = {"member_key": key, "outcome": "mapping_gap"}
                continue
            assert stored is not None and snapshot is not None
            evidence = {"ref": stored.ref, "sha256": stored.sha256}
            selected = indexed.get(member["provider_symbol"], [])
            if not selected:
                results[key] = {
                    "member_key": key,
                    "outcome": "missing",
                    "raw": evidence,
                    "reason": "no_source_row",
                }
                continue
            try:
                if len(selected) != 1:
                    raise FactSetRowError("FACTSET_LISTING_DUPLICATED")
                row = contract_row(
                    selected[0],
                    symbol=member["symbol"],
                    listing_id=member["provider_symbol"],
                    day=day,
                )
                if row["currency"] != member["currency"]:
                    raise FactSetRowError("FACTSET_CURRENCY_DIFFERS")
            except FactSetRowError:
                results[key] = {
                    "member_key": key,
                    "outcome": "invalid",
                    "raw": evidence,
                    "reason": "row_structure_invalid",
                }
                continue
            job_key = "factset:" + key
            restored = owner.restore(job_key=job_key)
            if len(restored) > 1:
                raise ValueError("FactSet prepared member identity is ambiguous")
            if restored:
                request = json.loads(restored[0])
            else:
                stamp = snapshot["fetched_at"]
                batch = {
                    "data_date": day.isoformat(),
                    "delivery_mode": "backfill" if historical else "incremental",
                    "declared_record_count": 1,
                    **(
                        {
                            "coverage_start_date": day.isoformat(),
                            "coverage_end_date": day.isoformat(),
                        }
                        if historical
                        else {}
                    ),
                }
                request = {
                    "dataset_key": dataset,
                    "source": "factset",
                    "schema_id": "market_eod",
                    "schema_version": 2,
                    "fetched_at": stamp,
                    "request_key": "factset:"
                    + checksum(canonical_bytes({"work": owner.work["work_id"], "member": key})),
                    "payload": {"batch": batch, "data": [row]},
                    "delivery": {
                        **deliveries[key],
                        "scheduled_for": stamp,
                        "target_data_date": day.isoformat(),
                    },
                }
                request["idempotency_key"] = request["request_key"]
                attach_raw_object(request, stored)
            # SourceAPIClient validates before registering the one immutable
            # descriptor, including its job key, and then confirms original bytes.
            try:
                prepared = self.delivery.prepare(request, job_key=job_key)
            except ProviderWorkAcquisitionStopped:
                raise HistoricalWorkDeferredError("FactSet acquisition is suspended") from None
            receipt = self._saved(
                "receipt:" + key, lambda: {"run_id": str(self.delivery.deliver(prepared).run_id)}
            )
            if self.on_receipt:
                self.on_receipt(key, request, receipt["run_id"])
            accepted[key] = (prepared, UUID(receipt["run_id"]))
            while len(accepted) > UNCONFIRMED_WINDOW:
                oldest = next(iter(accepted))
                results[oldest] = _confirmed(owner, self.delivery, oldest, *accepted.pop(oldest))
        for key, (prepared, run_id) in accepted.items():
            results[key] = _confirmed(owner, self.delivery, key, prepared, run_id)
        return {
            "version": 2,
            "schema_id": "market_eod",
            "schema_version": 2,
            "trade_date": day.isoformat(),
            "members": [results[member["member_key"]] for member in members],
        }


def _confirmed(owner, delivery, key: str, prepared: Any, run_id: UUID) -> dict[str, Any]:
    """One accepted member's row outcome, read once its run is terminal."""
    terminal = terminal_run(delivery, run_id, prepared, failed=owner.failed)
    if terminal.total_records != 1:
        raise ValueError("FactSet terminal row count differs")
    if (
        terminal.status == "completed"
        and terminal.success_records == 1
        and terminal.failed_records == 0
    ):
        return {"member_key": key, "outcome": "data"}
    if (
        terminal.status == "completed_with_errors"
        and terminal.failed_records == 1
        and terminal.success_records == 0
    ):
        return {"member_key": key, "outcome": "invalid"}
    raise ValueError("FactSet normalization did not produce a row outcome")


def completion_status(evidence: dict) -> str:
    """Partial only for a member the provider could still supply; a mapping gap cannot be."""
    return (
        "partial"
        if any(
            row["outcome"] not in {"data", "no_data", "mapping_gap"} for row in evidence["members"]
        )
        else "completed"
    )
