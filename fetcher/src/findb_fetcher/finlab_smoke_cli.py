"""Bounded, acquisition-only FinLab staging smoke command.

This command intentionally has no Source, R2, scheduler, or contract
dependencies.  Its only side effect is the single FinLab SDK read needed to
prove that the deployment environment can use its token.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
import sys
import time
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import NoReturn

from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabDatasetTable,
    FinLabSdkGateway,
)

EXPECTED_FINLAB_VERSION = "1.5.7"
SMOKE_DATASET = "price:收盤價"
REVIEWED_SYMBOLS = frozenset({"2330", "2317"})
DEFAULT_SYMBOLS = ("2330", "2317")
MAX_SYMBOLS = len(REVIEWED_SYMBOLS)

EXIT_OK = 0
EXIT_ARGUMENT_ERROR = 2
EXIT_RUNTIME_ERROR = 3
EXIT_ACQUISITION_ERROR = 4
MAX_OUTPUT_BYTES = 1024
MAX_IPC_BYTES = 512
# A fresh FinLab cache can require its initial metadata/data setup.  Five
# minutes remains a hard one-shot staging cap while avoiding a predictable
# first-run failure on a cold remote mount.
ACQUISITION_TIMEOUT_SECONDS = 300.0


class SmokeArgumentError(ValueError):
    """The invocation falls outside the reviewed smoke-test envelope."""


class _SmokeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SmokeArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _SmokeParser(description="Run the bounded, acquisition-only FinLab token smoke test.")
    parser.add_argument("--target-date", required=True, type=_parse_date)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated reviewed symbols; only 2330 and 2317 are allowed.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        symbols = _parse_symbols(args.symbols)
    except (SmokeArgumentError, ValueError, TypeError):
        _emit_error("invalid_arguments")
        return EXIT_ARGUMENT_ERROR

    try:
        version = importlib.metadata.version("finlab")
    except importlib.metadata.PackageNotFoundError:
        _emit_error("sdk_unavailable")
        return EXIT_RUNTIME_ERROR
    if version != EXPECTED_FINLAB_VERSION:
        _emit_error("sdk_version_mismatch")
        return EXIT_RUNTIME_ERROR

    try:
        # Validate the credential before forking, but leave SDK import/login and
        # all provider work to the isolated child process.
        FinLabSdkGateway.from_env()
    except FinLabConfigError:
        _emit_error("token_unavailable")
        return EXIT_RUNTIME_ERROR

    try:
        result = _run_acquisition(args.target_date, symbols)
    except Exception:
        # Fork/Pipe/process cleanup errors must not escape the bounded CLI.
        result = None
    if result is None:
        _emit_error("acquisition_failed")
        return EXIT_ACQUISITION_ERROR
    checksum, count = result

    _emit(
        {
            "content_sha256": checksum,
            "count": count,
            "dataset": SMOKE_DATASET,
            "date": args.target_date.isoformat(),
            "sdk_version": version,
            "status": "ok",
            "symbols": list(symbols),
        }
    )
    return EXIT_OK


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("target date must be ISO-8601") from exc


def _parse_symbols(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise SmokeArgumentError("symbols must be a string")
    symbols = tuple(value.split(","))
    if (
        not symbols
        or len(symbols) > MAX_SYMBOLS
        or any(not symbol or symbol.strip() != symbol for symbol in symbols)
        or len(set(symbols)) != len(symbols)
        or not set(symbols).issubset(REVIEWED_SYMBOLS)
    ):
        raise SmokeArgumentError("symbols are outside reviewed scope")
    return symbols


def _validate_and_checksum(
    table: FinLabDatasetTable,
    target_date: date,
    symbols: tuple[str, ...],
) -> tuple[str, int]:
    if not isinstance(table, FinLabDatasetTable):
        raise ValueError("invalid table")
    if table.dates != (target_date.isoformat(),) or table.symbols != symbols:
        raise ValueError("unexpected table axes")
    if len(table.values) != 1 or len(table.values[0]) != len(symbols):
        raise ValueError("unexpected table dimensions")

    normalized_values: list[str] = []
    for value in table.values[0]:
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("non-numeric value") from exc
        if not numeric.is_finite() or not math.isfinite(float(numeric)):
            raise ValueError("non-finite value")
        normalized_values.append(format(numeric.normalize(), "f"))

    content = json.dumps(
        {
            "dataset": SMOKE_DATASET,
            "date": target_date.isoformat(),
            "symbols": symbols,
            "values": normalized_values,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest(), len(symbols)


def _emit_error(code: str) -> None:
    _emit({"code": code, "status": "error"})


def _emit(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
        # All call sites are static/bounded.  Fail closed if this changes later.
        raise RuntimeError("smoke output exceeds its hard cap")
    print(encoded, file=sys.stdout)


def _run_acquisition(target_date: date, symbols: tuple[str, ...]) -> tuple[str, int] | None:
    """Run all provider code in a short-lived child with a private result pipe."""

    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_acquisition_worker, args=(send, target_date, symbols))
    started = time.monotonic()
    try:
        process.start()
        send.close()
        remaining = max(0.0, ACQUISITION_TIMEOUT_SECONDS - (time.monotonic() - started))
        if not receive.poll(remaining):
            return None
        try:
            return _decode_worker_result(receive.recv_bytes(MAX_IPC_BYTES))
        except (EOFError, OSError, ValueError):
            return None
    finally:
        receive.close()
        send.close()
        _terminate_process(process)


def _acquisition_worker(
    send: Connection,
    target_date: date,
    symbols: tuple[str, ...],
) -> None:
    """Fetch in a child whose public output remains null until it exits."""

    try:
        _silence_child_output()
    except BaseException:
        _send_worker_result(send, {"status": "error"})
        return

    result: dict[str, object] = {"status": "error"}
    try:
        gateway = FinLabSdkGateway.from_env()
        table = gateway.fetch_dataset(SMOKE_DATASET, target_date=target_date, symbols=symbols)
        checksum, count = _validate_and_checksum(table, target_date, symbols)
        result = {"checksum": checksum, "count": count, "status": "ok"}
    except BaseException:
        # SDK exceptions can expose request/token context.  Never serialize them.
        result = {"status": "error"}
    _send_worker_result(send, result)


def _send_worker_result(send: Connection, result: dict[str, object]) -> None:
    """Best-effort static private IPC; never write an error to public streams."""

    try:
        encoded = json.dumps(result, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= MAX_IPC_BYTES:
            send.send_bytes(encoded)
    except BaseException:
        pass
    finally:
        send.close()


def _silence_child_output() -> None:
    """Make Python, libc, and fd writes point at null for child lifetime."""

    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
    finally:
        os.close(null_fd)
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _decode_worker_result(payload: bytes) -> tuple[str, int] | None:
    if not isinstance(payload, bytes) or len(payload) > MAX_IPC_BYTES:
        return None
    try:
        result = json.loads(payload)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(result, dict) or set(result) != {"checksum", "count", "status"}:
        return None
    checksum = result["checksum"]
    count = result["count"]
    if (
        result["status"] != "ok"
        or not isinstance(checksum, str)
        or len(checksum) != 64
        or any(character not in "0123456789abcdef" for character in checksum)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < 1
        or count > MAX_SYMBOLS
    ):
        return None
    return checksum, count


def _terminate_process(process: BaseProcess) -> None:
    if process.pid is None:
        return
    process.join(timeout=0.1)
    if process.is_alive():
        process.terminate()
        process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)
