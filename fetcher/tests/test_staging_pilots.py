"""Fixture functional acceptance; live credentials/pipeline need separate evidence."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tomllib
from datetime import date, datetime, timezone
from datetime import time as clock_time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.pilot_catalog import PILOT_RUNTIMES, SUPPORTED_PILOTS, validate_staging_pilots
from findb_fetcher.raw_storage import RawObject
from findb_fetcher.shioaji_scheduler import (
    load_manifest,
    open_production_state,
    validate_production_state_path,
)
from findb_fetcher.taifex_pilot import PilotState, latest_completed_day, select_near_month

CONFIGS = Path(__file__).resolve().parents[1] / "configs"


@pytest.mark.parametrize("provider", list(SUPPORTED_PILOTS))
def test_provider_catalog_has_bounded_runtime_fixture_and_live_acceptance(provider: str) -> None:
    spec = validate_staging_pilots(CONFIGS)["providers"][provider]
    assert 1 <= len(set(spec["instruments"])) <= 2
    assert spec["fixture_acceptance"] == f"test_staging_pilots:{provider}"
    assert spec["runtime_command"]
    scripts = tomllib.loads((CONFIGS.parent / "pyproject.toml").read_text())["project"]["scripts"]
    assert scripts[spec["runtime_command"]] == PILOT_RUNTIMES[provider][1] + ":main"
    assert spec["live_acceptance"] == "source_raw_terminal_canonical_serve"


def test_catalog_missing_provider_and_unbound_config_fail_closed(tmp_path: Path) -> None:
    for source in CONFIGS.glob("*.json"):
        shutil.copy(source, tmp_path / source.name)
    path = tmp_path / "staging_provider_pilots.v1.json"
    catalog = json.loads(path.read_bytes())
    catalog["providers"].pop("taifex")
    path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError):
        validate_staging_pilots(tmp_path)


def test_staging_minute_identity_rejects_legacy_sequence_state(tmp_path: Path) -> None:
    old = load_manifest(CONFIGS / "shioaji_tw_pilot.v1.json")
    new = load_manifest(CONFIGS / "shioaji_tw_staging_pilot.v3.json")
    assert [item["sequence_count"] for item in new["sequences"]] == [1, 1]
    state = open_production_state(tmp_path / "state.sqlite3", old)
    # A durable legacy daily plan remains legacy, including its pending ETF sequences.
    state.ensure(
        "legacy", "2026-09-18", old["universe_id"], "legacy-update", old["sequences"], "2026-09-18"
    )
    with pytest.raises(ValueError):
        validate_production_state_path(tmp_path / "state.sqlite3", manifest=new)


def _rows() -> list[dict]:
    return [
        {
            "product_code": p,
            "contract_code": f"{p}:{month}",
            "contract_month": month,
            "trade_date": "2026-10-02",
            "session": session,
            "open": "100",
            "high": "110",
            "low": "90",
            "close": "105",
            "volume": 10,
            "settlement_price": "105",
            "open_interest": 10,
        }
        for p in ("TX", "MTX")
        for month in ("202610W1", "202610", "202611")
        for session in ("regular", "after_hours")
    ]


def test_actual_monthly_near_expiry_and_session_selection() -> None:
    result = select_near_month(_rows(), date(2026, 10, 2))
    assert len(result) == 4
    assert {row["contract_code"] for row in result} == {"TX:202610", "MTX:202610"}
    with pytest.raises(ValueError):
        select_near_month(
            [row for row in _rows() if row["session"] == "regular"], date(2026, 10, 2)
        )


def _calendar() -> SimpleNamespace:
    return SimpleNamespace(
        get_day=lambda market, day: (
            SimpleNamespace(
                is_open=day.weekday() < 5, day_status="open" if day.weekday() < 5 else "closed"
            ),
            1,
        )
    )


def test_latest_completed_trading_day_uses_separate_taifex_calendar() -> None:
    assert latest_completed_day(_calendar(), datetime(2026, 10, 5, 2, tzinfo=timezone.utc)) == date(
        2026, 10, 2
    )


@pytest.mark.parametrize(
    ("source_denied", "stop_after_acceptance"), [(False, False), (True, False), (False, True)]
)
def test_taifex_raw_first_prepared_restart_idempotency_and_terminal_counts(
    tmp_path: Path,
    contracts_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_denied: bool,
    stop_after_acceptance: bool,
) -> None:
    import threading

    from findb_fetcher import taifex_pilot

    events = []
    selected = _rows()

    def report(http, *, product, **kwargs):
        events.append("fetch")
        return product.encode()

    monkeypatch.setattr(taifex_pilot, "fetch_report", report)
    monkeypatch.setattr(
        taifex_pilot,
        "parse_report",
        lambda raw, **kwargs: [row for row in selected if row["product_code"] == raw.decode()],
    )

    class Store:
        def persist(self, raw, **kwargs):
            events.append("raw")
            digest = hashlib.sha256(raw).hexdigest()
            return RawObject("r2://" + "a" * 32 + "/pilot-raw/taifex/" + digest, digest, len(raw))

    requests = []
    stop_event = threading.Event()

    class DeniedError(RuntimeError):
        status_code = 401

    class Source:
        def prepare(self, request):
            requests.append(request)
            return request

        def deliver(self, prepared, **kwargs):
            events.append("deliver")
            if source_denied:
                raise DeniedError("secret provider detail must never escape")
            if len(requests) == 1 and not stop_after_acceptance:
                raise ValueError("temporary fixture failure")
            return SimpleNamespace(run_id=UUID(int=1), attempt_id=UUID(int=2))

        def get_run_status(self, *args, **kwargs):
            if len(requests) == 1 and stop_after_acceptance:
                stop_event.set()
                return SimpleNamespace(status="queued")
            return SimpleNamespace(
                status="completed", total_records=4, success_records=4, failed_records=0
            )

    config = json.loads((CONFIGS / "taifex_tw_staging_pilot.v1.json").read_bytes())
    state_path = tmp_path / "pilot.sqlite3"
    args = dict(
        now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc),
        calendar=_calendar(),
        http=object(),
        source=Source(),
        raw_store=Store(),
        registry=ContractRegistry(contracts_dir),
        config=config,
        stop_event=stop_event,
    )
    first = PilotState(state_path).run(**args)
    assert first["status"] == "retry_pending"
    if stop_after_acceptance:
        assert first["reason"] == "stopped"
        with sqlite3.connect(state_path) as db:
            assert db.execute("SELECT attempts FROM pilot_day").fetchone() == (0,)
        stop_event.clear()
    assert events[:5] == ["fetch", "raw", "fetch", "raw", "raw"]
    result = PilotState(state_path).run(**args)
    if source_denied:
        assert result["status"] == "retry_pending"
        result = PilotState(state_path).run(**args)
        assert result == {
            "target_date": "2026-10-02",
            "status": "blocked",
            "reason": "source:DeniedError:http_401",
        }
        assert PilotState(state_path).run(**args)["status"] == "blocked"
        assert requests[0] == requests[1] == requests[2]
        assert events.count("fetch") == 2
        with sqlite3.connect(state_path) as db:
            assert db.execute(
                "SELECT record_count,requests,attempts FROM pilot_day"
            ).fetchone() == (0, 2, 3)
        return
    assert result["status"] == "completed", result
    assert requests[0] == requests[1]
    assert events.count("fetch") == 2
    assert PilotState(state_path).run(**args)["status"] == "completed"
    assert len(requests) == 2
    with sqlite3.connect(state_path) as db:
        assert db.execute("SELECT record_count,requests FROM pilot_day").fetchone() == (4, 2)


def test_new_minute_pilot_selects_only_latest_completed_open_day():
    from findb_fetcher.shioaji_scheduler import ShioajiProductionScheduler
    from findb_fetcher.shioaji_staging import Result

    calls = []
    coordinator = SimpleNamespace(
        run=lambda target, **kwargs: (
            calls.append(target) or [Result("completed", "source", count=1)]
        )
    )
    scheduler = ShioajiProductionScheduler(
        state=SimpleNamespace(mark_cutoff=lambda value: None),
        manifest=load_manifest(CONFIGS / "shioaji_tw_staging_pilot.v3.json"),
        calendar=_calendar(),
        coordinator_factory=lambda now: coordinator,
    )
    result = scheduler.run_once(now=datetime(2026, 10, 3, 7, tzinfo=timezone.utc))
    assert result.status == "completed"
    assert calls == [date(2026, 10, 2)]


def test_new_minute_pilot_delivers_complete_bars_for_both_single_instrument_sequences(
    tmp_path, contracts_dir
):
    from findb_fetcher.client import PreparedDelivery
    from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot
    from findb_fetcher.shioaji_scheduler import ProductionCoordinator

    target = date(2026, 10, 2)
    acquired = []
    delivered = []

    class Gateway:
        def fetch_kbars(self, symbol, day):
            acquired.append((symbol, day))
            stamp = int(datetime(2026, 10, 2, 1, tzinfo=timezone.utc).timestamp() * 1_000_000_000)
            return ShioajiKbarsSnapshot(
                {
                    "ts": (stamp, stamp + 60_000_000_000),
                    **{
                        key: (100, 100)
                        for key in ("Open", "High", "Low", "Close", "Volume", "Amount")
                    },
                },
                None,
                None,
            )

    class Store:
        calls = 0

        def persist(self, raw, **kwargs):
            self.calls += 1
            return RawObject(
                "r2://" + "a" * 32 + "/pilot-raw/shioaji/" + str(self.calls),
                hashlib.sha256(raw).hexdigest(),
                len(raw),
            )

    store = Store()

    class Source:
        def prepare(self, request):
            assert store.calls > len(delivered)
            return PreparedDelivery(
                json.dumps(request).encode(),
                request["idempotency_key"],
                request["dataset_key"],
                "market_minute",
                1,
            )

        def deliver(self, prepared):
            delivered.append(json.loads(prepared.body))
            return SimpleNamespace(
                run_id=UUID(int=len(delivered)), attempt_id=UUID(int=len(delivered) + 10)
            )

        def get_run_status(self, *args, **kwargs):
            return SimpleNamespace(
                status="completed", total_records=2, success_records=2, failed_records=0
            )

    manifest = load_manifest(CONFIGS / "shioaji_tw_staging_pilot.v3.json")
    state = open_production_state(tmp_path / "state.sqlite3", manifest)
    coordinator = ProductionCoordinator(
        state,
        manifest,
        Gateway(),
        source=Source(),
        raw_store=store,
        contracts=ContractRegistry(contracts_dir),
        now=lambda: datetime(2026, 10, 2, 7, tzinfo=timezone.utc),
    )
    assert {row.code for row in coordinator.run(target, deliver=True)} == {"completed"}
    assert acquired == [("2330", target), ("0050", target)]
    assert [len(row["payload"]["data"]) for row in delivered] == [2, 2]
    assert {row["dataset_key"] for row in delivered} == {"tw_equity_minute", "tw_etf_minute"}
    assert {row.code for row in coordinator.run(target, deliver=True)} == {"completed"}
    assert len(acquired) == len(delivered) == 2
    state.close()


@pytest.mark.parametrize("target", [None, "production"])
def test_taifex_production_rejected_before_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str | None
) -> None:
    from findb_fetcher.taifex_pilot import main

    if target is None:
        monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("APP_ENVIRONMENT", target)
    assert (
        main(
            [
                "--initialize-state",
                "--config",
                str(CONFIGS / "taifex_tw_staging_pilot.v1.json"),
                "--state-path",
                str(tmp_path / "state.sqlite3"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "state.sqlite3").exists()


@pytest.mark.parametrize("provider", list(SUPPORTED_PILOTS))
def test_each_provider_real_cli_starts_offline_readiness(
    provider, tmp_path, monkeypatch, contracts_dir
):
    import importlib

    modules = {
        "twelve_data": "scheduler_cli",
        "finlab": "finlab_scheduler_cli",
        "shioaji": "shioaji_scheduler_cli",
        "taifex": "taifex_pilot",
    }
    env = {
        "APP_ENVIRONMENT": "staging",
        "SOURCE_API_URL": "https://source.example",
        "SOURCE_CLIENT_KEY": "fixture-source",
        "FINDB_SERVE_BASE_URL": "https://serve.example",
        "FETCHER_CALENDAR_SERVE_API_KEY": "fixture-calendar",
        "CLOUDFLARE_R2_ACCOUNT_ID": "a" * 32,
        "CLOUDFLARE_R2_RAW_BUCKET": "pilot-raw",
        "CLOUDFLARE_R2_RAW_ACCESS_KEY_ID": "fixture-r2",
        "CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY": "fixture-r2-secret",
        "FINLAB_API_TOKEN": "fixture-finlab",
        "SHIOAJI_API_KEY": "fixture-shioaji",
        "SHIOAJI_SECRET_KEY": "fixture-shioaji-secret",
        "SHIOAJI_SIMULATION": "true",
        "FETCHER_CONTRACTS_DIR": str(contracts_dir),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    module = importlib.import_module(f"findb_fetcher.{modules[provider]}")
    args = ["--check", "--state-path", str(tmp_path / "state.sqlite3")]
    if provider in {"twelve_data", "finlab"}:
        args.extend(["--schedule-file", str(CONFIGS / "daily_scheduler.staging.v3.json")])
    if provider == "twelve_data":
        args.extend(["--slot-id", "western_markets_window", "--dataset-key", "us_equity_eod"])
    if provider == "shioaji":
        args.extend(["--manifest", str(CONFIGS / "shioaji_tw_staging_pilot.v3.json")])
    if provider == "taifex":
        args.extend(["--config", str(CONFIGS / "taifex_tw_staging_pilot.v1.json")])
    assert module.main(args) == 0


@pytest.mark.parametrize("provider", list(SUPPORTED_PILOTS))
def test_each_provider_stopped_idle_then_running_acknowledgement_delivers(provider):
    import threading

    from findb_fetcher.scheduler_control import SchedulerControlLoop, validate_scheduler_definition

    stop = threading.Event()
    feeds = tuple(feed[0] for feed in SUPPORTED_PILOTS[provider])
    calls = []
    cycles = []

    class Control:
        scheduler_key = provider + "_fixture"
        _config = SimpleNamespace(scheduler_control_poll_seconds=0.01)

        def poll(self, **kwargs):
            calls.append(kwargs)
            assert len(calls) <= 8, "control must complete bounded startup fixture"
            desired = "stopped" if len(calls) == 1 else "running"
            if kwargs.get("cycle_completed_at") is not None:
                stop.set()
            return SimpleNamespace(
                provider=provider,
                dataset_keys=feeds,
                slot_id="taiwan_market_window",
                scheduled_local_time=clock_time(18),
                timezone="Asia/Taipei",
                desired_state=desired,
                revision=len(calls),
                server_time=datetime.now(timezone.utc),
            )

    def validator(response):
        validate_scheduler_definition(
            response,
            provider=provider,
            dataset_keys=feeds,
            slot_id="taiwan_market_window",
            scheduled_local_time="18:00:00",
            timezone_name="Asia/Taipei",
        )

    assert (
        SchedulerControlLoop(Control(), definition_validator=validator).run(
            lambda: cycles.append("fixture_delivered"), stop_event=stop
        )
        == 0
    )
    assert cycles == ["fixture_delivered"]
    assert [row["observed_state"] for row in calls[:3]] == ["stopped", "stopped", "running"]


def test_new_daily_state_never_claims_legacy_nvda_or_old_pending_dates(tmp_path):
    from findb_fetcher.schedule import load_schedule_manifest
    from findb_fetcher.scheduler_state import SchedulerState
    from findb_fetcher.twelve_data_scheduler import JobExecution, SchedulerService
    from findb_fetcher.universe import load_symbol_universe

    old_schedule = load_schedule_manifest(CONFIGS / "daily_scheduler.v2.json").feeds[0]
    old_universe = load_symbol_universe(CONFIGS / "twelve_data_us_common_stocks.v1.json")
    new_schedule = load_schedule_manifest(CONFIGS / "daily_scheduler.staging.v3.json").feeds[0]
    new_universe = load_symbol_universe(CONFIGS / "twelve_data_us_staging_pilot.v2.json")
    now = datetime(2026, 10, 5, 2, tzinfo=timezone.utc)
    old = SchedulerState(tmp_path / "old.sqlite3")
    old.enqueue_due(
        old_schedule, old_universe, date(2026, 10, 2), target_data_date=date(2026, 10, 1), now=now
    )
    state = SchedulerState(tmp_path / "new.sqlite3")
    state.enqueue_due(
        new_schedule, new_universe, date(2026, 10, 2), target_data_date=date(2026, 10, 1), now=now
    )
    observed = []

    class Executor:
        def execute(self, job, **kwargs):
            observed.append((job.symbol, job.target_data_date))
            return JobExecution(
                outcome="completed",
                succeeded=True,
                record_count=1,
                checkpoint_after=job.target_data_date,
            )

    service = SchedulerService(
        schedule=new_schedule,
        universe=new_universe,
        state=state,
        executor=Executor(),
        calendar=_calendar(),
    )
    assert service.run_once(now=now).claimed == 2
    assert set(observed) == {("AAPL", date(2026, 10, 2)), ("MSFT", date(2026, 10, 2))}
    with sqlite3.connect(tmp_path / "old.sqlite3") as db:
        assert db.execute("SELECT status FROM scheduled_job WHERE symbol='NVDA'").fetchone() == (
            "pending",
        )
