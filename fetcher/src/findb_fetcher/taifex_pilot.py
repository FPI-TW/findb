"""Durable staging-only TAIFEX pilot: two actual near-month contracts, one day."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig, MarketCalendarConfig, app_environment
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.market_calendar import PublishedCalendarClient
from findb_fetcher.pilot_catalog import validate_staging_pilots
from findb_fetcher.providers.taifex import fetch_report, parse_report
from findb_fetcher.raw_storage import (
    R2RawPayloadStore,
    RawStorageConfig,
    attach_raw_object,
    require_raw_provenance,
)
from findb_fetcher.scheduler_control import (
    SchedulerControlClient,
    SchedulerControlLoop,
    require_scheduler_stopped,
    scheduler_stop_event,
    validate_scheduler_definition,
)
from findb_fetcher.twelve_data_scheduler import _wait_for_terminal

CONTROL_KEY = "taifex_tw_futures_pilot_v1"
UNIVERSE_ID = "taifex_tw_staging_pilot_v1"
TAIPEI = ZoneInfo("Asia/Taipei")


def select_near_month(rows: list[dict[str, Any]], target: date) -> list[dict[str, Any]]:
    """Select one source-observed monthly expiry per product; retain both sessions."""
    selected = []
    for product in ("TX", "MTX"):
        members = [row for row in rows if row["product_code"] == product]
        months = sorted(
            {
                row["contract_month"]
                for row in members
                if re.fullmatch(r"[0-9]{6}", row["contract_month"])
                and row["contract_month"] >= target.strftime("%Y%m")
            }
        )
        if not months:
            raise ValueError("actual TAIFEX near-month expiry missing")
        observations = [row for row in members if row["contract_month"] == months[0]]
        if {row["session"] for row in observations} != {"regular", "after_hours"} or len(
            observations
        ) != 2:
            raise ValueError("TAIFEX near-month pilot requires both exchange-attributed sessions")
        for row in observations:
            if (
                row["contract_code"] != f"{product}:{months[0]}"
                or row["trade_date"] != target.isoformat()
            ):
                raise ValueError("actual TAIFEX contract identity differs")
            if row["close"] is None:
                raise ValueError("TAIFEX pilot needs source-provided closing observations")
        selected.extend(observations)
    return sorted(selected, key=lambda row: (row["contract_code"], row["session"]))


def latest_completed_day(calendar: Any, now: datetime) -> date:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("pilot clock must be timezone-aware")
    local = now.astimezone(TAIPEI)
    candidate = local.date() if local.time() >= clock_time(18) else local.date() - timedelta(days=1)
    for _ in range(14):
        day, _revision = calendar.get_day("TAIFEX", candidate)
        if day.is_open and day.day_status == "open":
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError("latest completed TAIFEX day unavailable")


class PilotState:
    """Immutable prepared delivery and request budget survive process restarts."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        if path.is_symlink():
            raise ValueError("pilot state cannot be a symlink")
        if read_only:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
                self._validate(db)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (identity TEXT PRIMARY KEY)")
            if db.execute("SELECT count(*) FROM meta").fetchone()[0] == 0:
                db.execute("INSERT INTO meta VALUES (?)", (UNIVERSE_ID,))
            db.execute(
                "CREATE TABLE IF NOT EXISTS pilot_day (target_date TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0, prepared TEXT, run_id TEXT, attempt_id TEXT, record_count INTEGER NOT NULL DEFAULT 0, last_outcome TEXT)"
            )
            self._validate(db)
        os.chmod(path, 0o600)

    @staticmethod
    def _validate(db: sqlite3.Connection) -> None:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)] or db.execute(
            "SELECT identity FROM meta"
        ).fetchall() != [(UNIVERSE_ID,)]:
            raise ValueError("TAIFEX pilot state identity invalid")
        columns = [row[1] for row in db.execute("PRAGMA table_info(pilot_day)")]
        if columns != [
            "target_date",
            "status",
            "attempts",
            "requests",
            "prepared",
            "run_id",
            "attempt_id",
            "record_count",
            "last_outcome",
        ]:
            raise ValueError("TAIFEX pilot state schema invalid")

    def run(
        self,
        *,
        now: datetime,
        calendar: Any,
        http: httpx.Client,
        source: Any,
        raw_store: Any,
        registry: ContractRegistry,
        config: dict[str, Any],
        stop_event: Any = None,
    ) -> dict[str, Any]:
        def monotonic() -> float:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("pilot stopped at a durable boundary")
            return time.monotonic()

        def pause(seconds: float) -> None:
            if stop_event is None:
                time.sleep(seconds)
            else:
                stop_event.wait(seconds)
                monotonic()

        target = latest_completed_day(calendar, now)
        # Host release ensures a single writer; lock also rejects accidental duplicate CLIs.
        with self.path.with_suffix(".writer.lock").open("a+b") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect(self.path) as db:
                db.row_factory = sqlite3.Row
                db.execute(
                    "INSERT OR IGNORE INTO pilot_day (target_date,status) VALUES (?, 'pending')",
                    (target.isoformat(),),
                )
                db.commit()
                row = db.execute(
                    "SELECT * FROM pilot_day WHERE target_date=?", (target.isoformat(),)
                ).fetchone()
                if (
                    row["status"] == "completed"
                    or row["attempts"] >= config["max_attempts_per_date"]
                ):
                    return {
                        "target_date": target.isoformat(),
                        "status": "blocked"
                        if row["attempts"] >= config["max_attempts_per_date"]
                        and row["status"] != "completed"
                        else row["status"],
                        "reason": row["last_outcome"],
                        "record_count": row["record_count"],
                    }
                monotonic()
                db.execute(
                    "UPDATE pilot_day SET attempts=attempts+1,status='running' WHERE target_date=?",
                    (target.isoformat(),),
                )
                db.commit()
                phase = "provider"
                try:
                    monotonic()
                    request = json.loads(row["prepared"]) if row["prepared"] else None
                    if request is None:
                        rows = []
                        raw_refs = []
                        for product in config["products"]:
                            monotonic()
                            spent = db.execute(
                                "SELECT requests FROM pilot_day WHERE target_date=?",
                                (target.isoformat(),),
                            ).fetchone()[0]
                            if spent >= config["max_requests_per_date"]:
                                raise ValueError("TAIFEX per-date request budget exhausted")
                            db.execute(
                                "UPDATE pilot_day SET requests=requests+1 WHERE target_date=?",
                                (target.isoformat(),),
                            )
                            db.commit()
                            raw = fetch_report(
                                http,
                                target_date=target,
                                latest=False,
                                product=product,
                                max_response_bytes=config["max_response_bytes"],
                            )
                            # Persist each complete report before any parse, mapping or Source call.
                            stored = raw_store.persist(
                                raw,
                                dataset_key="tw_futures_eod",
                                source_symbol=product,
                                provider="taifex",
                            )
                            raw_refs.append(stored)
                            parsed = parse_report(raw, target_date=target, historical=True)
                            if any(item["product_code"] != product for item in parsed):
                                raise ValueError("TAIFEX product report differs from request")
                            rows.extend(parsed)
                        selected = select_near_month(rows, target)
                        request = {
                            "dataset_key": "tw_futures_eod",
                            "schema_id": "futures_eod",
                            "schema_version": 1,
                            "source": "taifex",
                            "fetched_at": now.isoformat(),
                            "payload": {
                                "batch": {
                                    "data_date": target.isoformat(),
                                    "delivery_mode": "incremental",
                                    "declared_record_count": len(selected),
                                },
                                "data": selected,
                            },
                            "delivery": {
                                "slot_id": "taiwan_market_window",
                                "scheduled_for": datetime.combine(target, clock_time(18), TAIPEI)
                                .astimezone(timezone.utc)
                                .isoformat(),
                                "target_data_date": target.isoformat(),
                                "work_item_id": UNIVERSE_ID,
                            },
                        }
                        # Manifest of both raw reports preserves durable evidence for every record.
                        raw_manifest = json.dumps(
                            [{"ref": item.ref, "sha256": item.sha256} for item in raw_refs],
                            sort_keys=True,
                        ).encode()
                        stored = raw_store.persist(
                            raw_manifest,
                            dataset_key="tw_futures_eod",
                            source_symbol="TX-MTX",
                            provider="taifex",
                        )
                        identity = hashlib.sha256(
                            f"{UNIVERSE_ID}:{target.isoformat()}".encode()
                        ).hexdigest()
                        request["request_key"] = f"pilot:{identity}"
                        request["idempotency_key"] = f"pilot:{identity}"
                        attach_raw_object(request, stored)
                        registry.validate("futures_eod", 1, request)
                        db.execute(
                            "UPDATE pilot_day SET prepared=? WHERE target_date=?",
                            (json.dumps(request, sort_keys=True), target.isoformat()),
                        )
                        db.commit()
                    phase = "source"
                    monotonic()
                    require_raw_provenance(request)
                    registry.validate("futures_eod", 1, request)
                    records = request["payload"]["data"]
                    if (
                        len(records) != config["max_records_per_date"]
                        or select_near_month(records, target) != records
                    ):
                        raise ValueError("prepared TAIFEX identity invalid")
                    deadline = monotonic() + 1800
                    prepared = source.prepare(request)
                    receipt = source.deliver(prepared, deadline=deadline, monotonic=monotonic)
                    terminal = _wait_for_terminal(
                        source,
                        receipt.run_id,
                        prepared,
                        deadline=deadline,
                        poll_interval_seconds=30,
                        monotonic=monotonic,
                        sleep=pause,
                    )
                    if (
                        terminal.status != "completed"
                        or terminal.total_records != 4
                        or terminal.success_records != 4
                        or terminal.failed_records != 0
                    ):
                        raise ValueError("TAIFEX canonical terminal proof incomplete")
                    db.execute(
                        "UPDATE pilot_day SET status='completed',run_id=?,attempt_id=?,record_count=4 WHERE target_date=?",
                        (str(receipt.run_id), str(receipt.attempt_id), target.isoformat()),
                    )
                    db.commit()
                except Exception as exc:
                    if isinstance(exc, InterruptedError):
                        db.execute(
                            "UPDATE pilot_day SET attempts=attempts-1,status='retry_pending',last_outcome='stopped' WHERE target_date=?",
                            (target.isoformat(),),
                        )
                        db.commit()
                        return {
                            "target_date": target.isoformat(),
                            "status": "retry_pending",
                            "reason": "stopped",
                        }
                    status = (
                        "retry_pending"
                        if row["attempts"] + 1 < config["max_attempts_per_date"]
                        else "blocked"
                    )
                    reason = (
                        "stopped"
                        if isinstance(exc, InterruptedError)
                        else f"{phase}:{type(exc).__name__}"
                    )
                    http_status = getattr(exc, "status_code", None)
                    if type(http_status) is int:
                        reason += f":http_{http_status}"
                    db.execute(
                        "UPDATE pilot_day SET status=?,last_outcome=? WHERE target_date=?",
                        (status, reason, target.isoformat()),
                    )
                    db.commit()
                    return {"target_date": target.isoformat(), "status": status, "reason": reason}
        return {"target_date": target.isoformat(), "status": "completed", "record_count": 4}


