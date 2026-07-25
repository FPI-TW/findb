from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from findb_fetcher.client import SourceAPIResponseError, SourceAPITransportError
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.providers.twelve_data import (
    TwelveDataResponse,
    TwelveDataResponseError,
    build_market_eod_request,
)
from findb_fetcher.raw_storage import RawObject, RawStorageUploadError, attach_raw_object
from findb_fetcher.schedule import load_schedule_config
from findb_fetcher.scheduler_state import ScheduledJob, SchedulerState
from findb_fetcher.twelve_data_scheduler import (
    JobExecution,
    SchedulerService,
    TwelveDataScheduledExecutor,
)
from findb_fetcher.universe import load_symbol_universe

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
SCHEDULE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks_daily.v1.json"
UNIVERSE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks.v1.json"
FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "twelve_data" / "aapl_1day_2024-01-02_2024-01-05.json"
)
NOW = datetime(2026, 7, 25, 2, 5, tzinfo=timezone.utc)
ATTEMPT_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a91")
RUN_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a92")


class FakeProvider:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fetch_daily(self, symbol: str, **kwargs: object) -> TwelveDataResponse:
        self.calls.append((symbol, kwargs))
        if self.failure:
            raise self.failure
        response = json.loads(FIXTURE_PATH.read_text())
        response["meta"]["symbol"] = symbol
        return TwelveDataResponse(response, raw_bytes=json.dumps(response).encode())


