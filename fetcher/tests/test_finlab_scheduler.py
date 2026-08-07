from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.finlab_scheduler import FinLabScheduledExecutor, FinLabSchedulerService
from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.market_calendar import CalendarDay
from findb_fetcher.providers.finlab import (
    FinLabDatasetTable,
    FinLabTargetNotReadyError,
)
from findb_fetcher.raw_storage import RawObject, RawStorageUploadError
from findb_fetcher.schedule import load_schedule_manifest
from findb_fetcher.scheduler_state import ScheduledJob, SchedulerState

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
SCHEDULE_PATH = CONFIG_DIR / "daily_scheduler.v2.json"
UNIVERSE_PATH = CONFIG_DIR / "finlab_tw_review_required.v1.json"
NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
TARGET_DATE = date(2026, 7, 29)
ATTEMPT_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a91")
RUN_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a92")


def _schedule():
    manifest = load_schedule_manifest(SCHEDULE_PATH)
    return next(feed for feed in manifest.feeds if feed.slot_id == "taiwan_market_window")


def _tables() -> dict[str, FinLabDatasetTable]:
    return {
        "open": FinLabDatasetTable((TARGET_DATE.isoformat(),), ("2330", "2317"), ((1000, 200),)),
        "high": FinLabDatasetTable((TARGET_DATE.isoformat(),), ("2330", "2317"), ((1010, 205),)),
        "low": FinLabDatasetTable((TARGET_DATE.isoformat(),), ("2330", "2317"), ((995, 198),)),
        "close": FinLabDatasetTable((TARGET_DATE.isoformat(),), ("2330", "2317"), ((1005, 202),)),
        "volume": FinLabDatasetTable(
            (TARGET_DATE.isoformat(),), ("2330", "2317"), ((123456, 654321),)
        ),
    }


class Gateway:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def fetch_dataset(self, dataset: str, *, target_date: date, symbols: tuple[str, ...]):
        self.calls.append(dataset)
        if self.failure:
            raise self.failure
        assert target_date == TARGET_DATE
        assert symbols == ("2330", "2317")
        return _tables()[
            next(
                field
                for field, name in {
                    "open": "price:開盤價",
                    "high": "price:最高價",
                    "low": "price:最低價",
                    "close": "price:收盤價",
                    "volume": "price:成交股數",
                }.items()
                if name == dataset
            )
        ]