def _definition(response: Any) -> None:
    validate_scheduler_definition(
        response,
        provider="taifex",
        dataset_keys=("tw_futures_eod",),
        slot_id="taiwan_market_window",
        scheduled_local_time="18:00:00",
        timezone_name="Asia/Taipei",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("/app/configs/taifex_tw_staging_pilot.v1.json")
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path(
            os.getenv(
                "FETCHER_STATE_PATH", "/var/lib/findb-taifex-fetcher/staging-pilot-v1/state.sqlite3"
            )
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--initialize-state", action="store_true")
    mode.add_argument("--require-stopped", action="store_true")
    mode.add_argument("--run-forever", action="store_true")
    args = parser.parse_args(argv)
    try:
        if app_environment("") != "staging":
            raise ValueError("TAIFEX pilot requires the explicit staging deployment target")
        validate_staging_pilots(args.config.parent)
        config = json.loads(args.config.read_bytes())
        expected = json.loads((args.config.parent / "taifex_tw_staging_pilot.v1.json").read_bytes())
        if config != expected:
            raise ValueError("TAIFEX config differs from validated pilot")
        state = PilotState(args.state_path, read_only=args.require_stopped)
        if args.initialize_state:
            return 0
        fetcher = FetcherConfig.from_env()
        calendars = MarketCalendarConfig.from_env()
        registry = ContractRegistry(fetcher.contracts_dir)
        raw_config = RawStorageConfig.from_env()
        if args.check:
            print('{"mode":"taifex_pilot_check","status":"ok"}')
            return 0
        if args.require_stopped:
            require_scheduler_stopped(fetcher, CONTROL_KEY, definition_validator=_definition)
            return 0

        stop_signal = threading.Event()

        def cycle() -> dict[str, Any]:
            with ExitStack() as stack:
                calendar = stack.enter_context(PublishedCalendarClient(calendars))
                http = stack.enter_context(httpx.Client(timeout=30, follow_redirects=False))
                source = stack.enter_context(SourceAPIClient(fetcher, registry))
                result = state.run(
                    now=datetime.now(timezone.utc),
                    calendar=calendar,
                    http=http,
                    source=source,
                    raw_store=R2RawPayloadStore(raw_config),
                    registry=registry,
                    config=config,
                    stop_event=stop_signal,
                )
                print(json.dumps(result, sort_keys=True))
                return result

        def controlled_cycle() -> None:
            result = cycle()
            if result["status"] != "completed":
                raise RuntimeError(
                    f"taifex_pilot:{result['status']}:{result.get('reason', 'incomplete')}"
                )

        if args.run_forever:
            with scheduler_stop_event() as stop:
                stop_signal = stop
                with SchedulerControlClient(fetcher, CONTROL_KEY) as control:
                    return SchedulerControlLoop(control, definition_validator=_definition).run(
                        controlled_cycle, stop_event=stop
                    )
        result = cycle()
        return 0 if result["status"] == "completed" else 9 if result["status"] == "blocked" else 8
    except (ValueError, OSError, RuntimeError, sqlite3.Error):
        print('{"error":"taifex_pilot_configuration_failed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
