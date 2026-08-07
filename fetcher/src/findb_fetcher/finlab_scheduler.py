"""Durable, dataset-level FinLab pilot scheduler execution.

FinLab exposes five logical OHLCV datasets rather than one per-symbol API.  A
single scheduler work item therefore acquires the complete reviewed bundle for
one target date, persists its deterministic raw snapshot, and delivers one
``market_eod.v1`` full snapshot containing the two pilot rows.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from findb_fetcher.client import (
    PreparedDelivery,
    SourceAPIClient,
    SourceAPIDeadlineExceeded,
    SourceAPIProtocolError,
    SourceAPIResponseError,
    SourceAPITransportError,
)
from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.finlab_universe import FinLabPilotUniverse
from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabPayloadError,
    FinLabSdkError,
    FinLabTargetNotReadyError,
    fetch_finlab_dataset_bundle,
    prepare_market_eod_delivery,
)
from findb_fetcher.raw_storage import (
    RawPayloadStore,
    RawStorageUploadError,
    require_raw_provenance,
)
from findb_fetcher.schedule import ScheduleConfig
from findb_fetcher.scheduler_state import ScheduledJob, SchedulerState
from findb_fetcher.twelve_data_scheduler import (
    JobExecution,
    SchedulerRun,
    SchedulerService,
)

_TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})


class FinLabSchedulerError(RuntimeError):
    """A FinLab scheduler operation failed without exposing provider details."""


class FinLabScheduledExecutor:
    """Acquire, persist, prepare, deliver, and verify one FinLab bundle job."""

    def __init__(
        self,
        *,
        schedule: ScheduleConfig,
        universe: FinLabPilotUniverse,
        provider: object,
        source: SourceAPIClient,
        registry: ContractRegistry,
        raw_store: RawPayloadStore,
        state: SchedulerState | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if raw_store is None:
            raise ValueError("FinLab scheduled Source delivery requires raw object storage")
        if schedule.provider != "finlab" or schedule.dataset_key != universe.dataset_key:
            raise ValueError("FinLab schedule and universe identities do not match")
        if schedule.market != universe.market:
            raise ValueError("FinLab schedule and universe markets do not match")
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
        """Execute one dataset/date work item with deterministic replay semantics."""

        deadline = self._monotonic() + self._schedule.wait_timeout_seconds
        target_date = job.target_data_date or job.scheduled_date
        try:
            request = job.prepared_request
            if request is None:
                bundle = fetch_finlab_dataset_bundle(
                    self._provider,  # type: ignore[arg-type]
                    config=self._universe.dataset_config,
                    target_date=target_date,
                )
                request = prepare_market_eod_delivery(
                    bundle,
                    fetched_at=now,
                    raw_store=self._raw_store,
                    delivery=_delivery_metadata(self._schedule, job),
                )
                self._validate_request(request, target_date=target_date)
                self._registry.validate("market_eod", 1, request)
                # Save the exact request before any Source client work.  A
                # process restart can therefore replay it without five SDK or
                # another R2 calls.
                if self._state is not None:
                    self._state.save_prepared_request(job, request, now=now)
            else:
                if not isinstance(request, dict):
                    raise FinLabPayloadError("prepared FinLab request is not an object")
                require_raw_provenance(request)
                self._validate_request(request, target_date=target_date)
                self._registry.validate("market_eod", 1, request)

            # ``request`` is narrowed above for both paths; source.prepare is
            # intentionally the first network-facing operation after the
            # durable raw snapshot and prepared-request checkpoint.
            require_raw_provenance(request)
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

            status = getattr(run, "status", None)
            if status != "completed":
                outcome = (
                    "terminal_partial" if status == "completed_with_errors" else "terminal_failed"
                )
                return JobExecution(
                    outcome=outcome,
                    succeeded=False,
                    source_status=status if isinstance(status, str) else None,
                    attempt_id=receipt.attempt_id,
                    run_id=run.run_id,
                )

            total_records = getattr(run, "total_records", None)
            success_records = getattr(run, "success_records", None)
            failed_records = getattr(run, "failed_records", None)
            expected = self._universe.expected_record_count
            if (
                type(total_records) is not int
                or type(success_records) is not int
                or type(failed_records) is not int
                or total_records != expected
                or success_records != total_records
                or failed_records != 0
            ):
                return JobExecution(
                    outcome="terminal_count_mismatch",
                    succeeded=False,
                    source_status="completed",
                    attempt_id=receipt.attempt_id,
                    run_id=run.run_id,
                )

            return JobExecution(
                outcome="completed",
                succeeded=True,
                record_count=expected,
                checkpoint_after=target_date,
                source_status="completed",
                attempt_id=receipt.attempt_id,
                run_id=run.run_id,
            )
        except FinLabTargetNotReadyError:
            return JobExecution(
                outcome="target_not_ready",
                succeeded=False,
                retryable=True,
            )
        except FinLabSdkError as exc:
            # Some injected gateways use the base error for a publication
            # miss.  The message is already sanitized by the provider seam.
            if _looks_like_target_not_ready(exc):
                return JobExecution(
                    outcome="target_not_ready",
                    succeeded=False,
                    retryable=True,
                )
            return JobExecution(
                outcome="provider_failed",
                succeeded=False,
                retryable=True,
            )
        except FinLabConfigError:
            return JobExecution(outcome="config_failed", succeeded=False)
        except FinLabPayloadError:
            return JobExecution(outcome="bundle_invalid", succeeded=False)
        except RawStorageUploadError as exc:
            return JobExecution(
                outcome="raw_upload_failed",
                succeeded=False,
                retryable=exc.retryable,
            )
        except ContractError:
            return JobExecution(outcome="contract_invalid", succeeded=False)
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
        except (TypeError, ValueError):
            return JobExecution(outcome="request_invalid", succeeded=False)
        except Exception:
            # Provider libraries and SDKs can carry arbitrary exception text;
            # never let it reach durable state or CLI output.
            return JobExecution(outcome="execution_failed", succeeded=False)

    def _validate_request(self, request: dict[str, Any], *, target_date: date) -> None:
        if (
            request.get("dataset_key") != self._universe.dataset_key
            or request.get("schema_id") != "market_eod"
            or request.get("schema_version") != 1
            or request.get("source") != "finlab"
        ):
            raise FinLabPayloadError("FinLab request identity is invalid")
        payload = request.get("payload")
        batch = payload.get("batch") if isinstance(payload, dict) else None
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(batch, dict) or not isinstance(rows, list):
            raise FinLabPayloadError("FinLab request payload is invalid")
        if (
            batch.get("data_date") != target_date.isoformat()
            or batch.get("delivery_mode") != "full_snapshot"
            or batch.get("declared_record_count") != self._universe.expected_record_count
            or len(rows) != self._universe.expected_record_count
        ):
            raise FinLabPayloadError("FinLab request is not the complete target-date snapshot")
        expected = {item.canonical_symbol: item.source_symbol for item in self._universe.symbols}
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise FinLabPayloadError("FinLab request row is invalid")
            symbol = row.get("symbol")
            if (
                not isinstance(symbol, str)
                or symbol not in expected
                or symbol in seen
                or row.get("source_symbol") != expected[symbol]
                or row.get("trade_date") != target_date.isoformat()
                or row.get("currency") != self._universe.currency
            ):
                raise FinLabPayloadError("FinLab request row is outside reviewed scope")
            seen.add(symbol)
        if seen != set(expected):
            raise FinLabPayloadError("FinLab request does not contain all reviewed rows")


class FinLabSchedulerService:
    """Small convenience wrapper over the shared durable ``SchedulerService``."""

    def __init__(
        self,
        *,
        schedule: ScheduleConfig,
        universe: FinLabPilotUniverse,
        state: SchedulerState,
        provider: object,
        source: SourceAPIClient,
        registry: ContractRegistry,
        raw_store: RawPayloadStore,
        calendar: object | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._state_universe = universe.as_scheduler_universe()
        executor = FinLabScheduledExecutor(
            schedule=schedule,
            universe=universe,
            provider=provider,
            source=source,
            registry=registry,
            raw_store=raw_store,
            state=state,
            monotonic=monotonic,
            sleep=sleep,
        )
        self._service = SchedulerService(
            schedule=schedule,
            universe=self._state_universe,
            state=state,
            executor=executor,  # type: ignore[arg-type]
            calendar=calendar,  # type: ignore[arg-type]
        )

    def run_once(self, *, now: datetime) -> SchedulerRun:
        return self._service.run_once(now=now)


# Descriptive aliases for callers that prefer executor/scheduler naming.
FinLabSchedulerExecutor = FinLabScheduledExecutor
FinLabScheduler = FinLabSchedulerService


def _delivery_metadata(schedule: ScheduleConfig, job: ScheduledJob) -> dict[str, str]:
    target_date = job.target_data_date or job.scheduled_date
    scheduled = datetime.combine(
        job.scheduled_date,
        schedule.scheduled_local_time,
        tzinfo=ZoneInfo(schedule.timezone_name),
    )
    return {
        "slot_id": schedule.slot_id,
        "scheduled_for": scheduled.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target_data_date": target_date.isoformat(),
        "work_item_id": job.work_item or job.symbol,
    }


def _wait_for_terminal(
    source: SourceAPIClient,
    run_id: UUID,
    expected: PreparedDelivery,
    *,
    deadline: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> Any:
    while True:
        run = source.get_run_status(
            run_id,
            expected=expected,
            deadline=deadline,
            monotonic=monotonic,
        )
        if getattr(run, "status", None) in _TERMINAL_STATUSES:
            return run
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SourceAPIDeadlineExceeded()
        sleep(min(poll_interval_seconds, remaining))


def _looks_like_target_not_ready(error: FinLabSdkError) -> bool:
    text = str(error).lower()
    return "target date" in text or "not published" in text or "not ready" in text
