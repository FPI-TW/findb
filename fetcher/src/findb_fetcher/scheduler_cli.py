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
from findb_fetcher.config import ConfigError, FetcherConfig, MarketCalendarConfig
from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.market_calendar import MarketCalendarError, PublishedCalendarClient
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
from findb_fetcher.schedule import (
    ScheduleConfig,
    ScheduleError,
    load_schedule_config,
    load_schedule_manifest,
)
from findb_fetcher.scheduler_state import SchedulerState, SchedulerStateError
from findb_fetcher.twelve_data_scheduler import (
    SchedulerRun,
    SchedulerService,
    TwelveDataScheduledExecutor,
)
from findb_fetcher.universe import SymbolUniverse, UniverseError, load_symbol_universe

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
        "--slot-id",
        help="Run one v2 manifest slot. Required when a manifest contains multiple feeds.",
    )
    parser.add_argument(
        "--dataset-key",
        help="Select one dataset when a v2 slot contains multiple provider feeds.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run-forever",
        action="store_true",
        help="Poll continuously; the safe default runs one due cycle and exits.",
    )
    mode.add_argument(
        "--as-of",
        type=_aware_datetime,
        help="Override the UTC-aware scheduler clock for a one-shot run.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate scheduler configuration and durable state without external calls.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        schedule = _load_selected_schedule(args.schedule_file, args.slot_id, args.dataset_key)
        universe = load_symbol_universe(schedule.universe_file)
        _validate_schedule_universe(schedule, universe)
        if schedule.outputsize > universe.limits.max_records_per_symbol:
            raise ScheduleError("schedule outputsize exceeds universe limit")
        fetcher_config = FetcherConfig.from_env()
        calendar_config = MarketCalendarConfig.from_env()
        registry = ContractRegistry(fetcher_config.contracts_dir)
        raw_storage_config = RawStorageConfig.from_env()
        twelve_data_config = TwelveDataConfig.from_env()
        state = SchedulerState(args.state_path)
        if args.check:
            _emit_json(
                {"mode": "scheduler_check", "status": "ok"},
                stream=sys.stdout,
            )
            return EXIT_OK

        raw_store = R2RawPayloadStore(raw_storage_config)

        with ExitStack() as stack:
            calendar = (
                stack.enter_context(PublishedCalendarClient(calendar_config))
                if calendar_config.mode == "remote"
                else None
            )
            provider = stack.enter_context(TwelveDataClient(twelve_data_config))
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
                calendar=calendar,
            )
            if args.run_forever:
                return _run_forever(service, schedule.poll_interval_seconds)
            execution = service.run_once(now=args.as_of or datetime.now(timezone.utc))
        _emit_summary(execution)
        return _exit_code(execution)
    except (
        ConfigError,
        ContractError,
        MarketCalendarError,
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
            **({"skip_reason": execution.skip_reason} if execution.skip_reason is not None else {}),
            **(
                {"calendar_revision": execution.calendar_revision}
                if execution.calendar_revision is not None
                else {}
            ),
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


def _load_selected_schedule(path: Path, slot_id: str | None, dataset_key: str | None):
    manifest = load_schedule_manifest(path)
    if manifest.schedule_version == 1:
        if slot_id is not None or dataset_key is not None:
            raise ScheduleError("--slot-id and --dataset-key are only valid for v2 manifests")
        return load_schedule_config(path)
    if slot_id is None:
        raise ScheduleError("v2 manifests require --slot-id")
    matches = [
        feed
        for feed in manifest.feeds
        if feed.slot_id == slot_id and (dataset_key is None or feed.dataset_key == dataset_key)
    ]
    if len(matches) != 1:
        raise ScheduleError("requested v2 feed is missing or ambiguous")
    schedule = matches[0]
    if schedule.provider != "twelve_data":
        raise ScheduleError("FinLab scheduler adapter is not configured")
    # Disabled entries are intentional backlog guards: do not guess a canonical mapping.
    if not schedule.enabled:
        raise ScheduleError("requested slot is disabled")
    return schedule


def _validate_schedule_universe(
    schedule: ScheduleConfig,
    universe: SymbolUniverse,
) -> None:
    if (
        schedule.provider != universe.provider
        or schedule.dataset_key != universe.dataset_key
        or schedule.market != universe.market
    ):
        raise ScheduleError("schedule and universe identities do not match")
    if universe.estimated_credits > schedule.max_credits_per_run:
        raise ScheduleError("universe exceeds schedule credit limit")
    if len(universe.symbols) * schedule.outputsize > schedule.max_records_per_run:
        raise ScheduleError("universe exceeds schedule record limit")


if __name__ == "__main__":
    raise SystemExit(main())
