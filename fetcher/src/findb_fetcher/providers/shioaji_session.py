"""One pinned Shioaji login per persistent isolated provider process."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import multiprocessing
import os
import sys
from datetime import date
from typing import Any

from findb_fetcher.providers.shioaji import (
    MAX_ISOLATED_IPC_BYTES,
    ShioajiKbarsSnapshot,
    ShioajiSdkError,
    _decode_isolated_message,
    _kbars_mapping,
    _plain_kbars,
    _usage_bytes,
    _validate_symbol,
)

_MAX_CATALOGUE_BYTES = 4 * 1024 * 1024


class ShioajiSession:
    """Child-only SDK boundary; exports detached catalogue and scalar Kbars."""

    def __init__(self, api_key: str, secret_key: str, *, simulation: bool, sdk: Any = None) -> None:
        self.api: Any = None
        self.contracts: dict[str, Any] = {}
        if not api_key or not secret_key:
            raise ShioajiSdkError("shioaji credentials missing", code="CREDENTIALS")
        try:
            if importlib.metadata.version("shioaji") != "1.7.1":
                raise ShioajiSdkError("shioaji SDK version unsupported", code="SDK")
            sdk = sdk or importlib.import_module("shioaji")
            self.api = sdk.Shioaji(simulation=simulation)
            self.api.login(api_key=api_key, secret_key=secret_key, subscribe_trade=False)
            for exchange in ("TSE", "OTC"):
                group = getattr(self.api.Contracts.Stocks, exchange)
                for contract in group:
                    code = str(getattr(contract, "code", ""))
                    _validate_symbol(code)
                    if str(getattr(contract, "exchange", "")) != exchange or code in self.contracts:
                        raise ShioajiSdkError("shioaji contract mapping invalid", code="CONTRACT")
                    self.contracts[code] = contract
        except ShioajiSdkError:
            self.close()
            raise
        except Exception:
            self.close()
            raise ShioajiSdkError("shioaji login/catalogue unavailable", code="LOGIN") from None

    def catalogue(self) -> list[dict[str, str]]:
        return [
            {"symbol": code, "exchange": str(contract.exchange), "currency": "TWD"}
            for code, contract in sorted(self.contracts.items())
        ]

    def fetch_kbars(self, symbol: str, target: date) -> ShioajiKbarsSnapshot:
        _validate_symbol(symbol)
        if symbol not in self.contracts:
            raise ShioajiSdkError("shioaji contract missing", code="CONTRACT")
        try:
            before = _usage_bytes(self.api)
            raw = self.api.kbars(
                self.contracts[symbol], start=target.isoformat(), end=target.isoformat()
            )
            return ShioajiKbarsSnapshot(
                _plain_kbars(_kbars_mapping(raw)), before, _usage_bytes(self.api)
            )
        except ShioajiSdkError:
            raise
        except Exception:
            raise ShioajiSdkError("shioaji acquisition failed", code="ACQUISITION") from None

    def close(self) -> None:
        if self.api is not None:
            try:
                self.api.logout()
            except Exception:
                pass
            self.api = None


def _child(conn: Any, api_key: str, secret_key: str, simulation: bool) -> None:
    session = None
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(null, 1)
        os.dup2(null, 2)
        os.close(null)
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
        for name in tuple(os.environ):
            if any(
                term in name.upper()
                for term in ("API_KEY", "SECRET", "TOKEN", "SOURCE_CLIENT_KEY", "ACCESS_KEY")
            ):
                os.environ.pop(name, None)
        session = ShioajiSession(api_key, secret_key, simulation=simulation)
        while True:
            command = json.loads(conn.recv_bytes(256))
            if command == {"op": "close"}:
                break
            try:
                if command == {"op": "catalogue"}:
                    wire = json.dumps(
                        {"catalogue": session.catalogue()}, separators=(",", ":")
                    ).encode()
                    if len(wire) > _MAX_CATALOGUE_BYTES:
                        raise ShioajiSdkError("shioaji catalogue exceeded bound", code="PAYLOAD")
                elif set(command) == {"op", "symbol", "date"} and command["op"] == "kbars":
                    snapshot = session.fetch_kbars(
                        command["symbol"], date.fromisoformat(command["date"])
                    )
                    wire = json.dumps(
                        {
                            "code": "OK",
                            "stage": "payload",
                            "kbars": {key: list(value) for key, value in snapshot.kbars.items()},
                            "before": snapshot.usage_bytes_before,
                            "after": snapshot.usage_bytes_after,
                        },
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode()
                    if len(wire) > MAX_ISOLATED_IPC_BYTES:
                        raise ShioajiSdkError("shioaji IPC exceeded bound", code="PAYLOAD")
                else:
                    raise ShioajiSdkError("invalid command", code="PAYLOAD")
                conn.send_bytes(wire)
            except Exception:
                conn.send_bytes(b'{"error":"provider_boundary"}')
    except Exception:
        try:
            conn.send_bytes(b'{"error":"session_unavailable"}')
        except Exception:
            pass
    finally:
        if session is not None:
            session.close()
        conn.close()


class PersistentIsolatedShioajiGateway:
    """Silent credential-bearing child reused until stopped or transport failure."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        simulation: bool = False,
        timeout_seconds: float = 60,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.parent, child = multiprocessing.Pipe()
        # Spawn avoids inheriting unrelated provider credentials or HTTP clients.
        self.process = multiprocessing.get_context("spawn").Process(
            target=_child, args=(child, api_key, secret_key, simulation), daemon=True
        )
        self.process.start()
        child.close()

    def is_alive(self) -> bool:
        return not self.parent.closed and self.process.is_alive()

    def _request(self, command: dict[str, str], max_bytes: int) -> bytes:
        try:
            if not self.is_alive():
                raise ConnectionError
            self.parent.send_bytes(json.dumps(command, separators=(",", ":")).encode())
            if not self.parent.poll(self.timeout_seconds):
                raise TimeoutError
            raw = self.parent.recv_bytes(max_bytes)
            if "error" in json.loads(raw):
                raise ValueError
            return raw
        except Exception:
            self.close()
            raise ShioajiSdkError("isolated shioaji provider failed", code="ACQUISITION") from None

    def catalogue(self) -> list[dict[str, str]]:
        value = json.loads(self._request({"op": "catalogue"}, _MAX_CATALOGUE_BYTES))
        if (
            set(value) != {"catalogue"}
            or not isinstance(value["catalogue"], list)
            or len(value["catalogue"]) > 10000
        ):
            raise ShioajiSdkError("shioaji catalogue invalid", code="PAYLOAD")
        for row in value["catalogue"]:
            if (
                not isinstance(row, dict)
                or set(row) != {"symbol", "exchange", "currency"}
                or row["exchange"] not in {"TSE", "OTC"}
                or row["currency"] != "TWD"
            ):
                raise ShioajiSdkError("shioaji catalogue invalid", code="PAYLOAD")
            _validate_symbol(row["symbol"])
        return value["catalogue"]

    def fetch_kbars(self, symbol: str, target_date: date) -> ShioajiKbarsSnapshot:
        _validate_symbol(symbol)
        raw = self._request(
            {"op": "kbars", "symbol": symbol, "date": target_date.isoformat()},
            MAX_ISOLATED_IPC_BYTES,
        )
        value = _decode_isolated_message(raw)
        return ShioajiKbarsSnapshot(
            {key: tuple(items) for key, items in value["kbars"].items()},
            value["before"],
            value["after"],
        )

    def close(self) -> None:
        try:
            self.parent.send_bytes(b'{"op":"close"}')
            self.process.join(2)
        except Exception:
            pass
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(2)
        self.parent.close()
