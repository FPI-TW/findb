"""Concrete provider runtimes for leased historical work.

They reuse production executors, so acquisition, raw persistence, immutable
idempotency keys and terminal Source verification stay identical to recurrence.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig, app_environment
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.finlab_scheduler import FinLabScheduledExecutor
from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.historical_backfill import (
    HistoricalPreparedManualError,
    HistoricalWorkDeferredError,
    HistoricalWorkItem,
)
from findb_fetcher.provider_work import active, require_owner
from findb_fetcher.providers.finlab import FinLabSdkGateway, FinLabSymbol
from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig
from findb_fetcher.schedule import ScheduleConfig, load_schedule_manifest
from findb_fetcher.scheduler_state import ScheduledJob
from findb_fetcher.twelve_data_scheduler import TwelveDataScheduledExecutor
from findb_fetcher.universe import UniverseSymbol, load_symbol_universe


class HistoricalRuntimeError(RuntimeError):
    pass


def _execute(executor, schedule, job):
    owner = active()
    dispatch_before = owner.dispatch_observations if owner else 0
    if owner:
        job = replace(
            job, attempt_count=owner.journal.reserve_attempt(job.job_key, schedule.max_attempts)
        )
    from findb_fetcher.provider_work import (
        ProviderWorkAcquisitionStopped,
        ProviderWorkSourceUnavailableError,
    )

    try:
        result = executor.execute(job, now=datetime.now(timezone.utc))
    except (ProviderWorkAcquisitionStopped, ProviderWorkSourceUnavailableError):
        if owner and owner.dispatch_observations == dispatch_before:
            owner.journal.release_wait_attempt(job.job_key)
        raise
    if owner and result.succeeded:
        owner.journal.release_wait_attempt(job.job_key)
    if not result.succeeded and result.retryable:
        if owner and (
            result.prepared_delivery_wait
            or result.predispatch_quota_wait
            and owner.dispatch_observations == dispatch_before
        ):
            owner.journal.release_wait_attempt(job.job_key)
        raise HistoricalWorkDeferredError("historical operation is retryable")
    if not result.succeeded and result.outcome == "source_rejected":
        raise HistoricalPreparedManualError("permanent prepared Source rejection")
    return result


def _schedule(provider: str, dataset_key: str) -> ScheduleConfig:
    default_schedule = (
        "/app/configs/daily_scheduler.production.v3.json"
        if app_environment("staging").strip().lower() == "production"
        else "/app/configs/daily_scheduler.v2.json"
    )
    path = Path(os.getenv("FETCHER_HISTORICAL_SCHEDULE_FILE", default_schedule))
    for feed in load_schedule_manifest(path).feeds:
        if feed.provider == provider and feed.dataset_key == dataset_key and feed.enabled:
            return feed
    raise HistoricalRuntimeError("historical provider scope is not enabled")


def _job(
    schedule: ScheduleConfig, item: HistoricalWorkItem, symbol: str, canonical: str, exchange: str
) -> ScheduledJob:
    owner = active()
    job_key = f"historical:{item.request_id}:{item.trade_date}:{symbol}"
    return ScheduledJob(
        job_key=job_key,
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
        prepared_request=owner.journal.prepared(job_key) if owner else None,
    )


class TwelveDataHistoricalRunner:
    @classmethod
    def from_env(cls) -> "TwelveDataHistoricalRunner":
        return cls()

    def run(self, item: HistoricalWorkItem) -> UUID:
        require_owner(item.provider)
        if item.provider != "twelve_data":
            raise HistoricalRuntimeError("provider mismatch")
        schedule = _schedule(item.provider, item.dataset_key)
        universe = load_symbol_universe(schedule.universe_file)
        owner = active()
        if owner:
            frozen = owner.freeze(
                {
                    "mode": "canonical",
                    "members": [
                        {
                            "key": member.canonical_symbol,
                            "symbol": member.symbol,
                            "exchange": member.exchange,
                        }
                        for member in universe.symbols
                    ],
                    "universe_id": universe.universe_id,
                    "universe_version": universe.universe_version,
                }
            )
            universe = replace(
                universe,
                universe_id=frozen["universe_id"],
                universe_version=frozen["universe_version"],
                symbols=tuple(
                    UniverseSymbol(row["symbol"], row["key"], row["exchange"])
                    for row in frozen["members"]
                ),
            )
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
                state=owner.journal if owner else None,  # type: ignore[arg-type]
            )
            for member in universe.symbols:
                result = _execute(
                    executor,
                    schedule,
                    _job(schedule, item, member.symbol, member.canonical_symbol, member.exchange),
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
        require_owner(item.provider)
        if item.provider != "finlab":
            raise HistoricalRuntimeError("provider mismatch")
        schedule = _schedule(item.provider, item.dataset_key)
        default_universe = (
            "/app/configs/finlab_tw50_2026_09_21.v2.json"
            if app_environment("staging").strip().lower() == "production"
            else "/app/configs/finlab_tw_review_required.v1.json"
        )
        universe_path = Path(os.getenv("FETCHER_FINLAB_UNIVERSE_FILE", default_universe))
        universe = load_finlab_universe(universe_path)
        owner = active()
        if owner:
            frozen = owner.freeze(
                {
                    "mode": "canonical",
                    "members": [
                        {"key": member.canonical_symbol, "symbol": member.source_symbol}
                        for member in universe.symbols
                    ],
                    "manifest_id": universe.manifest_id,
                    "manifest_version": universe.manifest_version,
                    "field_datasets": universe.field_datasets,
                }
            )
            universe = replace(
                universe,
                manifest_id=frozen["manifest_id"],
                manifest_version=frozen["manifest_version"],
                field_datasets=frozen["field_datasets"],
                symbols=tuple(FinLabSymbol(row["symbol"], row["key"]) for row in frozen["members"]),
            )
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
                state=owner.journal if owner else None,  # type: ignore[arg-type]
            )
            result = _execute(
                executor,
                schedule,
                _job(schedule, item, universe.dataset_key, universe.dataset_key, universe.market),
            )
        if not result.succeeded or result.run_id is None:
            raise HistoricalRuntimeError("finlab terminal delivery failed")
        return result.run_id
