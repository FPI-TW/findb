"""CLI for fetching, validating, and optionally delivering Twelve Data EOD data."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
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
from findb_fetcher.raw_storage import (
    R2RawPayloadStore,
    RawStorageConfig,
    RawStorageError,
    attach_raw_object,
)
from findb_fetcher.twelve_data_universe import (
    UniverseExecution,
    execute_twelve_data_universe,
)
from findb_fetcher.universe import UniverseError, load_symbol_universe

EXIT_OK = 0
EXIT_LOCAL_ERROR = 2
EXIT_SOURCE_REJECTED = 3
EXIT_DELIVERY_ERROR = 4
EXIT_WAIT_TIMEOUT = 5
EXIT_TERMINAL_FAILURE = 6
EXIT_PARTIAL_FAILURE = 7

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
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--symbol")
    target.add_argument(
        "--universe-file",
        type=Path,
        help="Run the exact symbols and hard limits from a versioned universe JSON file.",
    )
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
    if args.universe_file is not None and (args.canonical_symbol or args.exchange):
        parser.error("--canonical-symbol and --exchange are single-symbol options")

    try:
        if args.universe_file is not None:
            return _run_universe(args)
        request, registry = _fetch_and_validate(args)
        if not args.deliver:
            _emit_summary(_request_summary(request, mode="dry_run", status="validated"))
            return EXIT_OK
        return _deliver(args, request, registry)
    except (
        TwelveDataError,
        ContractError,
        ConfigError,
        RawStorageError,
        UniverseError,
        ValueError,
    ):
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
    raw_object = None
    if args.deliver:
        raw_store = _raw_store_from_env()
        raw_object = raw_store.persist(
            response.raw_bytes,
            dataset_key=args.dataset_key,
            source_symbol=args.symbol,
        )
    request = build_market_eod_request(
        response,
        dataset_key=args.dataset_key,
        fetched_at=datetime.now(timezone.utc),
        requested_symbol=args.symbol,
        requested_exchange=args.exchange,
        canonical_symbol=args.canonical_symbol,
    )
    if raw_object is not None:
        attach_raw_object(request, raw_object)
    registry = ContractRegistry(args.contracts_dir)
    registry.validate("market_eod", 1, request)
    return request, registry


def _run_universe(args: argparse.Namespace) -> int:
    universe = load_symbol_universe(args.universe_file)
    if args.dataset_key != universe.dataset_key:
        raise UniverseError("CLI dataset_key must match the governed universe")
    registry = ContractRegistry(args.contracts_dir)
    deadline = time.monotonic() + args.wait_timeout_seconds if args.wait else None

    with ExitStack() as stack:
        provider = stack.enter_context(TwelveDataClient(TwelveDataConfig.from_env()))
        source: SourceAPIClient | None = None
        raw_store = None
        if args.deliver:
            source = stack.enter_context(SourceAPIClient(FetcherConfig.from_env(), registry))
            raw_store = _raw_store_from_env()
        execution = execute_twelve_data_universe(
            universe,
            provider=provider,
            registry=registry,
            start_date=args.start_date,
            end_date=args.end_date,
            outputsize=args.outputsize,
            source=source,
            raw_store=raw_store,
            wait=args.wait,
            deadline=deadline,
            poll_interval_seconds=args.poll_interval_seconds,
        )

    _emit_summary(_universe_summary(execution, args=args))
    if execution.status == "completed":
        return EXIT_OK
    _emit_error("universe_failed" if execution.succeeded_symbols == 0 else "partial_failure")
    if any(result.outcome == "wait_timeout" for result in execution.results):
        return EXIT_WAIT_TIMEOUT
    if execution.succeeded_symbols == 0 and any(
        result.outcome == "source_rejected" for result in execution.results
    ):
        return EXIT_SOURCE_REJECTED
    return EXIT_PARTIAL_FAILURE


def _raw_store_from_env() -> R2RawPayloadStore:
    return R2RawPayloadStore(RawStorageConfig.from_env())


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
        assert deadline is not None
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


def _universe_summary(
    execution: UniverseExecution,
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    mode = "universe_dry_run"
    if args.deliver:
        mode = "universe_deliver_wait" if args.wait else "universe_deliver"
    results: list[dict[str, Any]] = []
    for item in execution.results:
        result: dict[str, Any] = {
            "symbol": item.symbol,
            "outcome": item.outcome,
            "record_count": item.record_count,
        }
        if item.source_status is not None:
            result["source_status"] = item.source_status
        if item.attempt_id is not None:
            result["attempt_id"] = str(item.attempt_id)
        if item.run_id is not None:
            result["run_id"] = str(item.run_id)
        if item.total_records is not None:
            result["total_records"] = item.total_records
            result["success_records"] = item.success_records
            result["failed_records"] = item.failed_records
        results.append(result)
    return {
        "mode": mode,
        "status": execution.status,
        "universe_id": execution.universe_id,
        "universe_version": execution.universe_version,
        "dataset_key": execution.dataset_key,
        "symbols_requested": len(execution.results),
        "symbols_succeeded": execution.succeeded_symbols,
        "symbols_failed": execution.failed_symbols,
        "symbols_skipped": execution.skipped_symbols,
        "credits_budget": execution.credits_budget,
        "credits_used": execution.credits_used,
        "records_fetched": execution.records_fetched,
        "results": results,
    }


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
