"""Long-running Fetcher entrypoint for provider historical control work."""

from __future__ import annotations

import argparse
import time
from typing import cast

from findb_fetcher.config import FetcherConfig
from findb_fetcher.historical_backfill import (
    HistoricalControlClient,
    HistoricalProviderRunner,
    run_once,
)
from findb_fetcher.historical_runtime import FinLabHistoricalRunner, TwelveDataHistoricalRunner
from findb_fetcher.shioaji_historical import ExecutableShioajiHistoricalRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one provider's historical backfill control worker."
    )
    parser.add_argument("--provider", required=True, choices=("twelve_data", "finlab", "shioaji"))
    parser.add_argument("--run-forever", action="store_true")
    args = parser.parse_args(argv)
    runner = cast(
        HistoricalProviderRunner,
        {
            "twelve_data": TwelveDataHistoricalRunner.from_env,
            "finlab": FinLabHistoricalRunner.from_env,
            "shioaji": ExecutableShioajiHistoricalRunner.from_env,
        }[args.provider](),
    )
    with HistoricalControlClient(FetcherConfig.from_env()) as control:
        while run_once(control, runner):
            if not args.run_forever:
                continue
        if args.run_forever:
            while True:
                time.sleep(15)
                run_once(control, runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
