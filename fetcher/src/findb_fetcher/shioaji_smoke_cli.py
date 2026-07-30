"""Credential-safe, opt-in Shioaji staging smoke command (no delivery)."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import multiprocessing
import os
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, NoReturn

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.providers.shioaji import (
    ShioajiConfigError,
    ShioajiSdkGateway,
    build_market_minute_request,
)

EXPECTED_SHIOAJI_VERSION = "1.7.1"
REVIEWED_SYMBOLS = frozenset({"2330"})
MAX_PROVIDER_WINDOW_DAYS = 31
TIMEOUT_SECONDS = 60.0
MAX_IPC_BYTES = 512
MAX_OUTPUT_BYTES = 1024


class SmokeArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SmokeArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Run one bounded Shioaji simulation Kbars acquisition.")
    parser.add_argument("--target-date", required=True, type=_parse_date)
    parser.add_argument("--symbol", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.symbol not in REVIEWED_SYMBOLS or not _within_provider_window(args.target_date):
            raise SmokeArgumentError("unsafe scope")
    except (SmokeArgumentError, ValueError, TypeError):
        return _error("invalid_arguments", 2)
    try:
        version = importlib.metadata.version("shioaji")
    except importlib.metadata.PackageNotFoundError:
        return _error("sdk_unavailable", 3)
    if version != EXPECTED_SHIOAJI_VERSION:
        return _error("sdk_version_mismatch", 3)
    try:
        ShioajiSdkGateway.from_env(simulation=True)
    except ShioajiConfigError:
        return _error("credentials_unavailable", 3)
    result = _run_child(args.target_date, args.symbol)
    if result is None:
        return _error("acquisition_failed", 4)
    output: dict[str, object] = {
        "status": "ok",
        "sdk_version": version,
        "date": args.target_date.isoformat(),
        "symbol": args.symbol,
        "count": result[1],
        "content_sha256": result[0],
    }
    if result[2] is not None:
        output["usage_bytes_delta"] = result[2]
    _emit(output)
    return 0


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target date must be ISO-8601") from exc


def _within_provider_window(target: date) -> bool:
    today = date.today()
    return today - timedelta(days=MAX_PROVIDER_WINDOW_DAYS) <= target <= today


def _run_child(target: date, symbol: str) -> tuple[str, int, int | None] | None:
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(send, target, symbol))
    try:
        process.start()
        send.close()
        if not receive.poll(TIMEOUT_SECONDS):
            return None
        return _decode(receive.recv_bytes(MAX_IPC_BYTES))
    except (EOFError, OSError, ValueError):
        return None
    finally:
        receive.close()
        send.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)


def _worker(send: Connection, target: date, symbol: str) -> None:
    try:
        _silence_output()
        gateway = ShioajiSdkGateway.from_env(simulation=True)
        # This is a local monotonic attempt count; api.usage() bytes/connections
        # are intentionally never misrepresented as contract request counts.
        snapshot = gateway.fetch_kbars(symbol, target)
        request = build_market_minute_request(
            snapshot.kbars,
            dataset_key="tw_equity_minute",
            target_date=target,
            symbols=(symbol,),
            fetched_at=_utc_now(),
            usage_before_requests=0,
            usage_after_requests=1,
            snapshot_id=f"shioaji-smoke-{target:%Y%m%d}",
            daily_update_id=f"shioaji-smoke-{target:%Y%m%d}",
            universe_id="shioaji_tw_staging_v1",
        )
        ContractRegistry(_contracts_dir()).validate("market_minute", 1, request)
        payload = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        _send(
            send,
            {
                "status": "ok",
                "checksum": __import__("hashlib").sha256(payload).hexdigest(),
                "count": len(request["payload"]["data"]),
                "usage_bytes_delta": snapshot.usage_bytes_delta,
            },
        )
    except BaseException:
        _send(send, {"status": "error"})


def _contracts_dir() -> Path:
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


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _silence_output() -> None:
    fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    finally:
        os.close(fd)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _send(send: Connection, value: dict[str, Any]) -> None:
    try:
        raw = json.dumps(value, separators=(",", ":")).encode()
        if len(raw) <= MAX_IPC_BYTES:
            send.send_bytes(raw)
    except BaseException:
        pass
    finally:
        send.close()


def _decode(raw: bytes) -> tuple[str, int, int | None] | None:
    if not isinstance(raw, bytes) or len(raw) > MAX_IPC_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {
        "status",
        "checksum",
        "count",
        "usage_bytes_delta",
    }:
        return None
    checksum, count, usage_bytes_delta = (
        value["checksum"],
        value["count"],
        value["usage_bytes_delta"],
    )
    if (
        value["status"] != "ok"
        or not isinstance(checksum, str)
        or len(checksum) != 64
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 0
        or (
            usage_bytes_delta is not None
            and (type(usage_bytes_delta) is not int or usage_bytes_delta < 0)
        )
    ):
        return None
    return checksum, count, usage_bytes_delta


def _emit(value: dict[str, object]) -> None:
    output = json.dumps(value, separators=(",", ":"))
    if len(output.encode()) > MAX_OUTPUT_BYTES:
        raise RuntimeError("output cap exceeded")
    print(output)


def _error(code: str, status: int) -> int:
    _emit({"status": "error", "code": code})
    return status
