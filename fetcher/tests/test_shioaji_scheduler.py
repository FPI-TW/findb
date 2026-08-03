from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from findb_fetcher import shioaji_scheduler
from findb_fetcher.client import PreparedDelivery
from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot, ShioajiSdkError
from findb_fetcher.shioaji_scheduler import (
    EXPECTED,
    PILOT_UNIVERSE_ID,
    ProductionCoordinator,
    ProductionManifestError,
    ProductionStateError,
    SchedulerRun,
    ShioajiProductionScheduler,
    load_manifest,
    open_production_state,
    validate_production_state_path,
)
from findb_fetcher.shioaji_staging import Result
from findb_fetcher.shioaji_staging_state import ShioajiStagingState

TAIPEI = ZoneInfo("Asia/Taipei")
MANIFEST = Path(__file__).resolve().parents[1] / "configs" / "shioaji_tw_pilot.v1.json"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        ("true", True),
        ("TRUE", False),
        ("true ", False),
        ("YES", False),
        ("1", False),
        ("false", False),
        ("0", False),
        ("invalid", False),
    ],
)
def test_production_scheduler_forces_simulation_mode(
    raw: str | None,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if raw is None:
        monkeypatch.delenv("SHIOAJI_SIMULATION", raising=False)
    else:
        monkeypatch.setenv("SHIOAJI_SIMULATION", raw)
    assert shioaji_scheduler._simulation_is_true() is expected


def test_production_runtime_rejects_live_trading_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHIOAJI_SIMULATION", "false")
    with pytest.raises(
        shioaji_scheduler.ProductionRuntimeError,
        match="SHIOAJI_SIMULATION=true is required",
    ):
        shioaji_scheduler.validate_production_runtime()


def test_exact_production_manifest_and_no_staging_identity(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST)
    assert manifest["universe_id"] == PILOT_UNIVERSE_ID
    assert manifest["sequences"] == list(EXPECTED)
    changed = json.loads(MANIFEST.read_text())
    changed["universe_id"] = "shioaji_tw_staging_v1"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(changed))
    with pytest.raises(ProductionManifestError):
        load_manifest(bad)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"version":1,"version":1}')
    with pytest.raises(ProductionManifestError):
        load_manifest(duplicate)


class _Calendar:
    def __init__(self, status: str = "open") -> None:
        self.status = status
        self.calls: list[tuple[str, date]] = []

    def get_day(self, market: str, value: date) -> tuple[object, int]:
        self.calls.append((market, value))
        return SimpleNamespace(day_status=self.status, is_open=self.status == "open"), 7


class _Coordinator:
    def __init__(self, result: Result | None = None) -> None:
        self.calls: list[tuple[date, bool]] = []
        self.result = result or Result("completed", "source", count=1, symbol="2330")

    def run(self, target: date, *, deliver: bool) -> list[Result]:
        self.calls.append((target, deliver))
        return [self.result]


def _scheduler(calendar: _Calendar, coordinator: _Coordinator) -> ShioajiProductionScheduler:
    return ShioajiProductionScheduler(
        state=SimpleNamespace(mark_cutoff=lambda _daily_id: 0),  # type: ignore[arg-type]
        manifest=load_manifest(MANIFEST),
        calendar=calendar,
        coordinator_factory=lambda _now: coordinator,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("hour", "minute", "status", "reason"),
    [
        (14, 29, "skipped", "not_due"),
        (14, 30, "completed", None),
        (16, 59, "completed", None),
        (17, 0, "skipped", "cutoff_reached"),
    ],
)
def test_timing_boundaries_and_deliver_gate(
    hour: int,
    minute: int,
    status: str,
    reason: str | None,
) -> None:
    calendar = _Calendar()
    coordinator = _Coordinator()
    result = _scheduler(calendar, coordinator).run_once(
        now=datetime(2026, 8, 3, hour, minute, tzinfo=TAIPEI)
    )
    assert result.status == status and result.skip_reason == reason
    if reason is None:
        assert coordinator.calls == [(date(2026, 8, 3), True)]
    else:
        assert coordinator.calls == []


def test_calendar_skip_and_no_prior_day_catch_up() -> None:
    calendar = _Calendar("closed")
    coordinator = _Coordinator()
    scheduler = _scheduler(calendar, coordinator)
    closed = scheduler.run_once(now=datetime(2026, 8, 3, 14, 30, tzinfo=TAIPEI))
    assert closed.skip_reason == "calendar_closed"
    assert coordinator.calls == []

    calendar.status = "open"
    next_day = scheduler.run_once(now=datetime(2026, 8, 4, 14, 30, tzinfo=TAIPEI))
    assert next_day.target_date == date(2026, 8, 4)
    assert coordinator.calls == [(date(2026, 8, 4), True)]
    assert calendar.calls == [("TW", date(2026, 8, 3)), ("TW", date(2026, 8, 4))]


