"""Concrete provider runtimes for leased historical work.

A date whose request froze its Full release cohort is acquired member by member
through the Full daily adapters (``member_historical``). Requests created before
that freeze carry no cohort and keep the legacy scheduled executors below.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.execution_metadata import ExecutionDescriptor, historical_execution
from findb_fetcher.finlab_scheduler import FinLabScheduledExecutor
from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.historical_backfill import (
    HistoricalPreparedManualError,
    HistoricalRunResult,
    HistoricalWorkDeferredError,
    HistoricalWorkItem,
)
from findb_fetcher.member_historical import MemberHistoricalRunner, full_release_cohort
from findb_fetcher.provider_work import active, require_owner
from findb_fetcher.providers.finlab import FinLabSdkGateway, FinLabSymbol
from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig
from findb_fetcher.scheduler_state import ScheduledJob
from findb_fetcher.twelve_data_scheduler import TwelveDataScheduledExecutor
from findb_fetcher.universe import UniverseSymbol, load_symbol_universe


class HistoricalRuntimeError(RuntimeError):
    pass


def _execute(executor, execution, job):
    owner = active()
    dispatch_before = owner.dispatch_observations if owner else 0
    if owner:
        job = replace(
            job, attempt_count=owner.journal.reserve_attempt(job.job_key, execution.max_attempts)
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


def _job(
    execution: ExecutionDescriptor,
    item: HistoricalWorkItem,
    symbol: str,
    canonical: str,
    exchange: str,
) -> ScheduledJob:
    owner = active()
    job_key = f"historical:{item.request_id}:{item.trade_date}:{symbol}"
    return ScheduledJob(
        job_key=job_key,
        schedule_id=execution.schedule_id,
        scheduled_date=item.trade_date,
        universe_id=f"historical:{item.request_id}",
        universe_version=1,
        symbol=symbol,
        canonical_symbol=canonical,
        exchange=exchange,
        status="running",
        attempt_count=1,
        slot_id=execution.slot_id,
        provider=execution.provider,
        dataset_key=execution.dataset_key,
        work_item=symbol,
        target_data_date=item.trade_date,
        checkpoint_before=None,
        prepared_request=owner.journal.prepared(job_key) if owner else None,
    )


class _CohortRunner:
    """Routes Full release cohorts to the member runner; others stay legacy."""

    provider: str
    _members: MemberHistoricalRunner | None = None

    def _cohort(self, item: HistoricalWorkItem) -> HistoricalRunResult | None:
        if not full_release_cohort(item):
            return None
        if self._members is None:
            self._members = MemberHistoricalRunner.from_env(self.provider)
        return self._members.run(item)

    def close(self) -> None:
        if self._members is not None:
            self._members.close()
            self._members = None


class TwelveDataHistoricalRunner(_CohortRunner):
    provider = "twelve_data"

    @classmethod
    def from_env(cls) -> "TwelveDataHistoricalRunner":
        return cls()

    def run(self, item: HistoricalWorkItem) -> UUID | HistoricalRunResult:
        require_owner(item.provider)
        if item.provider != "twelve_data":
            raise HistoricalRuntimeError("provider mismatch")
        cohort = self._cohort(item)
        if cohort is not None:
            return cohort
        execution = historical_execution(item.provider, item.dataset_key)
        universe = load_symbol_universe(execution.universe_file)
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
                execution=execution,
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
                    execution,
                    _job(execution, item, member.symbol, member.canonical_symbol, member.exchange),
                )
                if not result.succeeded or result.run_id is None:
                    raise HistoricalRuntimeError("twelve_data terminal delivery failed")
                last = result.run_id
        if last is None:
            raise HistoricalRuntimeError("twelve_data produced no Source receipt")
        return last


class FinLabHistoricalRunner(_CohortRunner):
    provider = "finlab"

    @classmethod
    def from_env(cls) -> "FinLabHistoricalRunner":
        return cls()

    def run(self, item: HistoricalWorkItem) -> UUID | HistoricalRunResult:
        require_owner(item.provider)
        if item.provider != "finlab":
            raise HistoricalRuntimeError("provider mismatch")
        cohort = self._cohort(item)
        if cohort is not None:
            return cohort
        execution = historical_execution(item.provider, item.dataset_key)
        universe = load_finlab_universe(execution.universe_file)
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
                execution=execution,
                universe=universe,
                provider=FinLabSdkGateway.from_env(),
                source=source,
                registry=registry,
                raw_store=R2RawPayloadStore(RawStorageConfig.from_env()),
                state=owner.journal if owner else None,  # type: ignore[arg-type]
            )
            result = _execute(
                executor,
                execution,
                _job(execution, item, universe.dataset_key, universe.dataset_key, universe.market),
            )
        if not result.succeeded or result.run_id is None:
            raise HistoricalRuntimeError("finlab terminal delivery failed")
        return result.run_id
