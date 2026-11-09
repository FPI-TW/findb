"""Historical FactSet runner using the request's immutable current HKEX cohort."""

from __future__ import annotations

from dataclasses import replace

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.execution_metadata import historical_feed
from findb_fetcher.factset_eod import FactSetEOD, completion_status
from findb_fetcher.historical_backfill import HistoricalRunResult, HistoricalWorkItem
from findb_fetcher.provider_work import active
from findb_fetcher.providers.factset import FactSetConfig, FactSetGateway
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig


class FactSetHistoricalRunner:
    def __init__(self, *, gateway, delivery, raw_store, feed: dict) -> None:
        self.delivery, self.feed = delivery, feed
        self.eod = FactSetEOD(gateway, delivery, raw_store)

    @classmethod
    def from_env(cls) -> FactSetHistoricalRunner:
        config = replace(FetcherConfig.from_env(), max_attempts=1)
        metadata = historical_feed("factset", "hk_equity_eod")
        feed = {"slot_id": metadata["delivery"]["slot_id"]}
        return cls(
            gateway=FactSetGateway(FactSetConfig.from_env()),
            delivery=SourceAPIClient(config, ContractRegistry(config.contracts_dir)),
            raw_store=R2RawPayloadStore(RawStorageConfig.from_env(), total_max_attempts=1),
            feed=feed,
        )

    def run(self, item: HistoricalWorkItem) -> HistoricalRunResult:
        owner = active()
        snapshot = item.universe_snapshot
        if (
            owner is None
            or item.provider != "factset"
            or item.dataset_key != "hk_equity_eod"
            or owner.work["stable_key"] != f"historical:{item.item_id}"
            or not snapshot
            or snapshot.get("cohort") != "current_hkex"
        ):
            raise ValueError("FactSet historical cohort or owner is unavailable")
        members = snapshot["members"]
        owner.freeze(
            {
                "mode": "canonical",
                "contract": {"schema_id": "market_eod", "schema_version": 2},
                "universe_release_id": snapshot["release_id"],
                "members": [
                    {
                        "key": m["member_key"],
                        "source_symbol": m["provider_symbol"],
                        "mapping_status": m["mapping_status"],
                    }
                    for m in members
                ],
            }
        )
        evidence = self.eod.run(
            day=item.trade_date,
            dataset=item.dataset_key,
            members=members,
            historical=True,
            deliveries={
                m["member_key"]: {
                    "slot_id": self.feed["slot_id"],
                    "work_item_id": f"historical:{item.item_id}",
                }
                for m in members
            },
        )
        evidence.update(item_id=str(item.item_id), request_id=str(item.request_id))
        return HistoricalRunResult(None, evidence, completion_status(evidence))

    def close(self) -> None:
        self.delivery.__exit__(None, None, None)