def test_production_state_rejects_staging_sqlite_without_migration(tmp_path: Path) -> None:
    staging = ShioajiStagingState(tmp_path / "staging.sqlite3")
    staging.ensure(
        "d",
        "2026-08-03",
        "shioaji_tw_staging_v1",
        "u",
        [dict(EXPECTED[0])],
        "2026-08-03",
    )
    staging.close()
    with pytest.raises(ProductionStateError):
        validate_production_state_path(tmp_path / "staging.sqlite3", read_only=True)
    with pytest.raises(ProductionStateError):
        open_production_state(tmp_path / "staging.sqlite3")


def test_source_count_gate_requires_exact_prepared_rows() -> None:
    class Source:
        def __init__(self, value: object) -> None:
            self.value = value

        def get_run_status(self, *_args: object, **_kwargs: object) -> object:
            return self.value

    prepared = PreparedDelivery(
        json.dumps({"payload": {"data": [{"symbol": "2330"}, {"symbol": "2330"}]}}).encode(),
        "i",
        "d",
        "market_minute",
        1,
    )
    coordinator = object.__new__(ProductionCoordinator)
    coordinator.source = Source(
        SimpleNamespace(
            status="completed",
            total_records=2,
            success_records=2,
            failed_records=0,
        )
    )
    coordinator.monotonic = lambda: 0.0
    coordinator.sleep = lambda _seconds: None
    assert coordinator._wait_terminal("run", prepared) == "completed"

    coordinator.source = Source(
        SimpleNamespace(
            status="completed",
            total_records=3,
            success_records=2,
            failed_records=1,
        )
    )
    assert coordinator._wait_terminal("run", prepared) == "SOURCE_COUNT_MISMATCH"


def test_scheduler_run_is_bounded_and_result_is_explicit() -> None:
    result = SchedulerRun(date(2026, 8, 3), "failed", (Result("x", "state"),))
    assert result.status == "failed"


def test_production_retry_exhaustion_is_terminal_on_the_last_attempt(
    tmp_path: Path,
) -> None:
    class FailingGateway:
        calls = 0

        def fetch_kbars(self, _symbol: str, _target: date) -> ShioajiKbarsSnapshot:
            self.calls += 1
            raise ShioajiSdkError("provider detail must not escape", code="ACQUISITION")

    gateway = FailingGateway()
    state = ShioajiStagingState(tmp_path / "production-retries.sqlite3")
    coordinator = ProductionCoordinator(
        state,
        load_manifest(MANIFEST),
        gateway,
        now=lambda: datetime(2026, 8, 3, 16, 59, tzinfo=TAIPEI),
    )

    assert all(item.retryable for item in coordinator.run(date(2026, 8, 3), deliver=True))
    assert all(item.retryable for item in coordinator.run(date(2026, 8, 3), deliver=True))
    exhausted = coordinator.run(date(2026, 8, 3), deliver=True)

    assert len(exhausted) == len(EXPECTED)
    assert {item.code for item in exhausted} == {"ACQUISITION_ATTEMPTS_EXHAUSTED"}
    assert not any(item.retryable for item in exhausted)
    assert gateway.calls == 3 * len(EXPECTED)
    replayed = coordinator.run(date(2026, 8, 3), deliver=True)
    assert {item.code for item in replayed} == {"ACQUISITION_ATTEMPTS_EXHAUSTED"}
    assert gateway.calls == 3 * len(EXPECTED)
    state.close()


def test_cutoff_terminalizes_same_day_retry_pending_state(tmp_path: Path) -> None:
    class FailingGateway:
        calls = 0

        def fetch_kbars(self, _symbol: str, _target: date) -> ShioajiKbarsSnapshot:
            self.calls += 1
            raise ShioajiSdkError("provider detail must not escape", code="ACQUISITION")

    gateway = FailingGateway()
    state = ShioajiStagingState(tmp_path / "cutoff.sqlite3")
    manifest = load_manifest(MANIFEST)
    coordinator = ProductionCoordinator(
        state,
        manifest,
        gateway,
        now=lambda: datetime(2026, 8, 3, 16, 59, tzinfo=TAIPEI),
    )
    scheduler = ShioajiProductionScheduler(
        state=state,
        manifest=manifest,
        calendar=_Calendar(),
        coordinator_factory=lambda _now: coordinator,
    )

    pending = scheduler.run_once(now=datetime(2026, 8, 3, 16, 59, tzinfo=TAIPEI))
    assert pending.status == "retry_pending"
    assert gateway.calls == len(EXPECTED)
    cutoff = scheduler.run_once(now=datetime(2026, 8, 3, 17, 0, tzinfo=TAIPEI))

    assert cutoff.status == "skipped" and cutoff.skip_reason == "cutoff_reached"
    daily_id = f"{PILOT_UNIVERSE_ID}:2026-08-03"
    assert {state.get_terminal(daily_id, str(item["symbol"])) for item in EXPECTED} == {
        ("CUTOFF_REACHED", None)
    }
    assert gateway.calls == len(EXPECTED)
    state.close()
