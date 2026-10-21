"""One pinned Shioaji login per persistent isolated provider process."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.provider_work import durable_response
from findb_fetcher.providers.shioaji import (
    MAX_ISOLATED_IPC_BYTES,
    SHIOAJI_SDK_VERSION,
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
        self._api_key, self._secret_key = api_key.strip(), secret_key.strip()
        self.simulation = simulation
        self.api: Any = None
        self._sdk: Any = None
        self._factory: Any = None
        self.authentication_attempted = False
        self.authenticated = False
        self.cleanup_failed = False
        self.contracts: dict[str, Any] = {}
        if not self._api_key or not self._secret_key:
            raise ShioajiSdkError("shioaji credentials missing", code="CREDENTIALS")
        if sdk is not None:
            # Deliberately injected offline SDK doubles, never an environment fallback.
            self.prepare_create(sdk)
            self.create()
            self.login()
            for exchange in ("TSE", "OTC"):
                self.load_group(exchange)

    def prepare_create(self, sdk: Any = None) -> None:
        if importlib.metadata.version("shioaji") != SHIOAJI_SDK_VERSION:
            raise ShioajiSdkError("shioaji SDK version unsupported", code="SDK")
        self._sdk = sdk or importlib.import_module("shioaji")
        self._factory = self._sdk.Shioaji

    def create(self) -> None:
        self.api = self._factory(simulation=self.simulation)

    def login(self) -> None:
        self.authentication_attempted = True
        self.api.login(api_key=self._api_key, secret_key=self._secret_key, subscribe_trade=False)
        self.authenticated = True

    def load_group(self, exchange: str) -> None:
        group = getattr(self.api.Contracts.Stocks, exchange)
        for contract in group:
            code = str(getattr(contract, "code", ""))
            _validate_symbol(code)
            if str(getattr(contract, "exchange", "")) != exchange or code in self.contracts:
                raise ShioajiSdkError("shioaji contract mapping invalid", code="CONTRACT")
            self.contracts[code] = contract

    def catalogue(self) -> list[dict[str, str]]:
        return [
            {"symbol": code, "exchange": str(contract.exchange), "currency": "TWD"}
            for code, contract in sorted(self.contracts.items())
        ]

    def fetch_kbars(
        self, symbol: str, target: date, *, boundary: Any = None
    ) -> ShioajiKbarsSnapshot:
        _validate_symbol(symbol)
        if symbol not in self.contracts:
            raise ShioajiSdkError("shioaji contract missing", code="CONTRACT")
        try:
            before = _usage_bytes(self.api)
            from findb_fetcher.platform_limits import SHIOAJI_DAILY_BYTES

            if before is not None and before >= SHIOAJI_DAILY_BYTES:
                raise ShioajiSdkError(
                    "shioaji daily API byte quota exhausted or unavailable", code="QUOTA"
                )
            call = self.api.kbars
            contract = self.contracts[symbol]
            day = target.isoformat()
            outcome = "error"
            try:
                raw = (
                    boundary.invoke(self.api, call, contract, day)
                    if boundary is not None
                    else call(contract, start=day, end=day)
                )
                outcome = "success"
                if boundary is not None:
                    before = boundary.before["bytes"]
            finally:
                if boundary is not None:
                    boundary.finish(self.api, outcome=outcome)
            return ShioajiKbarsSnapshot(
                _plain_kbars(_kbars_mapping(raw)),
                before,
                (boundary.after["bytes"] if boundary.after is not None else None)
                if boundary is not None
                else _usage_bytes(self.api),
            )
        except ShioajiSdkError:
            raise
        except Exception:
            raise ShioajiSdkError("shioaji acquisition failed", code="ACQUISITION") from None

    def close(self) -> None:
        if self.api is not None:
            if self.authentication_attempted:
                try:
                    self.api.logout()
                except Exception:
                    self.cleanup_failed = True
            self.api = None
            self.authenticated = False
            self.authentication_attempted = False


def _child(conn: Any, api_key: str, secret_key: str, simulation: bool) -> None:
    session = None
    metadata = None
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
        from findb_fetcher.providers.shioaji_control import ChildBoundary, cleanup_session

        metadata = None
        while True:
            try:
                command = json.loads(conn.recv_bytes(4096))
            except (EOFError, OSError):
                break
            if command == {"op": "close"}:
                break
            if command == {"op": "catalogue"}:
                # Detached cached projection only. Bootstrap is parent-governed.
                conn.send_bytes(json.dumps({"catalogue": session.catalogue()}).encode())
                continue
            if "governor" not in command:
                raise ShioajiSdkError("unguarded SDK command", code="ACQUISITION")
            metadata = command["governor"]
            boundary = ChildBoundary(conn, metadata)
            try:
                kind = metadata["kind"]
                payload: dict[str, Any] = {}
                if kind == "sdk_create":
                    session.prepare_create()
                    boundary.start()
                    session.create()
                    boundary.finish()
                elif kind == "sdk_login":
                    login_call = session.api.login
                    login_arguments = {
                        "api_key": session._api_key,
                        "secret_key": session._secret_key,
                        "subscribe_trade": False,
                    }
                    boundary.start()
                    session.authentication_attempted = True
                    login_call(**login_arguments)
                    session.authenticated = True
                    boundary.finish(session.api)
                elif kind == "sdk_contracts":
                    group_call, exchange = session.load_group, command["exchange"]
                    boundary.start(session.api)
                    group_call(exchange)
                    boundary.finish(session.api)
                elif kind == "kbars":
                    snapshot = session.fetch_kbars(
                        command["symbol"], date.fromisoformat(command["date"]), boundary=boundary
                    )
                    payload = {
                        "code": "OK",
                        "stage": "payload",
                        "kbars": {key: list(value) for key, value in snapshot.kbars.items()},
                        "before": snapshot.usage_bytes_before,
                        "after": snapshot.usage_bytes_after,
                    }
                elif kind == "cleanup":
                    boundary.start()
                    session.close()
                    boundary.finish(outcome="error" if session.cleanup_failed else "success")
                else:
                    raise ShioajiSdkError("invalid SDK operation", code="ACQUISITION")
                wire = json.dumps(
                    {"stage": "result", "actual_start": boundary.started_at, "payload": payload},
                    allow_nan=False,
                ).encode()
                bound = MAX_ISOLATED_IPC_BYTES if kind == "kbars" else _MAX_CATALOGUE_BYTES
                if len(wire) > bound:
                    raise ShioajiSdkError("shioaji IPC exceeded bound", code="PAYLOAD")
                conn.send_bytes(wire)
                # Keep the inherited lock until parent receives the durable result.
                # Timeout/EOF must clean up under this SAME open-file description.
                acknowledgement = json.loads(conn.recv_bytes(4096))
                if acknowledgement != {"stage": "ack"}:
                    cleanup_session(session, metadata, inherited_fd=boundary.fd)
                    break
            except Exception as exc:
                boundary.finish(session.api if session.authenticated else None, outcome="error")
                cleanup_session(session, metadata, inherited_fd=boundary.fd)
                try:
                    conn.send_bytes(
                        json.dumps(
                            {
                                "stage": "result",
                                "actual_start": boundary.started_at,
                                "error": "provider_boundary",
                                "quota_wait": isinstance(exc, QuotaBlockedError)
                                and exc.quota_wait
                                and boundary.started_at is None,
                            }
                        ).encode()
                    )
                except (OSError, EOFError):
                    break
            finally:
                boundary.close()
    except Exception:
        try:
            conn.send_bytes(b'{"error":"session_unavailable"}')
        except Exception:
            pass
    finally:
        if session is not None:
            if session.authentication_attempted and metadata is not None:
                cleanup_session(session, metadata)
            else:
                session.close()
        conn.close()


def _provider_environment() -> dict[str, str]:
    # Explicit launch environment: no inherited Source/cloud/other-provider secrets.
    names = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")
    values = {name: os.environ[name] for name in names if name in os.environ}
    values["PROVIDER_WORK_REQUIRED"] = os.getenv("PROVIDER_WORK_REQUIRED", "false")
    return values


class _ProviderProcess:
    def __init__(self, fd: int) -> None:
        from findb_fetcher.provider_work import inherited_fds, require_acquisition

        require_acquisition("shioaji")

        self.child = subprocess.Popen(
            [sys.executable, "-m", "findb_fetcher.providers.shioaji_session", str(fd)],
            pass_fds=(fd, *inherited_fds()),
            env=_provider_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.pid = self.child.pid

    def is_alive(self) -> bool:
        return self.child.poll() is None

    def join(self, timeout: float) -> None:
        try:
            self.child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def terminate(self) -> None:
        self.child.terminate()

    def kill(self) -> None:
        self.child.kill()

    def reap_descendants(self) -> None:
        from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

        for action in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.pid, action)
            except ProcessLookupError:
                pass
            self.join(2)
        deadline = time.monotonic() + 2
        while True:
            try:
                os.killpg(self.pid, 0)
            except ProcessLookupError:
                return
            if time.monotonic() >= deadline:
                raise ProviderWorkRecoveryBlockedError("provider descendant process group remains")
            time.sleep(0.02)


def _entry() -> None:
    from multiprocessing.connection import Connection

    conn = Connection(int(sys.argv[1]))
    credentials = json.loads(conn.recv_bytes(4096))
    _child(conn, credentials["api_key"], credentials["secret_key"], credentials["simulation"])


class PersistentIsolatedShioajiGateway:
    """Silent credential-bearing child reused until stopped or transport failure."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        simulation: bool = True,
        timeout_seconds: float = 60,
    ) -> None:
        if not api_key.strip() or not secret_key.strip():
            raise ShioajiSdkError("shioaji credentials missing", code="CREDENTIALS")
        self.timeout_seconds = timeout_seconds
        self._credential = api_key
        self._constructed = False
        self._logged_in = False
        self._contracts_loaded = False
        self._path: Path | None = None
        self._account: str | None = None
        self.parent, child = multiprocessing.Pipe()
        self.process = _ProviderProcess(child.fileno())
        child.close()
        self.parent.send_bytes(
            json.dumps(
                {
                    "api_key": api_key.strip(),
                    "secret_key": secret_key.strip(),
                    "simulation": simulation,
                }
            ).encode()
        )

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

    @durable_response("shioaji_catalogue", ())
    def catalogue(self) -> list[dict[str, str]]:
        self._bootstrap()
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

    def _run(self, kind: str, **command: Any) -> tuple[dict[str, Any], int, int]:
        from findb_fetcher.acquisition import Acquisition
        from findb_fetcher.providers.shioaji_control import parent_fetch

        try:
            with Acquisition(
                "shioaji",
                credential=self._credential,
                kind=kind,
                state_path=self._path,
                account=self._account,
                strict=True,
            ) as operation:
                self._path, self._account = operation.state.path, operation.account
                try:
                    value = parent_fetch(
                        self.parent,
                        self.process,
                        {"op": kind, **command},
                        operation,
                        timeout=self.timeout_seconds,
                        max_bytes=MAX_ISOLATED_IPC_BYTES,
                    )
                except QuotaBlockedError:
                    self._stop()
                    operation.quarantined = self.process.is_alive()
                    raise
                except Exception:
                    self._stop()
                    operation.quarantined = self.process.is_alive()
                    raise ShioajiSdkError(
                        "isolated shioaji acquisition failed", code="ACQUISITION"
                    ) from None
        except QuotaBlockedError as exc:
            from findb_fetcher.provider_work import active, enabled

            # Public/legacy SDK callers receive the established safe SDK error.
            # Only a queue consumer can use proven predispatch quota to defer
            # without spending its frozen operation failure budget.
            if enabled() and active() is not None and exc.quota_wait and not exc.dispatched:
                raise
            raise ShioajiSdkError(
                "isolated shioaji acquisition failed", code="ACQUISITION"
            ) from None
        return value, operation.requests_before, operation.requests_after

    def _bootstrap(self) -> None:
        if not self._constructed:
            self._run("sdk_create")
            self._constructed = True
        if not self._logged_in:
            self._run("sdk_login")
            self._logged_in = True
        if not self._contracts_loaded:
            for exchange in ("TSE", "OTC"):
                self._run("sdk_contracts", exchange=exchange)
            self._contracts_loaded = True

    @durable_response("shioaji_kbars", ("symbol", "target_date"))
    def fetch_kbars(self, symbol: str, target_date: date) -> ShioajiKbarsSnapshot:
        _validate_symbol(symbol)
        self._bootstrap()
        value, before, after = self._run("kbars", symbol=symbol, date=target_date.isoformat())
        value = _decode_isolated_message(json.dumps(value).encode())
        return ShioajiKbarsSnapshot(
            {key: tuple(items) for key, items in value["kbars"].items()},
            value["before"],
            value["after"],
            before,
            after,
        )

    def _stop(self) -> None:
        try:
            self.parent.send_bytes(b'{"op":"close"}')
            self.process.join(2)
        except Exception:
            pass
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(2)
        if isinstance(self.process, _ProviderProcess):
            self.process.reap_descendants()
        self.parent.close()
        if self.process.is_alive():
            from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

            raise ProviderWorkRecoveryBlockedError("provider child has not been reaped")

    def close(self) -> None:
        if self.is_alive() and self._logged_in:
            try:
                self._run("cleanup")
            finally:
                self._logged_in = False
                self._stop()
        else:
            self._stop()


if __name__ == "__main__":
    _entry()
