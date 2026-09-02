"""Concrete provider runtimes for leased historical work.

They reuse production executors, so acquisition, raw persistence, immutable
idempotency keys and terminal Source verification stay identical to recurrence.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.finlab_scheduler import FinLabScheduledExecutor
from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.historical_backfill import HistoricalWorkItem
from findb_fetcher.providers.finlab import FinLabSdkGateway
from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig
from findb_fetcher.schedule import ScheduleConfig, load_schedule_manifest
from findb_fetcher.scheduler_state import ScheduledJob
from findb_fetcher.twelve_data_scheduler import TwelveDataScheduledExecutor
from findb_fetcher.universe import load_symbol_universe


class HistoricalRuntimeError(RuntimeError):
    pass


def _schedule(provider: str, dataset_key: str) -> ScheduleConfig:
    path = Path(
        os.getenv("FETCHER_HISTORICAL_SCHEDULE_FILE", "/app/configs/daily_scheduler.v2.json")
    )
    for feed in load_schedule_manifest(path).feeds:
        if feed.provider == provider and feed.dataset_key == dataset_key and feed.enabled:
            return feed
    raise HistoricalRuntimeError("historical provider scope is not enabled")


def _job(
    schedule: ScheduleConfig, item: HistoricalWorkItem, symbol: str, canonical: str, exchange: str
) -> ScheduledJob:
    return ScheduledJob(
        job_key=f"historical:{item.request_id}:{item.trade_date}:{symbol}",
        schedule_id=schedule.schedule_id,
        scheduled_date=item.trade_date,
        universe_id=f"historical:{item.request_id}",
        universe_version=1,
        symbol=symbol,
        canonical_symbol=canonical,
        exchange=exchange,
        status="running",
        attempt_count=1,
        slot_id=schedule.slot_id,
        provider=schedule.provider,
        dataset_key=schedule.dataset_key,
        work_item=symbol,
        target_data_date=item.trade_date,
        checkpoint_before=None,
        prepared_request=None,
    )


class TwelveDataHistoricalRunner:
    @classmethod
    def from_env(cls) -> "TwelveDataHistoricalRunner":
        return cls()

    def run(self, item: HistoricalWorkItem) -> UUID:
        if item.provider != "twelve_data":
            raise HistoricalRuntimeError("provider mismatch")
        schedule = _schedule(item.provider, item.dataset_key)
        universe = load_symbol_universe(schedule.universe_file)
        config = FetcherConfig.from_env()
        registry = ContractRegistry(config.contracts_dir)
        last: UUID | None = None
        with (
            TwelveDataClient(TwelveDataConfig.from_env()) as provider,
            SourceAPIClient(config, registry) as source,
        ):
            executor = TwelveDataScheduledExecutor(
                schedule=schedule,
                universe=universe,
                provider=provider,
                source=source,
                registry=registry,
                raw_store=R2RawPayloadStore(RawStorageConfig.from_env()),
            )
            for member in universe.symbols:
                result = executor.execute(
                    _job(schedule, item, member.symbol, member.canonical_symbol, member.exchange),
                    now=datetime.now(timezone.utc),
                )
                if not result.succeeded or result.run_id is None:
                    raise HistoricalRuntimeError("twelve_data terminal delivery failed")
                last = result.run_id
        if last is None:
            raise HistoricalRuntimeError("twelve_data produced no Source receipt")
        return last


class FinLabHistoricalRunner:
    @classmethod
    def from_env(cls) -> "FinLabHistoricalRunner":
        return cls()

    def run(self, item: HistoricalWorkItem) -> UUID:
        if item.provider != "finlab":
            raise HistoricalRuntimeError("provider mismatch")
        schedule = _schedule(item.provider, item.dataset_key)
        universe_path = Path(
            os.getenv("FETCHER_FINLAB_UNIVERSE_FILE", "/app/configs/finlab_tw_equity_eod.v1.json")
        )
        universe = load_finlab_universe(universe_path)
        config = FetcherConfig.from_env()
        registry = ContractRegistry(config.contracts_dir)
        with SourceAPIClient(config, registry) as source:
            executor = FinLabScheduledExecutor(
                schedule=schedule,
                universe=universe,
                provider=FinLabSdkGateway.from_env(),
                source=source,
                registry=registry,
                raw_store=R2RawPayloadStore(RawStorageConfig.from_env()),
            )
            result = executor.execute(
                _job(schedule, item, universe.dataset_key, universe.dataset_key, universe.market),
                now=datetime.now(timezone.utc),
            )
        if not result.succeeded or result.run_id is None:
            raise HistoricalRuntimeError("finlab terminal delivery failed")
        return result.run_id
