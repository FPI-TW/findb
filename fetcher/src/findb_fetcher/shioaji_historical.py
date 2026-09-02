"""Isolated Shioaji historical acquisition for control-plane work items.

The recurring scheduler's cutoff/state are intentionally not used.  The fixed
reviewed universe and gateway rate/IPC boundaries are retained; a caller must
provide the same raw-first contract delivery bridge used by production.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.historical_backfill import HistoricalWorkItem
from findb_fetcher.providers.shioaji import IsolatedShioajiGateway, ShioajiKbarsSnapshot
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig
from findb_fetcher.shioaji_scheduler import (
    EXPECTED,
    ProductionCoordinator,
    default_manifest_path,
    load_manifest,
)
from findb_fetcher.shioaji_staging_state import ShioajiStagingState


class ShioajiHistoricalBridgeUnavailableError(RuntimeError):
    """Fail closed when deployment has not supplied the normal delivery bridge."""


@dataclass(slots=True)
class ShioajiHistoricalRunner:
    gateway: IsolatedShioajiGateway
    deliver_snapshot: Callable[[HistoricalWorkItem, str, ShioajiKbarsSnapshot], UUID]

    def run(self, item: HistoricalWorkItem) -> UUID:
        if item.provider != "shioaji":
            raise ShioajiHistoricalBridgeUnavailableError("provider mismatch")
        sequences = [row for row in EXPECTED if row["dataset_key"] == item.dataset_key]
        if not sequences:
            raise ShioajiHistoricalBridgeUnavailableError(
                "dataset is outside fixed Shioaji universe"
            )
        # Every historical date uses a new local execution context.  This
        # bypasses only the recurring cutoff; it does not alter scheduler state.
        last_run: UUID | None = None
        for sequence in sequences:
            snapshot = self.gateway.fetch_kbars(str(sequence["symbol"]), item.trade_date)
            last_run = self.deliver_snapshot(item, str(sequence["symbol"]), snapshot)
        if last_run is None:
            raise ShioajiHistoricalBridgeUnavailableError("sequenced snapshot has no Source run")
        return last_run


class ExecutableShioajiHistoricalRunner:
    """Concrete request-scoped runner used by the historical worker CLI."""

    def __init__(self, *, fetcher: FetcherConfig, contracts: ContractRegistry) -> None:
        self.fetcher = fetcher
        self.contracts = contracts

    @classmethod
    def from_env(cls) -> "ExecutableShioajiHistoricalRunner":
        fetcher = FetcherConfig.from_env()
        return cls(fetcher=fetcher, contracts=ContractRegistry(fetcher.contracts_dir))

    def run(self, item: HistoricalWorkItem) -> UUID:
        if item.provider != "shioaji":
            raise ShioajiHistoricalBridgeUnavailableError("provider mismatch")
        rows = [dict(row) for row in EXPECTED if row["dataset_key"] == item.dataset_key]
        if not rows:
            raise ShioajiHistoricalBridgeUnavailableError(
                "dataset is outside fixed Shioaji universe"
            )
        manifest = load_manifest(default_manifest_path())
        # Preserve fixed provider sequence semantics, but lease only the one
        # requested dataset. The SQLite state cannot overlap recurring state.
        manifest = {
            **manifest,
            "sequences": rows,
            "universe_id": f"{manifest['universe_id']}:historical:{item.request_id}",
        }
        root = Path(os.getenv("FETCHER_HISTORICAL_STATE_DIR", "/var/lib/findb-historical"))
        state = ShioajiStagingState(root / f"{item.request_id}.sqlite3")
        gateway = IsolatedShioajiGateway.from_env()
        raw_store = R2RawPayloadStore(RawStorageConfig.from_env())
        with SourceAPIClient(self.fetcher, self.contracts) as source:
            coordinator = ProductionCoordinator(
                state,
                manifest,
                gateway,
                raw_store=raw_store,
                source=source,
                contracts=self.contracts,
                now=lambda: datetime.now(ZoneInfo("Asia/Taipei")),
            )
            results = coordinator.run(item.trade_date, deliver=True, historical=True)
        completed = [result for result in results if result.code == "completed"]
        if len(completed) != len(rows):
            raise ShioajiHistoricalBridgeUnavailableError("sequenced snapshot did not complete")
        # The final sequenced receipt is a useful, non-secret control-plane
        # reference. Every completed sequence must have one; omit nothing
        # silently because the dashboard needs to link an item to Source.
        final_run_id = completed[-1].run_id
        if final_run_id is None:
            raise ShioajiHistoricalBridgeUnavailableError("sequenced snapshot has no Source run")
        try:
            return UUID(final_run_id)
        except (TypeError, ValueError) as exc:
            raise ShioajiHistoricalBridgeUnavailableError(
                "sequenced snapshot has an invalid Source run"
            ) from exc
