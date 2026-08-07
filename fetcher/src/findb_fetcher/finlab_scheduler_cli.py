"""CLI for the isolated, durable FinLab Taiwan pilot scheduler."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import ConfigError, FetcherConfig, MarketCalendarConfig
from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.finlab_scheduler import FinLabSchedulerService
from findb_fetcher.finlab_universe import (
    FinLabPilotUniverse,
    FinLabUniverseError,
    load_finlab_universe,
)
from findb_fetcher.market_calendar import MarketCalendarError, PublishedCalendarClient
from findb_fetcher.providers.finlab import FinLabError, FinLabSdkGateway
from findb_fetcher.raw_storage import (
    R2RawPayloadStore,
    RawStorageConfig,
    RawStorageError,
)
from findb_fetcher.schedule import (
    ScheduleConfig,
    ScheduleError,
    load_schedule_manifest,
)
from findb_fetcher.scheduler_control import (
    SchedulerControlClient,
    SchedulerControlLoop,
    validate_scheduler_definition,
)
from findb_fetcher.scheduler_state import SchedulerState, SchedulerStateError
from findb_fetcher.twelve_data_scheduler import SchedulerRun

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_RETRY_PENDING = 8
EXIT_SCHEDULE_FAILED = 9
MAX_OUTPUT_BYTES = 2048
DEFAULT_SLOT_ID = "taiwan_market_window"
DEFAULT_DATASET_KEY = "tw_equity_eod"
SCHEDULER_CONTROL_KEY = "finlab_tw_equity_eod_v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the durable FinLab Taiwan reviewed pilot scheduler."
    )
    parser.add_argument("--schedule-file", type=Path, default=_default_schedule_file())
    parser.add_argument("--state-path", type=Path, default=_default_state_path())
    parser.add_argument(
        "--slot-id",
        default=DEFAULT_SLOT_ID,
        help="v2 manifest slot; the FinLab pilot is governed by taiwan_market_window.",
    )
    parser.add_argument(
        "--dataset-key",
        default=DEFAULT_DATASET_KEY,
        help="v2 feed dataset; the pilot is governed by tw_equity_eod.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run-forever",
        action="store_true",
        help="Poll continuously; the default runs one due cycle and exits.",
    )
    mode.add_argument(
        "--as-of",
        type=_aware_datetime,
        help="Override the UTC-aware scheduler clock for a one-shot run.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate local FinLab configuration/state without external clients or work.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        schedule = _load_selected_schedule(
            args.schedule_file,
            args.slot_id,
            args.dataset_key,
            reject_disabled=not args.run_forever,
        )
        universe = load_finlab_universe(schedule.universe_file)
        state_universe = _validate_schedule_universe(schedule, universe)

        # These constructors only validate local environment values.  They are
        # deliberately before the check return but do not create network/SDK
        # clients.  Client construction and all side effects are below it.
        fetcher_config = FetcherConfig.from_env()
        calendar_config = MarketCalendarConfig.from_env()
        registry = ContractRegistry(fetcher_config.contracts_dir)
        state = SchedulerState(args.state_path)
        if not os.getenv("FINLAB_API_TOKEN", "").strip():
            raise ConfigError("FINLAB_API_TOKEN is required for FinLab scheduler execution")

        if args.check:
            _emit_json(
                {
                    "mode": "finlab_scheduler_check",
                    "status": "ok",
                    "slot_id": schedule.slot_id,
                    "dataset_key": universe.dataset_key,
                    "work_item_count": len(state_universe.symbols),
                },
                stream=sys.stdout,
            )
            return EXIT_OK

        if args.run_forever:
            return _run_forever(
                fetcher_config,
                lambda: _run_cycle(
                    schedule=schedule,
                    universe=universe,
                    state=state,
                    registry=registry,
                    fetcher_config=fetcher_config,
                    calendar_config=calendar_config,
                ),
                expected_definition=(
                    schedule.provider,
                    (schedule.dataset_key,),
                    schedule.slot_id,
                    schedule.scheduled_local_time.isoformat(),
                    schedule.timezone_name,
                ),
            )

        raw_storage_config = RawStorageConfig.from_env()
        with ExitStack() as stack:
            calendar = stack.enter_context(PublishedCalendarClient(calendar_config))
            provider = FinLabSdkGateway.from_env()
            source = stack.enter_context(SourceAPIClient(fetcher_config, registry))
            raw_store = R2RawPayloadStore(raw_storage_config)
            service = FinLabSchedulerService(
                schedule=schedule,
                universe=universe,
                state=state,
                provider=provider,
                source=source,
                registry=registry,
                raw_store=raw_store,
                calendar=calendar,
            )
            execution = service.run_once(now=args.as_of or datetime.now(timezone.utc))
        _emit_summary(execution)
        return _exit_code(execution)
    except (
        ConfigError,
        ContractError,
        FinLabError,
        FinLabUniverseError,
        MarketCalendarError,
        RawStorageError,
        ScheduleError,
        SchedulerStateError,
        OSError,
        ValueError,
    ):
        _emit_json({"error": "finlab_scheduler_configuration_failed"}, stream=sys.stderr)
        return EXIT_CONFIG_ERROR


def _load_selected_schedule(
    path: Path,
    slot_id: str | None = DEFAULT_SLOT_ID,
    dataset_key: str | None = DEFAULT_DATASET_KEY,
    *,
    reject_disabled: bool = True,
) -> ScheduleConfig:
    manifest = load_schedule_manifest(path)
    if manifest.schedule_version != 2:
        raise ScheduleError("FinLab scheduler requires a v2 schedule manifest")
    if slot_id is None or dataset_key is None:
        raise ScheduleError("FinLab scheduler requires --slot-id and --dataset-key")
    matches = [
        feed
        for feed in manifest.feeds
        if feed.slot_id == slot_id and feed.dataset_key == dataset_key
    ]
    if len(matches) != 1:
        raise ScheduleError("requested FinLab v2 feed is missing or ambiguous")
    schedule = matches[0]
    if (
        schedule.provider != "finlab"
        or schedule.market != "TW"
        or schedule.dataset_key != DEFAULT_DATASET_KEY
    ):
        raise ScheduleError("requested v2 feed is not the governed FinLab TW pilot")
    if reject_disabled and not schedule.enabled:
        raise ScheduleError("requested FinLab slot is disabled")
    # ``enabled`` is retained as a local manifest hint only for one-shot
    # configuration checks. DB desired_state is the sole authority for a
    # long-running cycle.
    return schedule


def _validate_schedule_universe(
    schedule: ScheduleConfig,
    universe: FinLabPilotUniverse,
):
    if (
        schedule.provider != universe.provider
        or schedule.dataset_key != universe.dataset_key
        or schedule.market != universe.market
    ):
        raise ScheduleError("FinLab schedule and universe identities do not match")
    state_universe = universe.as_scheduler_universe()
    if state_universe.estimated_credits > schedule.max_credits_per_run:
        raise ScheduleError("FinLab work item exceeds schedule credit limit")
    if universe.expected_record_count != 2:
        raise ScheduleError("FinLab pilot must contain exactly two reviewed rows")
    if state_universe.limits.max_total_records_per_run > schedule.max_records_per_run:
        # The feed's configured bound is per scheduler cycle; it may be lower
        # than the generic state hard cap, but must still fit the two rows.
        if universe.expected_record_count > schedule.max_records_per_run:
            raise ScheduleError("FinLab pilot exceeds schedule record limit")
    return state_universe


def _run_forever(
    config: FetcherConfig,
    cycle_factory: Any,
    *,
    stop_event: Any = None,
    expected_definition: tuple[str, tuple[str, ...], str, str, str] | None = None,
) -> int:
    with SchedulerControlClient(config, SCHEDULER_CONTROL_KEY) as control:
        validator = None
        if expected_definition is not None:
            provider, dataset_keys, slot_id, scheduled_local_time, timezone_name = (
                expected_definition
            )

            def validator(response: Any) -> None:
                validate_scheduler_definition(
                    response,
                    provider=provider,
                    dataset_keys=dataset_keys,
                    slot_id=slot_id,
                    scheduled_local_time=scheduled_local_time,
                    timezone_name=timezone_name,
                )

        loop = (
            SchedulerControlLoop(control, definition_validator=validator)
            if validator is not None
            else SchedulerControlLoop(control)
        )
        return loop.run(cycle_factory, stop_event=stop_event)


def _run_cycle(
    *,
    schedule: ScheduleConfig,
    universe: FinLabPilotUniverse,
    state: SchedulerState,
    registry: ContractRegistry,
    fetcher_config: FetcherConfig,
    calendar_config: MarketCalendarConfig,
) -> None:
    """Construct FinLab/Source resources only after a running acknowledgement."""

    raw_storage_config = RawStorageConfig.from_env()
    with ExitStack() as stack:
        calendar = stack.enter_context(PublishedCalendarClient(calendar_config))
        provider = FinLabSdkGateway.from_env()
        source = stack.enter_context(SourceAPIClient(fetcher_config, registry))
        raw_store = R2RawPayloadStore(raw_storage_config)
        service = FinLabSchedulerService(
            schedule=schedule,
            universe=universe,
            state=state,
            provider=provider,
            source=source,
            registry=registry,
            raw_store=raw_store,
            calendar=calendar,
        )
        execution = service.run_once(now=datetime.now(timezone.utc))
    if execution.enqueued or execution.claimed:
        _emit_summary(execution)


def _exit_code(execution: SchedulerRun) -> int:
    if execution.status == "failed":
        return EXIT_SCHEDULE_FAILED
    if execution.status in {"retry_pending", "pending"}:
        return EXIT_RETRY_PENDING
    return EXIT_OK


def _emit_summary(execution: SchedulerRun) -> None:
    _emit_json(
        {
            "mode": "finlab_scheduler_once",
            "status": execution.status,
            "schedule_id": execution.schedule_id,
            "scheduled_date": execution.scheduled_date.isoformat(),
            "enqueued": execution.enqueued,
            "claimed": execution.claimed,
            "credits_used": execution.credits_used,
            "status_counts": execution.status_counts,
            "results": [
                {
                    "work_item": result.symbol,
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
    ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("FinLab scheduler summary exceeds output size limit")
    print(encoded.decode("utf-8"), file=stream)


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
    configured = os.getenv("FETCHER_FINLAB_SCHEDULE_FILE")
    if configured:
        return Path(configured)
    container_path = Path("/app/configs/daily_scheduler.v2.json")
    if container_path.is_file():
        return container_path
    return Path(__file__).resolve().parents[2] / "configs" / "daily_scheduler.v2.json"


def _default_state_path() -> Path:
    return Path(
        os.getenv("FETCHER_FINLAB_STATE_PATH", "/var/lib/findb-finlab-fetcher/state.sqlite3")
    )


if __name__ == "__main__":
    raise SystemExit(main())