class FakeSource:
    def __init__(
        self,
        *,
        status: str = "completed",
        delivery_failure: Exception | None = None,
    ) -> None:
        self.status = status
        self.delivery_failure = delivery_failure
        self.requests: list[dict[str, Any]] = []

    def prepare(self, request: dict[str, Any]) -> SimpleNamespace:
        self.requests.append(deepcopy(request))
        return SimpleNamespace(
            dataset_key=request["dataset_key"],
            schema_id=request["schema_id"],
            schema_version=request["schema_version"],
        )

    def deliver(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        if self.delivery_failure:
            raise self.delivery_failure
        return SimpleNamespace(attempt_id=ATTEMPT_ID, run_id=RUN_ID, status="queued")

    def get_run_status(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            run_id=RUN_ID,
            status=self.status,
            total_records=2,
            success_records=2 if self.status == "completed" else 0,
            failed_records=0 if self.status == "completed" else 2,
        )


def _job(
    checkpoint: date | None = None,
    *,
    scheduled_date: date = date(2026, 7, 25),
) -> ScheduledJob:
    return ScheduledJob(
        job_key="a" * 64,
        schedule_id="twelve_data_us_common_stocks_daily_v1",
        scheduled_date=scheduled_date,
        universe_id="twelve_data_us_common_stocks_v1",
        universe_version=1,
        symbol="AAPL",
        canonical_symbol="AAPL",
        exchange="NASDAQ",
        status="running",
        attempt_count=1,
        checkpoint_before=checkpoint,
        prepared_request=None,
    )


def _executor(
    contracts_dir: Path,
    *,
    provider: FakeProvider | None = None,
    source: FakeSource | None = None,
    raw_store: object | None = None,
    state: SchedulerState | None = None,
) -> TwelveDataScheduledExecutor:
    return TwelveDataScheduledExecutor(
        schedule=load_schedule_config(SCHEDULE_PATH),
        universe=load_symbol_universe(UNIVERSE_PATH),
        provider=provider or FakeProvider(),  # type: ignore[arg-type]
        source=source or FakeSource(),  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        raw_store=raw_store or RecordingRawStore([]),  # type: ignore[arg-type]
        state=state,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )


class RecordingRawStore:
    def __init__(
        self,
        events: list[str],
        failure: RawStorageUploadError | None = None,
    ) -> None:
        self.events = events
        self.failure = failure
        self.calls = 0

    def persist(self, raw_bytes: bytes, **_kwargs: object) -> RawObject:
        self.events.append("upload")
        self.calls += 1
        assert raw_bytes.startswith(b"{")
        if self.failure:
            raise self.failure
        return RawObject(
            ref=f"r2://{'a' * 32}/findb-fetcher-raw/raw/a.json",
            sha256="a" * 64,
            size_bytes=len(raw_bytes),
        )


class RawProvider(FakeProvider):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def fetch_daily(self, symbol: str, **kwargs: object) -> TwelveDataResponse:
        self.events.append("fetch")
        payload = super().fetch_daily(symbol, **kwargs)
        return payload


def test_scheduler_executor_rejects_missing_raw_store(contracts_dir: Path) -> None:
    with pytest.raises(ValueError, match="raw object storage"):
        TwelveDataScheduledExecutor(
            schedule=load_schedule_config(SCHEDULE_PATH),
            universe=load_symbol_universe(UNIVERSE_PATH),
            provider=FakeProvider(),  # type: ignore[arg-type]
            source=FakeSource(),  # type: ignore[arg-type]
            registry=ContractRegistry(contracts_dir),
            raw_store=None,  # type: ignore[arg-type]
        )


class OrderedSource(FakeSource):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def prepare(self, request: dict[str, Any]) -> SimpleNamespace:
        self.events.append("prepare")
        assert request["payload"]["batch"]["source_raw_ref"].startswith("r2://")
        return super().prepare(request)

    def deliver(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        self.events.append("deliver")
        return super().deliver(*_args, **_kwargs)


def test_scheduler_uploads_before_prepare_and_delivery(contracts_dir: Path) -> None:
    events: list[str] = []
    source = OrderedSource(events)
    result = _executor(
        contracts_dir,
        provider=RawProvider(events),
        source=source,
        raw_store=RecordingRawStore(events),
    ).execute(_job(), now=NOW)

    assert result.succeeded
    assert events == ["fetch", "upload", "prepare", "deliver"]
    batch = source.requests[0]["payload"]["batch"]
    assert batch["source_raw_ref"] == (f"r2://{'a' * 32}/findb-fetcher-raw/raw/a.json")
    assert batch["source_raw_sha256"] == "a" * 64


@pytest.mark.parametrize("retryable", [False, True])
def test_scheduler_raw_upload_failure_never_delivers_or_advances_checkpoint(
    contracts_dir: Path,
    retryable: bool,
) -> None:
    events: list[str] = []
    source = OrderedSource(events)
    result = _executor(
        contracts_dir,
        provider=RawProvider(events),
        source=source,
        raw_store=RecordingRawStore(
            events,
            RawStorageUploadError("safe failure", retryable=retryable),
        ),
    ).execute(_job(), now=NOW)

    assert result.outcome == "raw_upload_failed"
    assert result.retryable is retryable
    assert result.checkpoint_after is None
    assert source.requests == []
    assert events == ["fetch", "upload"]


def test_scheduler_prepared_retry_does_not_refetch_or_reupload(
    contracts_dir: Path,
) -> None:
    request = build_market_eod_request(
        json.loads(FIXTURE_PATH.read_text()),
        dataset_key="us_equity_eod",
        fetched_at=NOW,
        requested_symbol="AAPL",
    )
    attach_raw_object(
        request,
        RawObject(
            ref=f"r2://{'a' * 32}/findb-fetcher-raw/raw/a.json",
            sha256="a" * 64,
            size_bytes=1,
        ),
    )
    events: list[str] = []
    provider = RawProvider(events)
    raw_store = RecordingRawStore(events)
    source = OrderedSource(events)

    result = _executor(
        contracts_dir,
        provider=provider,
        source=source,
        raw_store=raw_store,
    ).execute(replace(_job(), prepared_request=request), now=NOW)

    assert result.succeeded
    assert provider.calls == []
    assert raw_store.calls == 0
    assert events == ["prepare", "deliver"]


def test_scheduler_rejects_legacy_or_partial_prepared_raw_state(
    contracts_dir: Path,
) -> None:
    request = {
        "payload": {
            "batch": {
                "source_raw_ref": f"r2://{'a' * 32}/findb-fetcher-raw/raw/a.json",
            }
        }
    }
    events: list[str] = []

    result = _executor(
        contracts_dir,
        raw_store=RecordingRawStore(events),
    ).execute(replace(_job(), prepared_request=request), now=NOW)

    assert result.outcome == "raw_provenance_missing"
    assert not result.retryable
    assert events == []


def test_executor_delivers_only_rows_after_checkpoint(contracts_dir: Path) -> None:
    provider = FakeProvider()
    source = FakeSource()

    result = _executor(contracts_dir, provider=provider, source=source).execute(
        _job(date(2024, 1, 3), scheduled_date=date(2024, 1, 5)),
        now=NOW,
    )

    assert result.outcome == "completed"
    assert result.succeeded is True
    assert result.record_count == 2
    assert result.checkpoint_after == date(2024, 1, 5)
    assert result.attempt_id == ATTEMPT_ID
    assert provider.calls == [
        (
            "AAPL",
            {
                "start_date": date(2024, 1, 4),
                "end_date": date(2024, 1, 5),
                "exchange": "NASDAQ",
            },
        )
    ]
    assert [row["trade_date"] for row in source.requests[0]["payload"]["data"]] == [
        "2024-01-04",
        "2024-01-05",
    ]


def test_executor_marks_up_to_date_without_source_delivery(contracts_dir: Path) -> None:
    source = FakeSource()

    result = _executor(contracts_dir, source=source).execute(
        _job(date(2024, 1, 5), scheduled_date=date(2024, 1, 5)),
        now=NOW,
    )

    assert result == JobExecution(
        outcome="up_to_date",
        succeeded=True,
        checkpoint_after=date(2024, 1, 5),
    )
    assert source.requests == []


def test_executor_bootstrap_is_bounded_and_large_checkpoint_gap_is_rejected(
    contracts_dir: Path,
) -> None:
    provider = FakeProvider()
    executor = _executor(contracts_dir, provider=provider)

    bootstrap = executor.execute(_job(), now=NOW)
    gap = executor.execute(_job(date(2024, 1, 3)), now=NOW)

    assert bootstrap.succeeded is True
    assert provider.calls == [("AAPL", {"outputsize": 20, "exchange": "NASDAQ"})]
    assert gap.outcome == "checkpoint_gap_exceeded"
    assert gap.succeeded is False


@pytest.mark.parametrize("status", ["failed", "completed_with_errors"])
def test_terminal_normalization_failure_never_advances_checkpoint(
    contracts_dir: Path,
    status: str,
) -> None:
    result = _executor(contracts_dir, source=FakeSource(status=status)).execute(
        _job(date(2024, 1, 3), scheduled_date=date(2024, 1, 5)),
        now=NOW,
    )

    assert result.outcome == "terminal_failure"
    assert result.succeeded is False
    assert result.retryable is False
    assert result.checkpoint_after is None


@pytest.mark.parametrize(
    ("failure", "outcome", "retryable"),
    [
        (
            TwelveDataResponseError("rate limited", status_code=429, provider_code=429),
            "provider_rate_limited",
            True,
        ),
        (TwelveDataResponseError("bad request", status_code=400), "provider_failed", False),
        (SourceAPIResponseError(429), "source_rate_limited", True),
        (SourceAPIResponseError(403), "source_rejected", False),
    ],
)
def test_executor_classifies_persistent_retry_safety(
    contracts_dir: Path,
    failure: Exception,
    outcome: str,
    retryable: bool,
) -> None:
    if isinstance(failure, TwelveDataResponseError):
        executor = _executor(contracts_dir, provider=FakeProvider(failure))
    else:
        executor = _executor(
            contracts_dir,
            source=FakeSource(delivery_failure=failure),
        )

    result = executor.execute(_job(), now=NOW)

    assert result.outcome == outcome
    assert result.retryable is retryable
    assert result.succeeded is False


def test_service_persists_success_and_retry_across_cycles(
    tmp_path: Path,
) -> None:
    schedule = load_schedule_config(SCHEDULE_PATH)
    universe = load_symbol_universe(UNIVERSE_PATH)
    state = SchedulerState(tmp_path / "state.sqlite3")

    class ScriptedExecutor:
        attempts: dict[str, int] = {}

        def execute(self, job: ScheduledJob, *, now: datetime) -> JobExecution:
            del now
            self.attempts[job.symbol] = self.attempts.get(job.symbol, 0) + 1
            if job.symbol == "MSFT" and self.attempts[job.symbol] == 1:
                return JobExecution(
                    outcome="provider_rate_limited",
                    succeeded=False,
                    retryable=True,
                )
            return JobExecution(
                outcome="completed",
                succeeded=True,
                record_count=1,
                checkpoint_after=date(2026, 7, 24),
            )

    executor = ScriptedExecutor()
    service = SchedulerService(
        schedule=schedule,
        universe=universe,
        state=state,
        executor=executor,  # type: ignore[arg-type]
    )

    first = service.run_once(now=NOW)
    assert first.status == "retry_pending"
    assert first.enqueued == 3
    assert first.claimed == 3
    assert first.credits_used == 3
    assert first.status_counts == {"completed": 2, "retry_wait": 1}
    assert state.checkpoint(universe, "AAPL") == date(2026, 7, 24)
    assert state.checkpoint(universe, "MSFT") is None

    before_retry = service.run_once(now=NOW + timedelta(seconds=30))
    assert before_retry.claimed == 0
    assert before_retry.status == "retry_pending"

    after_retry = service.run_once(now=NOW + timedelta(seconds=60))
    assert after_retry.claimed == 1
    assert after_retry.status == "completed"
    assert after_retry.results[0].attempt_count == 2
    assert state.checkpoint(universe, "MSFT") == date(2026, 7, 24)


def test_delivery_retry_reuses_persisted_request_without_refetching(
    tmp_path: Path,
    contracts_dir: Path,
) -> None:
    schedule = load_schedule_config(SCHEDULE_PATH)
    universe = load_symbol_universe(UNIVERSE_PATH)
    state = SchedulerState(tmp_path / "state.sqlite3")
    state.enqueue_due(schedule, universe, date(2024, 1, 5), now=NOW)
    first_job = state.claim_due(schedule, universe, now=NOW, limit=1)[0]
    provider = FakeProvider()
    source = FakeSource(delivery_failure=SourceAPITransportError("offline"))
    executor = TwelveDataScheduledExecutor(
        schedule=schedule,
        universe=universe,
        provider=provider,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        raw_store=RecordingRawStore([]),
        state=state,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )

    first = executor.execute(first_job, now=NOW)
    assert first.outcome == "delivery_failed"
    assert (
        state.record_failure(
            first_job,
            schedule,
            now=NOW,
            outcome=first.outcome,
            retryable=first.retryable,
        )
        == "retry_wait"
    )
    retry_job = state.claim_due(
        schedule,
        universe,
        now=NOW + timedelta(seconds=60),
        limit=1,
    )[0]
    assert retry_job.prepared_request is not None

    source.delivery_failure = None
    provider.failure = AssertionError("delivery retry must not refetch provider data")
    second = executor.execute(retry_job, now=NOW + timedelta(seconds=60))

    assert second.outcome == "completed"
    assert len(provider.calls) == 1
    assert source.requests[0] == source.requests[1]