class RawStore:
    def __init__(self, events: list[str], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure
        self.calls = 0

    def persist(self, raw_bytes: bytes, **_kwargs: object) -> RawObject:
        self.events.append("raw")
        self.calls += 1
        if self.failure:
            raise self.failure
        import hashlib

        return RawObject(
            "r2://0123456789abcdef0123456789abcdef/findb-fetcher-raw/finlab.json",
            hashlib.sha256(raw_bytes).hexdigest(),
            len(raw_bytes),
        )


class Source:
    def __init__(self, events: list[str], *, total: int = 2, success: int = 2, failed: int = 0):
        self.events = events
        self.total = total
        self.success = success
        self.failed = failed
        self.requests: list[dict[str, object]] = []

    def prepare(self, request: dict[str, object]) -> SimpleNamespace:
        self.events.append("prepare")
        self.requests.append(copy.deepcopy(request))
        return SimpleNamespace(
            body=b"prepared",
            idempotency_key=request["idempotency_key"],
            dataset_key=request["dataset_key"],
            schema_id="market_eod",
            schema_version=1,
        )

    def deliver(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        self.events.append("deliver")
        return SimpleNamespace(attempt_id=ATTEMPT_ID, run_id=RUN_ID, status="queued")

    def get_run_status(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        self.events.append("poll")
        return SimpleNamespace(
            run_id=RUN_ID,
            status="completed",
            total_records=self.total,
            success_records=self.success,
            failed_records=self.failed,
        )


class Calendar:
    def get_day(self, market: str, value: date) -> tuple[CalendarDay, int]:
        assert market == "TW"
        return CalendarDay(value, "open", True), 7


def _service(
    tmp_path: Path,
    *,
    gateway: Gateway | None = None,
    source: Source | None = None,
    raw_store: RawStore | None = None,
):
    universe = load_finlab_universe(UNIVERSE_PATH)
    events: list[str] = []
    gateway = gateway or Gateway()
    source = source or Source(events)
    raw_store = raw_store or RawStore(events)
    state = SchedulerState(tmp_path / "state.sqlite3")
    service = FinLabSchedulerService(
        schedule=_schedule(),
        universe=universe,
        state=state,
        provider=gateway,
        source=source,  # type: ignore[arg-type]
        registry=ContractRegistry(CONTRACTS_DIR),
        raw_store=raw_store,
        calendar=Calendar(),
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )
    return service, state, universe, gateway, source, raw_store, events


def test_dataset_bundle_is_one_work_item_and_raw_precedes_source(tmp_path: Path) -> None:
    service, state, universe, gateway, source, raw_store, events = _service(tmp_path)

    result = service.run_once(now=NOW)

    assert result.status == "completed"
    assert result.enqueued == result.claimed == 1
    assert gateway.calls == [
        "price:開盤價",
        "price:最高價",
        "price:最低價",
        "price:收盤價",
        "price:成交股數",
    ]
    assert events == ["raw", "prepare", "deliver", "poll"]
    assert source.requests[0]["payload"]["batch"]["delivery_mode"] == "full_snapshot"
    assert state.checkpoint(universe.as_scheduler_universe(), "tw_equity_eod") == TARGET_DATE
    assert raw_store.calls == 1


def test_target_not_ready_is_retryable_and_does_not_prepare(tmp_path: Path) -> None:
    service, _state, _universe, gateway, source, _raw, _events = _service(
        tmp_path,
        gateway=Gateway(FinLabTargetNotReadyError("not published")),
    )

    result = service.run_once(now=NOW)

    assert result.status == "retry_pending"
    assert result.results[0].outcome == "target_not_ready"
    assert result.results[0].state_status == "retry_wait"
    assert gateway.calls == ["price:開盤價"]
    assert source.requests == []


def test_terminal_count_gate_blocks_checkpoint(tmp_path: Path) -> None:
    events: list[str] = []
    service, state, universe, _gateway, source, _raw, _events = _service(
        tmp_path,
        source=Source(events, total=2, success=1, failed=1),
    )

    result = service.run_once(now=NOW)

    assert result.status == "failed"
    assert result.results[0].outcome == "terminal_count_mismatch"
    assert state.checkpoint(universe.as_scheduler_universe(), "tw_equity_eod") is None
    assert source.requests


def test_prepared_request_replay_skips_gateway_and_raw(tmp_path: Path) -> None:
    service, state, universe, gateway, source, raw_store, _events = _service(tmp_path)
    first = service.run_once(now=NOW)
    assert first.status == "completed"

    # Reuse the exact request snapshot to model a process restart after
    # save_prepared_request and before Source delivery.
    request = source.requests[0]
    job = ScheduledJob(
        job_key="b" * 64,
        schedule_id=_schedule().schedule_id,
        scheduled_date=TARGET_DATE,
        universe_id=universe.manifest_id,
        universe_version=1,
        symbol="tw_equity_eod",
        canonical_symbol="tw_equity_eod",
        exchange="TW",
        status="running",
        attempt_count=2,
        checkpoint_before=TARGET_DATE,
        prepared_request=request,
        slot_id="taiwan_market_window",
        provider="finlab",
        dataset_key="tw_equity_eod",
        work_item="tw_equity_eod",
        target_data_date=TARGET_DATE,
    )
    replay_source = Source([], total=2, success=2, failed=0)
    executor = FinLabScheduledExecutor(
        schedule=_schedule(),
        universe=universe,
        provider=Gateway(RuntimeError("must not fetch")),
        source=replay_source,  # type: ignore[arg-type]
        registry=ContractRegistry(CONTRACTS_DIR),
        raw_store=RawStore([], RuntimeError("must not upload")),
        state=None,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )

    execution = executor.execute(job, now=NOW)

    assert execution.succeeded
    assert gateway.calls  # original cycle fetched the five datasets
    assert raw_store.calls == 1
    assert replay_source.requests


def test_retryable_raw_failure_is_classified_without_source_call(tmp_path: Path) -> None:
    failure = RawStorageUploadError("temporary", retryable=True)
    service, _state, _universe, _gateway, source, _raw, _events = _service(
        tmp_path,
        raw_store=RawStore([], failure),
    )

    result = service.run_once(now=NOW)

    assert result.status == "retry_pending"
    assert result.results[0].outcome == "raw_upload_failed"
    assert source.requests == []
