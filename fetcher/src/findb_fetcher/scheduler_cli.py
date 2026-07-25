"""CLI for one-shot or long-running durable Twelve Data scheduling."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import ConfigError, FetcherConfig
from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataError,
)
from findb_fetcher.raw_storage import (
    R2RawPayloadStore,
    RawStorageConfig,
    RawStorageError,
)
from findb_fetcher.schedule import ScheduleError, load_schedule_config
from findb_fetcher.scheduler_state import SchedulerState, SchedulerStateError
from findb_fetcher.twelve_data_scheduler import (
    SchedulerRun,
    SchedulerService,
    TwelveDataScheduledExecutor,
)
from findb_fetcher.universe import UniverseError, load_symbol_universe

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_RETRY_PENDING = 8
EXIT_SCHEDULE_FAILED = 9
MAX_OUTPUT_BYTES = 2048


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the durable scheduler for a governed Twelve Data universe."
    )
    parser.add_argument(
        "--schedule-file",
        type=Path,
        default=_default_schedule_file(),
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=_default_state_path(),
    )
    parser.add_argument(
        "--run-forever",
        action="store_true",
        help="Poll continuously; the safe default runs one due cycle and exits.",
    )
    parser.add_argument(
        "--as-of",
        type=_aware_datetime,
        help="Override the UTC-aware scheduler clock for a one-shot run.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_forever and args.as_of is not None:
        raise SystemExit("--as-of cannot be combined with --run-forever")
    try:
        schedule = load_schedule_config(args.schedule_file)
        universe = load_symbol_universe(schedule.universe_file)
        if schedule.outputsize > universe.limits.max_records_per_symbol:
            raise ScheduleError("schedule outputsize exceeds universe limit")
        fetcher_config = FetcherConfig.from_env()
        registry = ContractRegistry(fetcher_config.contracts_dir)
        state = SchedulerState(args.state_path)
        raw_store = R2RawPayloadStore(RawStorageConfig.from_env())

        with ExitStack() as stack:
            provider = stack.enter_context(TwelveDataClient(TwelveDataConfig.from_env()))
            source = stack.enter_context(SourceAPIClient(fetcher_config, registry))
            executor = TwelveDataScheduledExecutor(
                schedule=schedule,
                universe=universe,
                provider=provider,
                source=source,
                registry=registry,
                raw_store=raw_store,
                state=state,
            )
            service = SchedulerService(
                schedule=schedule,
                universe=universe,
                state=state,
                executor=executor,
            )
            if args.run_forever:
                return _run_forever(service, schedule.poll_interval_seconds)
            execution = service.run_once(now=args.as_of or datetime.now(timezone.utc))
        _emit_summary(execution)
        return _exit_code(execution)
    except (
        ConfigError,
        ContractError,
        ScheduleError,
        RawStorageError,
        SchedulerStateError,
        TwelveDataError,
        UniverseError,
        OSError,
        ValueError,
    ):
        _emit_json({"error": "scheduler_configuration_failed"}, stream=sys.stderr)
        return EXIT_CONFIG_ERROR


def _run_forever(service: SchedulerService, poll_interval_seconds: int) -> int:
    while True:
        execution = service.run_once(now=datetime.now(timezone.utc))
        if execution.enqueued or execution.claimed:
            _emit_summary(execution)
        time.sleep(poll_interval_seconds)


def _exit_code(execution: SchedulerRun) -> int:
    if execution.status == "failed":
        return EXIT_SCHEDULE_FAILED
    if execution.status in {"retry_pending", "pending"}:
        return EXIT_RETRY_PENDING
    return EXIT_OK


def _emit_summary(execution: SchedulerRun) -> None:
    _emit_json(
        {
            "mode": "scheduler_once",
            "status": execution.status,
            "schedule_id": execution.schedule_id,
            "scheduled_date": execution.scheduled_date.isoformat(),
            "enqueued": execution.enqueued,
            "claimed": execution.claimed,
            "credits_used": execution.credits_used,
            "status_counts": execution.status_counts,
            "results": [
                {
                    "symbol": result.symbol,
                    "outcome": result.outcome,
                    "state_status": result.state_status,
                    "attempt_count": result.attempt_count,
                    "record_count": result.record_count,
                    **(
                        {"checkpoint_after": result.checkpoint_after.isoformat()}
                        if result.checkpoint_after
                        else {}
                    ),
                }
                for result in execution.results
            ],
        },
        stream=sys.stdout,
    )


def _emit_json(payload: dict[str, Any], *, stream: Any) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("scheduler summary exceeds the output size limit")
    print(encoded.decode(), file=stream)


def _aware_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--as-of must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of must include a timezone")
    return parsed.astimezone(timezone.utc)


def _default_schedule_file() -> Path:
    configured = os.getenv("FETCHER_SCHEDULE_FILE")
    if configured:
        return Path(configured)
    container_path = Path("/app/configs/twelve_data_us_common_stocks_daily.v1.json")
    if container_path.is_file():
        return container_path
    return (
        Path(__file__).resolve().parents[2]
        / "configs"
        / "twelve_data_us_common_stocks_daily.v1.json"
    )


def _default_state_path() -> Path:
    return Path(os.getenv("FETCHER_STATE_PATH", "/var/lib/findb-fetcher/state.sqlite3"))


if __name__ == "__main__":
    raise SystemExit(main())
