"""CLI for manually fetching and validating a Twelve Data EOD contract payload."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataError,
    build_market_eod_request,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Twelve Data daily prices and print a validated market_eod.v1 request."
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--dataset-key", default="us_equity_eod")
    parser.add_argument("--canonical-symbol")
    parser.add_argument("--exchange")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--outputsize", type=int)
    parser.add_argument(
        "--contracts-dir",
        type=Path,
        default=_default_contracts_dir(),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with TwelveDataClient(TwelveDataConfig.from_env()) as provider:
            response = provider.fetch_daily(
                args.symbol,
                start_date=args.start_date,
                end_date=args.end_date,
                outputsize=args.outputsize,
                exchange=args.exchange,
            )
        request = build_market_eod_request(
            response,
            dataset_key=args.dataset_key,
            fetched_at=datetime.now(timezone.utc),
            requested_symbol=args.symbol,
            requested_exchange=args.exchange,
            canonical_symbol=args.canonical_symbol,
        )
        registry = ContractRegistry(args.contracts_dir)
        registry.validate("market_eod", 1, request)
    except (TwelveDataError, ValueError) as exc:
        print(f"Fetch failed: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _default_contracts_dir() -> Path:
    configured = os.getenv("FETCHER_CONTRACTS_DIR")
    if configured:
        return Path(configured)
    container_path = Path("/app/contracts")
    if container_path.is_dir():
        return container_path
    return Path.cwd() / "contracts"


if __name__ == "__main__":
    raise SystemExit(main())
