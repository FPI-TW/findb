"""CLI for the production-pilot Shioaji recurring scheduler."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Never, Sequence

from findb_fetcher.scheduler_control import SchedulerControlClient, SchedulerControlLoop
from findb_fetcher.shioaji_scheduler import (
    MAX_OUTPUT_BYTES,
    SchedulerRun,
    build_runtime,
    default_manifest_path,
    default_state_path,
    load_manifest,
    open_production_state,
    validate_contracts,
    validate_production_runtime,
    validate_production_state_path,
)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_RETRY_PENDING = 8
EXIT_SCHEDULE_FAILED = 9
SCHEDULER_CONTROL_KEY = "shioaji_tw_pilot_v1"


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise ValueError("invalid arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="findb-fetch-shioaji-scheduler",
        description="Run the governed four-symbol Shioaji production pilot.",
        add_help=True,
    )
    parser.add_argument("--manifest", type=Path, default=default_manifest_path())
    parser.add_argument("--state-path", type=Path, default=default_state_path())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run-forever", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        manifest = load_manifest(args.manifest)
        validate_production_state_path(args.state_path, read_only=args.check)
        fetcher, calendar_config, raw_config = validate_production_runtime()
        contracts = validate_contracts(fetcher.contracts_dir)
        if args.check:
            # Check mode intentionally stops before SQLite opening and before
            # any provider, R2, Source, or published-calendar client exists.
            return _emit(
                {"code": "CHECK_OK", "stage": "manifest", "count": len(manifest["sequences"])}
            )

        state = open_production_state(args.state_path)
        try:
            if args.run_forever:
                return _run_forever(
                    fetcher,
                    lambda: _run_cycle(
                        manifest,
                        state,
                        fetcher=fetcher,
                        calendar_config=calendar_config,
                        raw_config=raw_config,
                        contracts=contracts,
                    ),
                )
            scheduler, clients = build_runtime(
                manifest,
                state,
                fetcher=fetcher,
                calendar_config=calendar_config,
                raw_config=raw_config,
                contracts=contracts,
            )
            execution = scheduler.run_once(
                now=datetime.now(timezone.utc),
            )
        finally:
            state.close()
            # Source and calendar own HTTP clients; R2's boto client has no
            # required lifecycle hook and is intentionally not touched here.
            _close_clients(locals().get("clients"))
        return _emit_run(execution)
    except Exception:
        return _emit(
            {"code": "SAFE_FAILURE", "stage": "setup", "count": 0},
            status=EXIT_CONFIG_ERROR,
        )


def _run_forever(
    config: Any,
    cycle_factory: Any,
    *,
    stop_event: Any = None,
) -> int:
    with SchedulerControlClient(config, SCHEDULER_CONTROL_KEY) as control:
        loop = SchedulerControlLoop(control)
        return loop.run(cycle_factory, stop_event=stop_event)


def _run_cycle(
    manifest: dict[str, Any],
    state: Any,
    *,
    fetcher: Any,
    calendar_config: Any,
    raw_config: Any,
    contracts: Any,
) -> None:
    """Build provider/runtime clients only after control grants a cycle."""

    scheduler, clients = build_runtime(
        manifest,
        state,
        fetcher=fetcher,
        calendar_config=calendar_config,
        raw_config=raw_config,
        contracts=contracts,
    )
    try:
        execution = scheduler.run_once(now=datetime.now(timezone.utc))
        _emit_run(execution)
    finally:
        # Source and calendar own HTTP clients; R2's boto client has no
        # required lifecycle hook and is intentionally not touched here.
        _close_clients(clients)


def _close_clients(clients: object) -> None:
    if not isinstance(clients, tuple):
        return
    for client in clients:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _emit_run(execution: SchedulerRun) -> int:
    payload: dict[str, Any] = {
        "code": "OK" if execution.status in {"completed", "skipped"} else "RUN_FAILED",
        "stage": "schedule",
        "status": execution.status,
        "target_date": execution.target_date.isoformat(),
        "count": sum(result.count for result in execution.results),
        "results": [
            {
                "code": result.code,
                "stage": result.stage,
                "count": result.count,
                "retryable": result.retryable,
                "symbol": result.symbol,
                **({"reason": result.reason} if result.reason is not None else {}),
                **({"request_id": result.request_id} if result.request_id else {}),
                **({"run_id": result.run_id} if result.run_id else {}),
            }
            for result in execution.results
        ],
    }
    if execution.skip_reason is not None:
        payload["skip_reason"] = execution.skip_reason
    if execution.calendar_revision is not None:
        payload["calendar_revision"] = execution.calendar_revision
    return _emit(payload, status=_exit_code(execution))


_emit_summary = _emit_run


def _exit_code(execution: SchedulerRun) -> int:
    if execution.status == "failed":
        return EXIT_SCHEDULE_FAILED
    if execution.status == "retry_pending":
        return EXIT_RETRY_PENDING
    return EXIT_OK


def _emit(value: dict[str, Any], *, status: int = EXIT_OK) -> int:
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = '{"code":"SAFE_FAILURE","stage":"output","count":0}'
        status = EXIT_CONFIG_ERROR
    if len(raw.encode()) > MAX_OUTPUT_BYTES:
        raw = '{"code":"SAFE_FAILURE","stage":"output","count":0}'
        status = EXIT_CONFIG_ERROR
    sys.stdout.write(raw + "\n")
    return status


_emit_json = _emit


if __name__ == "__main__":
    raise SystemExit(main())
