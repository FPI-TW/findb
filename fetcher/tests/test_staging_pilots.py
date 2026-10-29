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


def _calendar() -> SimpleNamespace:
    return SimpleNamespace(
        get_day=lambda market, day: (
            SimpleNamespace(
                is_open=day.weekday() < 5, day_status="open" if day.weekday() < 5 else "closed"
            ),
            1,
        )
    )


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


@pytest.mark.parametrize("provider", list(SUPPORTED_PILOTS))
def test_catalog_runtime_arguments_execute_supported_offline_check(provider, tmp_path):
    import os
    import subprocess
    import sys

    spec = validate_staging_pilots(CONFIGS)["providers"][provider]
    original_files = set(tmp_path.iterdir())
    result = subprocess.run(
        [sys.executable, "-m", PILOT_RUNTIMES[provider][1], *spec["runtime_arguments"], "--check"],
        cwd=CONFIGS.parent,
        env={
            "PATH": os.environ["PATH"],
            "APP_ENVIRONMENT": "staging",
            "FETCHER_ACCOUNT_STATE_PATH": str(tmp_path / "account/governor.sqlite3"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "valid",
        "desired_state": "stopped",
        "provider": provider,
    }
    assert set(tmp_path.iterdir()) == original_files


@pytest.mark.parametrize("provider", list(SUPPORTED_PILOTS))
@pytest.mark.parametrize("mutation", ["retired-command", "profile", "provider", "config"])
def test_catalog_rejects_retired_or_mismatched_runtime_binding(tmp_path, provider, mutation):
    for source in CONFIGS.glob("*.json"):
        shutil.copy(source, tmp_path / source.name)
    path = tmp_path / "staging_provider_pilots.v1.json"
    catalog = json.loads(path.read_bytes())
    spec = catalog["providers"][provider]
    if mutation == "retired-command":
        spec["runtime_command"] = {
            "twelve_data": "findb-fetch-scheduler",
            "finlab": "findb-fetch-finlab-scheduler",
            "shioaji": "findb-fetch-shioaji-scheduler",
            "taifex": "findb-fetch-taifex-pilot",
        }[provider]
    else:
        argument = {
            "profile": "--execution-profile",
            "provider": "--provider",
            "config": "--config",
        }[mutation]
        spec["runtime_arguments"][spec["runtime_arguments"].index(argument) + 1] = "mismatched"
    path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match="scope or acceptance metadata"):
        validate_staging_pilots(tmp_path)
