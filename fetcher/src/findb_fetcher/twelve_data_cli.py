"""CLI for fetching, validating, and optionally delivering Twelve Data EOD data."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from findb_fetcher.client import (
    PreparedDelivery,
    RunStatus,
    SourceAPIClient,
    SourceAPIDeadlineExceeded,
    SourceAPIProtocolError,
    SourceAPIResponseError,
    SourceAPITransportError,
)
from findb_fetcher.config import ConfigError, FetcherConfig
from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataError,
    build_market_eod_request,
)

EXIT_OK = 0
EXIT_LOCAL_ERROR = 2
EXIT_SOURCE_REJECTED = 3
EXIT_DELIVERY_ERROR = 4
EXIT_WAIT_TIMEOUT = 5
EXIT_TERMINAL_FAILURE = 6

DEFAULT_WAIT_TIMEOUT_SECONDS = 3600.0
MAX_WAIT_TIMEOUT_SECONDS = 7200.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
MAX_POLL_INTERVAL_SECONDS = 60.0
MAX_OUTPUT_BYTES = 2048
TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch and validate Twelve Data daily prices, with optional Source delivery."
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
    parser.add_argument(
        "--deliver",
        action="store_true",
        help="Deliver the validated immutable request to the FinDB Source API.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the delivered run to reach a terminal normalization status.",
    )
    parser.add_argument(
        "--wait-timeout-seconds",
        type=_wait_timeout_seconds,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help=f"Overall delivery/wait deadline (default {DEFAULT_WAIT_TIMEOUT_SECONDS:g}s).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=_poll_interval_seconds,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Run-status polling interval (default {DEFAULT_POLL_INTERVAL_SECONDS:g}s).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.wait and not args.deliver:
        parser.error("--wait requires --deliver")
    if args.wait and args.poll_interval_seconds > args.wait_timeout_seconds:
        parser.error("--poll-interval-seconds must not exceed --wait-timeout-seconds")

    try:
        request, registry = _fetch_and_validate(args)
        if not args.deliver:
            _emit_summary(_request_summary(request, mode="dry_run", status="validated"))
            return EXIT_OK
        return _deliver(args, request, registry)
    except (TwelveDataError, ContractError, ConfigError, ValueError):
        _emit_error("local_validation_failed")
        return EXIT_LOCAL_ERROR
    except SourceAPIDeadlineExceeded:
        _emit_error("wait_timeout")
        return EXIT_WAIT_TIMEOUT if args.wait else EXIT_DELIVERY_ERROR
    except SourceAPIResponseError as exc:
        _emit_error(
            "source_rejected" if _is_permanent_rejection(exc.status_code) else "delivery_failed",
            http_status=exc.status_code,
            failure_code=exc.code,
        )
        return (
            EXIT_SOURCE_REJECTED
            if _is_permanent_rejection(exc.status_code)
            else EXIT_DELIVERY_ERROR
        )
    except (SourceAPIProtocolError, SourceAPITransportError, httpx.TransportError):
        _emit_error("delivery_failed")
        return EXIT_DELIVERY_ERROR


def _fetch_and_validate(args: argparse.Namespace) -> tuple[dict[str, Any], ContractRegistry]:
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
    return request, registry


def _deliver(
    args: argparse.Namespace,
    request: Mapping[str, Any],
    registry: ContractRegistry,
) -> int:
    config = FetcherConfig.from_env()
    deadline = time.monotonic() + args.wait_timeout_seconds if args.wait else None
    with SourceAPIClient(config, registry) as source:
        prepared = source.prepare(request)
        receipt = source.deliver(prepared, deadline=deadline)
        if not args.wait:
            summary = _request_summary(request, mode="deliver", status=receipt.status)
            summary.update(
                {
                    "attempt_id": str(receipt.attempt_id),
                    "run_id": str(receipt.run_id),
                }
            )
            _emit_summary(summary)
            return EXIT_OK
        run = _wait_for_terminal(
            source,
            receipt.run_id,
            prepared,
            deadline=deadline,
            poll_interval_seconds=args.poll_interval_seconds,
        )

    summary = _request_summary(request, mode="deliver_wait", status=run.status)
    summary.update(
        {
            "attempt_id": str(receipt.attempt_id),
            "run_id": str(run.run_id),
            "total_records": run.total_records,
            "success_records": run.success_records,
            "failed_records": run.failed_records,
        }
    )
    if run.failure_code is not None:
        summary["failure_code"] = run.failure_code
    _emit_summary(summary)
    if run.status == "completed":
        return EXIT_OK
    _emit_error("terminal_failure", failure_code=run.failure_code)
    return EXIT_TERMINAL_FAILURE


def _wait_for_terminal(
    source: SourceAPIClient,
    run_id: Any,
    expected: PreparedDelivery,
    *,
    deadline: float,
    poll_interval_seconds: float,
) -> RunStatus:
    while True:
        run = source.get_run_status(run_id, expected=expected, deadline=deadline)
        if run.status in TERMINAL_STATUSES:
            return run
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceAPIDeadlineExceeded()
        time.sleep(min(poll_interval_seconds, remaining))


def _request_summary(
    request: Mapping[str, Any],
    *,
    mode: str,
    status: str,
) -> dict[str, Any]:
    payload = request.get("payload")
    batch = payload.get("batch") if isinstance(payload, Mapping) else None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    summary: dict[str, Any] = {
        "mode": mode,
        "status": status,
        "dataset_key": request.get("dataset_key"),
        "schema_id": request.get("schema_id"),
        "schema_version": request.get("schema_version"),
        "source": request.get("source"),
        "request_key": request.get("request_key"),
        "idempotency_key": request.get("idempotency_key"),
        "record_count": len(data) if isinstance(data, list) else 0,
    }
    if isinstance(batch, Mapping):
        for key in ("data_date", "delivery_mode", "coverage_start_date", "coverage_end_date"):
            value = batch.get(key)
            if value is not None:
                summary[key] = value
    return summary


def _emit_summary(summary: Mapping[str, Any]) -> None:
    _emit_bounded_json(dict(summary), stream=sys.stdout)


def _emit_error(
    category: str,
    *,
    http_status: int | None = None,
    failure_code: str | None = None,
) -> None:
    payload: dict[str, Any] = {"error": category}
    if http_status is not None:
        payload["http_status"] = http_status
    if failure_code is not None:
        payload["failure_code"] = failure_code
    _emit_bounded_json(payload, stream=sys.stderr)


def _emit_bounded_json(payload: dict[str, Any], *, stream: Any) -> None:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("CLI summary exceeds the output size limit")
    print(encoded.decode("utf-8"), file=stream)


def _is_permanent_rejection(status_code: int) -> bool:
    return 400 <= status_code < 500 and status_code != 429


def _wait_timeout_seconds(value: str) -> float:
    return _bounded_positive_float(value, "--wait-timeout-seconds", MAX_WAIT_TIMEOUT_SECONDS)


def _poll_interval_seconds(value: str) -> float:
    return _bounded_positive_float(value, "--poll-interval-seconds", MAX_POLL_INTERVAL_SECONDS)


def _bounded_positive_float(value: str, label: str, upper_bound: float) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0 or parsed > upper_bound:
        raise argparse.ArgumentTypeError(
            f"{label} must be finite and greater than 0, up to {upper_bound:g}"
        )
    return parsed


def _default_contracts_dir() -> Path:
    configured = os.getenv("FETCHER_CONTRACTS_DIR")
    if configured:
        return Path(configured)
    container_path = Path("/app/contracts")
    if container_path.is_dir():
        return container_path
    repository_path = Path(__file__).resolve().parents[3] / "contracts"
    if repository_path.is_dir():
        return repository_path
    return Path.cwd() / "contracts"


if __name__ == "__main__":
    raise SystemExit(main())
