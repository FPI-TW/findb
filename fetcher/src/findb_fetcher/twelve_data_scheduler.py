"""Durable scheduled execution for the governed Twelve Data universe."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from findb_fetcher.client import (
    PreparedDelivery,
    SourceAPIClient,
    SourceAPIDeadlineExceeded,
    SourceAPIProtocolError,
    SourceAPIResponseError,
    SourceAPITransportError,
)
from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataNoNewDataError,
    TwelveDataPayloadError,
    TwelveDataResponseError,
    build_market_eod_request,
)
from findb_fetcher.raw_storage import (
    RawPayloadStore,
    RawStorageUploadError,
    attach_raw_object,
    require_raw_provenance,
)
from findb_fetcher.schedule import ScheduleConfig, ScheduleError
from findb_fetcher.scheduler_state import ScheduledJob, SchedulerState
from findb_fetcher.universe import SymbolUniverse

_TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})


@dataclass(frozen=True, slots=True)
class JobExecution:
    outcome: str
    succeeded: bool
    retryable: bool = False
    record_count: int = 0
    checkpoint_after: date | None = None
    source_status: str | None = None
    attempt_id: UUID | None = None
    run_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ScheduledJobResult:
    symbol: str
    outcome: str
    state_status: str
    attempt_count: int
    record_count: int = 0
    checkpoint_after: date | None = None


@dataclass(frozen=True, slots=True)
class SchedulerRun:
    schedule_id: str
    scheduled_date: date
    enqueued: int
    claimed: int
    credits_used: int
    status_counts: dict[str, int]
    results: tuple[ScheduledJobResult, ...]

    @property
    def status(self) -> str:
        if self.status_counts.get("failed", 0):
            return "failed"
        if self.status_counts.get("retry_wait", 0) or self.status_counts.get("running", 0):
            return "retry_pending"
        if self.status_counts.get("pending", 0):
            return "pending"
        return "completed"


class TwelveDataScheduledExecutor:
    """Fetch one symbol, deliver it, and wait before allowing checkpoint progress."""

    def __init__(
        self,
        *,
        schedule: ScheduleConfig,
        universe: SymbolUniverse,
        provider: TwelveDataClient,
        source: SourceAPIClient,
        registry: ContractRegistry,
        raw_store: RawPayloadStore,
        state: SchedulerState | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if raw_store is None:
            raise ValueError("scheduled Source delivery requires raw object storage")
        self._schedule = schedule
        self._universe = universe
        self._provider = provider
        self._source = source
        self._registry = registry
        self._raw_store = raw_store
        self._state = state
        self._monotonic = monotonic
        self._sleep = sleep

    def execute(self, job: ScheduledJob, *, now: datetime) -> JobExecution:
        deadline = self._monotonic() + self._schedule.wait_timeout_seconds
        try:
            request = job.prepared_request
            if request is None:
                query: dict[str, Any]
                if job.checkpoint_before is None:
                    query = {"outputsize": self._schedule.outputsize}
                else:
                    start_date = job.checkpoint_before + timedelta(days=1)
                    if start_date > job.scheduled_date:
                        return JobExecution(
                            outcome="up_to_date",
                            succeeded=True,
                            checkpoint_after=job.checkpoint_before,
                        )
                    if (
                        job.scheduled_date - start_date
                    ).days > self._universe.limits.max_date_span_days:
                        return JobExecution(
                            outcome="checkpoint_gap_exceeded",
                            succeeded=False,
                        )
                    query = {
                        "start_date": start_date,
                        "end_date": job.scheduled_date,
                    }
                response = self._provider.fetch_daily(
                    job.symbol,
                    exchange=job.exchange,
                    **query,
                )
                raw_object = self._raw_store.persist(
                    response.raw_bytes,
                    dataset_key=self._universe.dataset_key,
                    source_symbol=job.symbol,
                )
                request = build_market_eod_request(
                    response,
                    dataset_key=self._universe.dataset_key,
                    fetched_at=now,
                    requested_symbol=job.symbol,
                    requested_exchange=job.exchange,
                    canonical_symbol=job.canonical_symbol,
                    allowed_instrument_types=(self._universe.instrument_type,),
                    after_trade_date=job.checkpoint_before,
                )
                attach_raw_object(request, raw_object)
            try:
                # New Fetcher delivery is stricter than the optional legacy v1 fields.
                require_raw_provenance(request)
            except RawStorageUploadError:
                return JobExecution(
                    outcome="raw_provenance_missing",
                    succeeded=False,
                )
            self._registry.validate("market_eod", 1, request)
            record_count = len(request["payload"]["data"])
            if record_count > self._universe.limits.max_records_per_symbol:
                return JobExecution(
                    outcome="record_limit_exceeded",
                    succeeded=False,
                )
            if job.prepared_request is None and self._state is not None:
                self._state.save_prepared_request(job, request, now=now)
            prepared = self._source.prepare(request)
            receipt = self._source.deliver(
                prepared,
                deadline=deadline,
                monotonic=self._monotonic,
            )
            run = _wait_for_terminal(
                self._source,
                receipt.run_id,
                prepared,
                deadline=deadline,
                poll_interval_seconds=min(
                    self._schedule.poll_interval_seconds,
                    self._schedule.wait_timeout_seconds,
                ),
                monotonic=self._monotonic,
                sleep=self._sleep,
            )
            if run.status != "completed":
                return JobExecution(
                    outcome="terminal_failure",
                    succeeded=False,
                    source_status=run.status,
                    attempt_id=receipt.attempt_id,
                    run_id=run.run_id,
                )
            checkpoint_after = _request_checkpoint(request)
            return JobExecution(
                outcome="completed",
                succeeded=True,
                record_count=record_count,
                checkpoint_after=checkpoint_after,
                source_status=run.status,
                attempt_id=receipt.attempt_id,
                run_id=run.run_id,
            )
        except TwelveDataNoNewDataError:
            return JobExecution(
                outcome="up_to_date",
                succeeded=True,
                checkpoint_after=job.checkpoint_before,
            )
        except TwelveDataResponseError as exc:
            retryable = (
                exc.is_rate_limited
                or exc.status_code is None
                or (exc.status_code is not None and exc.status_code >= 500)
            )
            return JobExecution(
                outcome="provider_rate_limited" if exc.is_rate_limited else "provider_failed",
                succeeded=False,
                retryable=retryable,
            )
        except (TwelveDataPayloadError, ContractError, ValueError):
            return JobExecution(outcome="mapping_failed", succeeded=False)
        except RawStorageUploadError as exc:
            return JobExecution(
                outcome="raw_upload_failed",
                succeeded=False,
                retryable=exc.retryable,
            )
        except SourceAPIDeadlineExceeded:
            return JobExecution(outcome="wait_timeout", succeeded=False, retryable=True)
        except SourceAPIResponseError as exc:
            return JobExecution(
                outcome="source_rate_limited" if exc.status_code == 429 else "source_rejected",
                succeeded=False,
                retryable=exc.status_code == 429 or exc.status_code >= 500,
            )
        except SourceAPITransportError:
            return JobExecution(outcome="delivery_failed", succeeded=False, retryable=True)
        except SourceAPIProtocolError:
            return JobExecution(outcome="source_protocol_failed", succeeded=False)


class SchedulerService:
    """Materialize one due schedule and durably record each symbol outcome."""

    def __init__(
        self,
        *,
        schedule: ScheduleConfig,
        universe: SymbolUniverse,
        state: SchedulerState,
        executor: TwelveDataScheduledExecutor,
    ) -> None:
        if schedule.outputsize > universe.limits.max_records_per_symbol:
            raise ScheduleError("schedule outputsize exceeds universe per-symbol record limit")
        self._schedule = schedule
        self._universe = universe
        self._state = state
        self._executor = executor

    def run_once(self, *, now: datetime) -> SchedulerRun:
        scheduled_date = self._schedule.latest_due_date(now)
        enqueued = self._state.enqueue_due(
            self._schedule,
            self._universe,
            scheduled_date,
            now=now,
        )
        jobs = self._state.claim_due(
            self._schedule,
            self._universe,
            now=now,
            limit=len(self._universe.symbols),
        )
        results: list[ScheduledJobResult] = []
        for job in jobs:
            execution = self._executor.execute(job, now=now)
            if execution.succeeded:
                self._state.complete(
                    job,
                    now=now,
                    outcome=execution.outcome,
                    record_count=execution.record_count,
                    checkpoint_after=execution.checkpoint_after,
                    attempt_id=execution.attempt_id,
                    run_id=execution.run_id,
                )
                state_status = "completed"
            else:
                state_status = self._state.record_failure(
                    job,
                    self._schedule,
                    now=now,
                    outcome=execution.outcome,
                    retryable=execution.retryable,
                    attempt_id=execution.attempt_id,
                    run_id=execution.run_id,
                )
            results.append(
                ScheduledJobResult(
                    symbol=job.symbol,
                    outcome=execution.outcome,
                    state_status=state_status,
                    attempt_count=job.attempt_count,
                    record_count=execution.record_count,
                    checkpoint_after=execution.checkpoint_after,
                )
            )
        return SchedulerRun(
            schedule_id=self._schedule.schedule_id,
            scheduled_date=scheduled_date,
            enqueued=enqueued,
            claimed=len(jobs),
            credits_used=sum(
                self._universe.credit_cost_per_symbol
                for job in jobs
                if job.prepared_request is None
            ),
            status_counts=self._state.status_counts(
                self._schedule.schedule_id,
                scheduled_date,
            ),
            results=tuple(results),
        )


def _request_checkpoint(request: dict[str, Any]) -> date:
    payload = request.get("payload")
    batch = payload.get("batch") if isinstance(payload, dict) else None
    if not isinstance(batch, dict):
        raise ValueError("validated request is missing batch metadata")
    raw = batch.get("coverage_end_date", batch.get("data_date"))
    if not isinstance(raw, str):
        raise ValueError("validated request is missing checkpoint date")
    return date.fromisoformat(raw)


def _wait_for_terminal(
    source: SourceAPIClient,
    run_id: UUID,
    expected: PreparedDelivery,
    *,
    deadline: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
):
    while True:
        run = source.get_run_status(
            run_id,
            expected=expected,
            deadline=deadline,
            monotonic=monotonic,
        )
        if run.status in _TERMINAL_STATUSES:
            return run
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SourceAPIDeadlineExceeded()
        sleep(min(poll_interval_seconds, remaining))
