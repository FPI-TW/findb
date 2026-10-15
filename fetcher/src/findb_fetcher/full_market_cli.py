"""Environment-bound full-market entrypoint; every rollout remains manually stopped."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig, MarketCalendarConfig, app_environment
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
        or value["deployment_target"] not in {"local", "staging", "production"}
        or value["desired_state"] != "stopped"
        or type(value["poll_seconds"]) is not int
        or not 1 <= value["poll_seconds"] <= 30
    ):
        raise ValueError("full-market rollout config must be environment-bound/stopped v1")
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
        "--execution-profile", choices=("pilot", "full_market"), default="full_market"
    )
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
    if args.execution_profile == "pilot" and args.state_path == Path(
        "/var/lib/findb-full-market/state.sqlite3"
    ):
        args.state_path = Path("/var/lib/findb-pilot/state.sqlite3")
    runtime = None
    source = None
    delivery = None
    calendar = None
    try:
        account_path = Path(
            os.getenv("FETCHER_ACCOUNT_STATE_PATH", "/var/lib/findb-account/governor.sqlite3")
        )
        if args.state_path.resolve() == account_path.resolve():
            raise ValueError("execution checkpoint must be separate from account governor")
        config = load_config(args.config)
        if config["deployment_target"] != app_environment("local"):
            raise ValueError("runtime environment identity mismatch")
        os.environ["FETCHER_CONSUMER_PROFILE"] = args.execution_profile
        os.environ["FETCHER_EXECUTION_PROFILE"] = args.execution_profile
        os.environ["FETCHER_FULL_MARKET_CONFIG"] = str(args.config)
        os.environ["FETCHER_ACCOUNT_READINESS_FILE"] = str(
            args.readiness_file
            or Path(
                os.getenv(
                    "FETCHER_ACCOUNT_READINESS_FILE",
                    f"/var/lib/findb-account/readiness/{args.provider}.json",
                )
            )
        )
        if args.check:
            print(
                json.dumps(
                    {"status": "valid", "desired_state": "stopped", "provider": args.provider}
                )
            )
            return 0
        if args.require_stopped:
            # A control probe must never construct/migrate an empty replacement
            # for a lost checkpoint. Validate existing core schema read-only
            # before loading credentials or creating a Source client.
            if (
                not args.state_path.is_file()
                or args.state_path.is_symlink()
                or args.state_path.parent.is_symlink()
            ):
                raise ValueError("full-market checkpoint must already exist")
            with closing(
                sqlite3.connect(args.state_path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as db:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("checkpoint integrity failed")
                core_columns = {
                    "full_work": "key,provider,plan_id,member_key,status,attempts,lease_until,body,body_sha256,receipt,reason,next_at",
                    "full_plan": "plan_id,provider,dataset,trade_date,body,complete,late",
                    "full_quota": "account,window,requests,bytes,blocked_until",
                    "full_cursor": "provider,dataset,trade_date",
                }
                for table, columns in core_columns.items():
                    db.execute(f"SELECT {columns} FROM {table} LIMIT 0")
                identity_table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_identity'"
                ).fetchone()
                identity = (
                    db.execute("SELECT profile FROM execution_identity WHERE id=1").fetchone()
                    if identity_table
                    else None
                )
                if identity is not None and identity[0] != args.execution_profile:
                    raise ValueError("checkpoint execution profile mismatch")
                if identity is None and args.execution_profile != "full_market":
                    raise ValueError("pilot checkpoint identity missing")
        else:
            state = FullMarketState(args.state_path)
            state.bind_profile(args.execution_profile)
        if args.initialize_state or args.health:
            print(json.dumps(state.health(args.provider), separators=(",", ":")))
            return 0
        fetcher = FetcherConfig.from_env()
        scheduler_key = f"{args.execution_profile}_{args.provider}_v1"
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
            source = FullMarketSourceClient(fetcher, execution_profile=args.execution_profile)
            delivery = SourceAPIClient(fetcher, ContractRegistry(fetcher.contracts_dir))
            calendar = PublishedCalendarClient(MarketCalendarConfig.from_env())

            def can_acquire() -> bool:
                if (
                    args.execution_profile == "full_market"
                    and os.getenv("FULL_MARKET_ENABLED", "false").lower() != "true"
                ):
                    return False
                if os.getenv("FETCHER_CONSUMER_PROFILE") == "maintenance":
                    return (
                        source.request(
                            "GET",
                            "full-market/authorization?consumer=maintenance&execution_profile="
                            + args.execution_profile,
                        ).get("acquisition_allowed")
                        is True
                    )
                current = control.poll(observed_state="running")
                authorization = source.request(
                    "GET", "full-market/authorization?consumer=" + args.execution_profile
                )
                return (
                    current.desired_state == "running"
                    and authorization.get("acquisition_allowed") is True
                )

            runtime = FullMarketRuntime(
                provider=args.provider,
                execution_profile=args.execution_profile,
                config=config,
                state=state,
                source=source,
                delivery=delivery,
                calendar=calendar,
                raw_store=R2RawPayloadStore(RawStorageConfig.from_env()),
                can_acquire=can_acquire,
                readiness_file=Path(os.environ["FETCHER_ACCOUNT_READINESS_FILE"]),
            )
            if args.sync_universes:
                os.environ["FETCHER_CONSUMER_PROFILE"] = "maintenance"
                # Publishing remains an Admin-only action.
                print(json.dumps(runtime.sync_universes(), separators=(",", ":")))
                return 0
            with scheduler_stop_event() as stopped:
                while not stopped.is_set():
                    runtime.drain()
                    observed = control.poll(observed_state="stopped")
                    if observed.desired_state == "running" and can_acquire():
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
            "full-market startup or operation blocked; inspect baseline, calendar, authentication and durable state",
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
