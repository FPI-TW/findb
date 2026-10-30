"""Long-running Fetcher entrypoint for provider historical control work."""

from __future__ import annotations

import argparse
import os
import threading
from typing import cast

from findb_fetcher.config import FetcherConfig
from findb_fetcher.factset_historical import FactSetHistoricalRunner
from findb_fetcher.historical_backfill import (
    HistoricalControlClient,
    HistoricalProviderRunner,
    run_once,
)
from findb_fetcher.historical_runtime import FinLabHistoricalRunner, TwelveDataHistoricalRunner
from findb_fetcher.scheduler_control import scheduler_stop_event
from findb_fetcher.shioaji_historical import ExecutableShioajiHistoricalRunner
from findb_fetcher.source_recovery import SourceWaitStoppedError, retry_source
from findb_fetcher.taifex_historical import TaifexHistoricalRunner


def main(argv: list[str] | None = None) -> int:
    os.environ["FETCHER_CONSUMER_PROFILE"] = "historical"
    parser = argparse.ArgumentParser(
        description="Run one provider's historical backfill control worker."
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=("twelve_data", "finlab", "shioaji", "taifex", "factset"),
    )
    parser.add_argument("--run-forever", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="Validate configuration without claiming work"
    )
    args = parser.parse_args(argv)
    if args.check:
        FetcherConfig.from_env()
        return 0
    with scheduler_stop_event() as stopper:
        runner = cast(
            HistoricalProviderRunner,
            {
                "twelve_data": TwelveDataHistoricalRunner.from_env,
                "finlab": FinLabHistoricalRunner.from_env,
                "shioaji": ExecutableShioajiHistoricalRunner.from_env,
                "taifex": TaifexHistoricalRunner.from_env,
                "factset": FactSetHistoricalRunner.from_env,
            }[args.provider](),
        )
        with HistoricalControlClient(FetcherConfig.from_env(), provider=args.provider) as control:
            try:
                run_worker(control, runner, run_forever=args.run_forever, stop_event=stopper)
            finally:
                close = getattr(runner, "close", None)
                if callable(close):
                    close()
    return 0


def run_worker(
    control: HistoricalControlClient,
    runner: HistoricalProviderRunner,
    *,
    run_forever: bool,
    stop_event: threading.Event,
) -> None:
    """Finish a leased date and its terminal report before honoring a stop.

    Do not interrupt raw persistence or delivery. An interrupted process still
    relies on the server's expiring lease and immutable delivery identity.
    """
    while not stop_event.is_set():
        try:
            worked = retry_source(
                lambda: run_once(control, runner),
                stopped=stop_event,
                run_forever=run_forever,
            )
        except SourceWaitStoppedError:
            return
        if worked:
            continue
        if not run_forever:
            return
        stop_event.wait(15)


if __name__ == "__main__":
    raise SystemExit(main())
