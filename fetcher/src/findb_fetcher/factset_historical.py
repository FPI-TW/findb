"""Historical FactSet runner using the request's immutable current HKEX cohort."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig, app_environment
from findb_fetcher.contracts import ContractRegistry
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
        from findb_fetcher.full_market_cli import load_config

        config = replace(FetcherConfig.from_env(), max_attempts=1)
        target = app_environment()
        manifest = load_config(
            Path(
                os.getenv(
                    "FETCHER_FULL_MARKET_CONFIG", f"/app/configs/full_market.{target}.v1.json"
                )
            )
        )
        if manifest["deployment_target"] != target:
            raise ValueError("FactSet historical environment differs")
        feed = next(feed for feed in manifest["feeds"] if feed["provider"] == "factset")
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
