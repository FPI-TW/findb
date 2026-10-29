"""Durable, raw-first, whole-day TAIFEX historical acquisition and delivery."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import sqlite3
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import httpx

from findb_fetcher.client import (
    SourceAPIClient,
    SourceAPIResponseError,
    SourceAPITransportError,
)
from findb_fetcher.config import FetcherConfig, app_environment
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_cli import load_config
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.full_market_universe import TAIFEX_HISTORY_URL, canonical_bytes, checksum
from findb_fetcher.historical_backfill import (
    HistoricalControlError,
    HistoricalRunResult,
    HistoricalWorkDeferredError,
    HistoricalWorkItem,
)
from findb_fetcher.platform_limits import account_identity
from findb_fetcher.providers.taifex import (
    PRODUCTS,
    TaifexOperationalError,
    fetch_report,
    is_no_trade,
    persist_dated_report,
)
from findb_fetcher.raw_storage import (
    R2RawPayloadStore,
    RawStorageConfig,
    RawStorageUploadError,
    attach_raw_object,
)


class TaifexHistoricalRunner:
    def __init__(
        self,
        *,
        path: Path,
        http: Any,
        source: Any,
        raw_store: Any,
        registry: ContractRegistry,
        feed: dict[str, Any],
        identity: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path, self.http, self.source = path, http, source
        self.raw_store, self.registry, self.feed = raw_store, registry, feed
        self.clock, self.sleep = clock, sleep
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("historical checkpoint must not be a symlink")
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS identity (value TEXT PRIMARY KEY)")
            rows = db.execute("SELECT value FROM identity").fetchall()
            if rows and rows != [(identity,)]:
                raise ValueError("historical checkpoint environment/Source identity differs")
            db.execute("INSERT OR IGNORE INTO identity VALUES (?)", (identity,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS work (item_id TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
        os.chmod(path, 0o600)

    @classmethod
    def from_env(cls) -> TaifexHistoricalRunner:
        from hashlib import sha256

        config = replace(FetcherConfig.from_env(), max_attempts=1)
        registry = ContractRegistry(config.contracts_dir)
        target = app_environment()
        manifest = load_config(
            Path(
                os.getenv(
                    "FETCHER_FULL_MARKET_CONFIG", f"/app/configs/full_market.{target}.v1.json"
                )
            )
        )
        if manifest["deployment_target"] != target:
            raise ValueError("historical feed target differs")
        feed = next(
            feed
            for feed in manifest["feeds"]
            if feed["provider"] == "taifex" and feed["dataset_key"] == "tw_futures_eod"
        )
        root = Path(os.getenv("FETCHER_HISTORICAL_STATE_DIR", "/var/lib/findb-historical-taifex"))
        return cls(
            path=root / "state.sqlite3",
            http=httpx.Client(timeout=30, follow_redirects=False),
            source=SourceAPIClient(config, registry),
            raw_store=R2RawPayloadStore(RawStorageConfig.from_env(), total_max_attempts=1),
            registry=registry,
            feed=feed,
            identity=f"{target}:{config.source_api_url}:{sha256(config.source_client_key.encode()).hexdigest()}",
        )

    def _save(self, item: HistoricalWorkItem, state: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO work VALUES (?,?) ON CONFLICT(item_id) DO UPDATE SET body=excluded.body",
                (str(item.item_id), canonical_bytes(state).decode()),
            )

    def _cooldown(self) -> float:
        path = os.getenv("FETCHER_ACCOUNT_STATE_PATH")
        if not path:
            return 0
        state = FullMarketState(Path(path))
        account = account_identity("taifex", state)
        with state.connection() as db:
            row = db.execute(
                "SELECT next_at FROM full_pacing WHERE account=?", (account,)
            ).fetchone()
            return float(row[0]) if row else 0

    def _operation(
        self, item: HistoricalWorkItem, state: dict[str, Any], key: str, action: Callable[[], Any]
    ) -> Any:
        attempt = state.setdefault("operations", {}).setdefault(key, {"attempts": 0, "next_at": 0})
        while True:
            now = self.clock()
            ready = max(
                float(attempt["next_at"]), self._cooldown() if key.startswith("fetch:") else 0
            )
            expiry = item.lease_expires_at.timestamp() if item.lease_expires_at else now + 900
            from findb_fetcher.provider_work import active

            owner = active()
            if owner:
                if owner.failed.is_set():
                    raise HistoricalWorkDeferredError("provider work heartbeat unavailable")
                expiry = now + 900
            if max(now, ready) + 35 >= expiry:
                raise HistoricalWorkDeferredError("lease boundary")
            if ready > now:
                self.sleep(min(30, ready - now))
                continue
            count = attempt.get("attempts", attempt.get("failures", 0))
            if count >= 3:
                raise ValueError("historical operation retry exhausted")
            # Commit budget and retry floor before dispatch, including hard interruption.
            # The caller acknowledges success atomically with its durable result.
            attempt["attempts"] = count + 1
            audit = {"key": key, "attempt": count + 1, "reserved_at": now, "outcome": "unknown"}
            state.setdefault("operation_history", []).append(audit)
            delay = (30, 120, 120)[count]
            attempt["next_at"] = now + delay
            self._save(item, state)
            try:
                result = action()
            except Exception as exc:
                if isinstance(exc, QuotaBlockedError) and exc.quota_wait and not exc.dispatched:
                    audit["outcome"] = "quota_wait"
                    audit["predispatch"] = True
                    attempt["attempts"] = count
                    attempt.setdefault("quota_waits", []).append(self.clock())
                    attempt["next_at"] = max(self.clock() + 60, self._cooldown())
                    self._save(item, state)
                    if owner:
                        raise HistoricalWorkDeferredError("account quota wait") from None
                    continue
                retryable = (
                    isinstance(
                        exc, (httpx.TransportError, SourceAPITransportError, QuotaBlockedError)
                    )
                    or isinstance(exc, HistoricalControlError)
                    and (
                        exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500
                    )
                    or isinstance(exc, TaifexOperationalError)
                    and exc.retryable
                    or isinstance(exc, SourceAPIResponseError)
                    and (exc.status_code == 429 or exc.status_code >= 500)
                    or isinstance(exc, RawStorageUploadError)
                    and exc.retryable
                )
                if not retryable:
                    audit["outcome"] = "failed"
                    self._save(item, state)
                    raise
                audit["outcome"] = "retryable_failure"
                attempt["next_at"] = self.clock() + max(delay, getattr(exc, "retry_after", 0) or 0)
                self._save(item, state)
                if owner:
                    raise HistoricalWorkDeferredError(
                        "retryable operation retained for FIFO tail retry"
                    ) from None
                if attempt["attempts"] >= 3:
                    raise ValueError("historical operation retry exhausted") from None
            else:
                audit["outcome"] = "success"
                state["operations"].pop(key)
                return result

    def _freeze_evidence(self, evidence: dict) -> None:
        from findb_fetcher.provider_work import active

        owner = active()
        if owner:
            owner.freeze(
                {
                    "mode": "canonical",
                    "members": [
                        {"key": f"{row['product_code']}|{row['contract_code']}|{row['session']}"}
                        for row in evidence["rows"]
                    ],
                    "no_data_evidence": {
                        f"{row['product_code']}|{row['contract_code']}|{row['session']}": evidence[
                            "manifest"
                        ]
                        for row in evidence["rows"]
                        if is_no_trade(row)
                    },
                    "evidence": evidence,
                }
            )

    def run(self, item: HistoricalWorkItem) -> HistoricalRunResult:
        from findb_fetcher.provider_work import require_owner

        require_owner(item.provider)
        if item.lease_expires_at is None or item.lease_expires_at.tzinfo is None:
            raise ValueError("TAIFEX requires a timezone-aware historical lease expiry")
        if item.provider != "taifex" or item.dataset_key != "tw_futures_eod":
            raise ValueError("historical TAIFEX scope differs")
        with self.path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(self.path) as db:
                row = db.execute(
                    "SELECT body FROM work WHERE item_id=?", (str(item.item_id),)
                ).fetchone()
            identity = {
                "request_id": str(item.request_id),
                "trade_date": item.trade_date.isoformat(),
            }
            state: dict[str, Any] = (
                json.loads(row[0]) if row else {**identity, "reports": {}, "bytes": {}}
            )
            if any(state.get(key) != value for key, value in identity.items()):
                raise ValueError("historical checkpoint scope differs")
            from findb_fetcher.provider_work import active

            owner = active()
            if (
                owner
                and "prepared" not in state
                and (owner.work.get("manifest") or {}).get("evidence")
            ):
                restored = owner.restore(job_key=f"historical:{item.item_id}")
                if restored:
                    if len(restored) != 1:
                        raise ValueError("historical prepared identity is ambiguous")
                    state["prepared"] = json.loads(restored[0])
                    state["evidence"] = owner.work["manifest"]["evidence"]
                    self._save(item, state)
            if "prepared" not in state and "evidence" not in state:
                for product in PRODUCTS:
                    if product in state["reports"]:
                        continue
                    if product not in state["bytes"]:
                        raw = self._operation(
                            item,
                            state,
                            "fetch:" + product,
                            lambda: fetch_report(
                                self.http,
                                target_date=item.trade_date,
                                latest=False,
                                product=product,
                            ),
                        )
                        state["bytes"][product] = base64.b64encode(raw).decode()
                        self._save(item, state)
                    raw = base64.b64decode(state["bytes"][product], validate=True)
                    stored, rows = persist_dated_report(
                        raw,
                        target_date=item.trade_date,
                        product=product,
                        persist=lambda value: self._operation(
                            item,
                            state,
                            "raw:" + product,
                            lambda: self.raw_store.persist(
                                value,
                                dataset_key=item.dataset_key,
                                source_symbol=product,
                                provider="taifex",
                                payload_format="csv_cp950",
                            ),
                        ),
                    )
                    if stored.sha256 != checksum(raw) or stored.size_bytes != len(raw):
                        raise ValueError("persisted report checksum differs")
                    state["reports"][product] = {
                        "reference": {
                            **asdict(stored),
                            "product_code": product,
                            "url": TAIFEX_HISTORY_URL,
                        },
                        "rows": rows,
                    }
                    self._save(item, state)
                if "manifest_body" not in state:
                    rows = sorted(
                        [row for report in state["reports"].values() for row in report["rows"]],
                        key=lambda row: (row["product_code"], row["contract_code"], row["session"]),
                    )
                    state["manifest_body"] = {
                        "version": 1,
                        "item_id": str(item.item_id),
                        **identity,
                        "fetched_at": datetime.fromtimestamp(self.clock(), timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "reports": [state["reports"][product]["reference"] for product in PRODUCTS],
                        "rows": rows,
                    }
                    self._save(item, state)
                manifest = state["manifest_body"]
                raw_manifest = canonical_bytes(manifest)
                stored = self._operation(
                    item,
                    state,
                    "manifest",
                    lambda: self.raw_store.persist(
                        raw_manifest,
                        dataset_key=item.dataset_key,
                        source_symbol="historical-manifest",
                        provider="taifex",
                    ),
                )
                if stored.sha256 != checksum(raw_manifest) or stored.size_bytes != len(
                    raw_manifest
                ):
                    raise ValueError("persisted manifest checksum differs")
                evidence = {**manifest, "manifest": asdict(stored)}
                data = [row for row in manifest["rows"] if not is_no_trade(row)]
                if len(manifest["rows"]) > 5000 or len(raw_manifest) > 1_000_000:
                    raise ValueError("historical report exceeds completion bounds")
                if data:
                    request = {
                        "dataset_key": item.dataset_key,
                        "schema_id": "futures_eod",
                        "schema_version": 1,
                        "source": "taifex",
                        "request_key": f"historical:{item.item_id}",
                        "idempotency_key": f"historical:{item.item_id}",
                        "fetched_at": manifest["fetched_at"],
                        "delivery": {
                            "slot_id": self.feed["slot_id"],
                            "scheduled_for": manifest["fetched_at"],
                            "target_data_date": item.trade_date.isoformat(),
                            "work_item_id": f"historical:{item.item_id}",
                        },
                        "payload": {
                            "batch": {
                                "data_date": item.trade_date.isoformat(),
                                "delivery_mode": "backfill",
                                "coverage_start_date": item.trade_date.isoformat(),
                                "coverage_end_date": item.trade_date.isoformat(),
                                "declared_record_count": len(data),
                            },
                            "data": data,
                        },
                    }
                    attach_raw_object(request, stored)
                    self.registry.validate("futures_eod", 1, request)
                    if len(canonical_bytes(request)) > 1_000_000:
                        raise ValueError("historical Source batch exceeds bounds")
                    self._freeze_evidence(evidence)
                    if owner:
                        owner.reserve(request, job_key=f"historical:{item.item_id}")
                    state["prepared"] = request
                state["evidence"] = evidence
                self._save(item, state)
            self._freeze_evidence(state["evidence"])
            if "prepared" not in state:
                return HistoricalRunResult(None, state["evidence"])
            prepared = self.source.prepare(state["prepared"])
            if "receipt" not in state:
                receipt = self._operation(
                    item, state, "delivery", lambda: self.source.deliver(prepared)
                )
                state["receipt"] = str(receipt.run_id)
                self._save(item, state)
            run_id = UUID(state["receipt"])
            while True:
                terminal = self._operation(
                    item,
                    state,
                    "terminal",
                    lambda: self.source.get_run_status(run_id, expected=prepared),
                )
                self._save(item, state)
                if terminal.status in {"queued", "pending", "processing", "retrying"}:
                    self.sleep(15)
                    continue
                if (
                    terminal.status != "completed"
                    or terminal.failed_records
                    or terminal.total_records != len(state["prepared"]["payload"]["data"])
                    or terminal.success_records != terminal.total_records
                ):
                    raise ValueError("historical canonical terminal proof incomplete")
                return HistoricalRunResult(run_id, state["evidence"])

    def complete(self, control: Any, item: HistoricalWorkItem, result: HistoricalRunResult) -> None:
        with self.path.with_suffix(".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(self.path) as db:
                state = json.loads(
                    db.execute(
                        "SELECT body FROM work WHERE item_id=?", (str(item.item_id),)
                    ).fetchone()[0]
                )
            self._operation(item, state, "completion", lambda: control.complete(item, result))
            self._save(item, state)

    def close(self) -> None:
        self.http.close()
        self.source.__exit__(None, None, None)
