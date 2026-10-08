"""Production-only full-market entrypoint; rollout defaults remain stopped."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig, MarketCalendarConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_client import FullMarketSourceClient
from findb_fetcher.full_market_runtime import FullMarketRuntime
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.market_calendar import PublishedCalendarClient
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig
from findb_fetcher.scheduler_control import SchedulerControlClient, scheduler_stop_event

_SCOPES = {
    "twelve_data": {"us_equity_eod", "hk_equity_eod"},
    "finlab": {"tw_equity_eod", "tw_etf_eod"},
    "shioaji": {"tw_equity_minute", "tw_etf_minute"},
    "taifex": {"tw_futures_eod"},
}


def load_config(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 65536:
        raise ValueError("full-market config exceeds bound")
    value = json.loads(path.read_bytes())
    if (
        set(value) != {"version", "deployment_target", "desired_state", "poll_seconds", "feeds"}
        or value["version"] != 1
        or value["deployment_target"] != "production"
        or value["desired_state"] != "stopped"
        or type(value["poll_seconds"]) is not int
        or not 1 <= value["poll_seconds"] <= 30
    ):
        raise ValueError("full-market rollout config must be production/stopped v1")
    actual = set()
    for feed in value["feeds"]:
        if set(feed) != {
            "provider",
            "dataset_key",
            "market",
            "asset_class",
            "source_timezone",
            "scheduled_time",
            "target_date_lag_days",
            "completion_window_seconds",
            "slot_id",
        }:
            raise ValueError("full-market feed shape invalid")
        pair = (feed["provider"], feed["dataset_key"])
        if pair in actual:
            raise ValueError("full-market feed duplicated")
        actual.add(pair)
        dataset = feed["dataset_key"]
        expected_market = (
            "US" if dataset.startswith("us_") else "HK" if dataset.startswith("hk_") else "TW"
        )
        expected_asset = (
            "future" if dataset == "tw_futures_eod" else "etf" if "_etf_" in dataset else "equity"
        )
        if (
            feed["market"] != expected_market
            or feed["asset_class"] != expected_asset
            or feed["slot_id"]
            not in {
                "western_markets_window",
                "global_markets_window",
                "taiwan_market_window",
                "asia_pacific_markets_window",
            }
        ):
            raise ValueError("full-market feed identity differs from governed scope")
        ZoneInfo(feed["source_timezone"])
        clock_time.fromisoformat(feed["scheduled_time"])
        if feed["target_date_lag_days"] != 0 or feed["completion_window_seconds"] < 1:
            raise ValueError("full-market feed clock invalid")
    expected = {
        (provider, dataset) for provider, datasets in _SCOPES.items() for dataset in datasets
    }
    if actual != expected:
        raise ValueError("full-market coverage must contain all seven feeds")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("/app/configs/full_market.production.v1.json")
    )
    parser.add_argument("--provider", required=True, choices=sorted(_SCOPES))
    parser.add_argument(
        "--state-path", type=Path, default=Path("/var/lib/findb-full-market/state.sqlite3")
    )
    parser.add_argument("--readiness-file", type=Path)
    modes = parser.add_mutually_exclusive_group()
    for mode in (
        "check",
        "require-stopped",
        "initialize-state",
        "run-forever",
        "once",
        "sync-universes",
        "health",
    ):
        modes.add_argument("--" + mode, action="store_true")
    args = parser.parse_args(argv)
    runtime = None
    source = None
    delivery = None
    calendar = None
    try:
        config = load_config(args.config)
        if args.check:
            print(
                json.dumps(
                    {"status": "valid", "desired_state": "stopped", "provider": args.provider}
                )
            )
            return 0
        state = FullMarketState(args.state_path)
        if args.initialize_state or args.health:
            print(json.dumps(state.health(args.provider), separators=(",", ":")))
            return 0
        fetcher = FetcherConfig.from_env()
        scheduler_key = f"full_market_{args.provider}_v1"
        with SchedulerControlClient(fetcher, scheduler_key) as control:
            observed = control.poll(observed_state="stopped")
            if (
                observed.provider != args.provider
                or set(observed.dataset_keys) != _SCOPES[args.provider]
            ):
                raise ValueError("full-market control scope mismatch")
            if args.require_stopped:
                if observed.desired_state != "stopped":
                    raise ValueError("DB full-market control must be stopped")
                print(json.dumps({"status": "stopped", "provider": args.provider}))
                return 0
            source = FullMarketSourceClient(fetcher)
            delivery = SourceAPIClient(fetcher, ContractRegistry(fetcher.contracts_dir))
            calendar = PublishedCalendarClient(MarketCalendarConfig.from_env())
            last_control_poll = 0.0
            acquisition_enabled = False

            def can_acquire() -> bool:
                nonlocal last_control_poll, acquisition_enabled
                tick = time.monotonic()
                if tick - last_control_poll >= fetcher.scheduler_control_poll_seconds:
                    current = control.poll(observed_state="running")
                    acquisition_enabled = current.desired_state == "running"
                    last_control_poll = tick
                return acquisition_enabled

            runtime = FullMarketRuntime(
                provider=args.provider,
                config=config,
                state=state,
                source=source,
                delivery=delivery,
                calendar=calendar,
                raw_store=R2RawPayloadStore(RawStorageConfig.from_env()),
                can_acquire=can_acquire if not args.sync_universes else None,
                readiness_file=args.readiness_file
                or Path(f"/var/lib/findb-full-market/readiness/{args.provider}.json"),
            )
            if args.sync_universes:
                # Publishing/activation remains an Admin-only action.
                print(json.dumps(runtime.sync_universes(), separators=(",", ":")))
                return 0
            with scheduler_stop_event() as stopped:
                while not stopped.is_set():
                    observed = control.poll(observed_state="stopped")
                    if observed.desired_state == "running":
                        try:
                            control.poll(
                                observed_state="running",
                                cycle_started_at=datetime.now(timezone.utc),
                            )
                            health = runtime.cycle()
                            print(json.dumps(health, separators=(",", ":")), flush=True)
                            control.poll(
                                observed_state="stopped",
                                cycle_completed_at=datetime.now(timezone.utc),
                            )
                        except Exception:
                            # Fixed error text avoids exposing provider/credential diagnostics.
                            control.poll(
                                observed_state="stopped", last_error="full_market_cycle_blocked"
                            )
                            print(
                                json.dumps(
                                    {
                                        "provider": args.provider,
                                        "status": "blocked",
                                        **state.health(args.provider),
                                    },
                                    separators=(",", ":"),
                                ),
                                flush=True,
                            )
                            if not args.run_forever:
                                return 2
                    else:
                        runtime.pause()
                    if not args.run_forever:
                        return 0
                    stopped.wait(config["poll_seconds"])
        return 0
    except Exception:
        print(
            "full-market startup or operation blocked; inspect approved readiness and durable state",
            file=sys.stderr,
        )
        return 2
    finally:
        if runtime is not None:
            runtime.close()
        if source is not None:
            source.close()
        if delivery is not None:
            delivery.close()
        if calendar is not None:
            calendar.close()


if __name__ == "__main__":
    raise SystemExit(main())
