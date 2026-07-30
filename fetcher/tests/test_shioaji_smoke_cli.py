from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from findb_fetcher import shioaji_smoke_cli
from findb_fetcher.providers.shioaji import ShioajiConfigError, ShioajiKbarsSnapshot

SECRET_MARKER = "credential-and-account-context-must-not-appear"


def _wall_clock_ns(value: datetime) -> int:
    return int((value - datetime(1970, 1, 1)).total_seconds() * 1_000_000_000)


def test_rejects_unsafe_arguments_before_sdk(capsys: pytest.CaptureFixture[str]) -> None:
    assert shioaji_smoke_cli.main(["--target-date", "2020-01-01", "--symbol", "9999"]) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "error", "code": "invalid_arguments"}


def test_rejects_sdk_version_without_provider_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shioaji_smoke_cli.importlib.metadata, "version", lambda _: "1.7.0")
    assert (
        shioaji_smoke_cli.main(["--target-date", date.today().isoformat(), "--symbol", "2330"]) == 3
    )
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "code": "sdk_version_mismatch",
    }


def test_sanitizes_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shioaji_smoke_cli.importlib.metadata, "version", lambda _: "1.7.1")
    monkeypatch.setattr(
        shioaji_smoke_cli.ShioajiSdkGateway,
        "from_env",
        classmethod(
            lambda cls, **_: (_ for _ in ()).throw(ShioajiConfigError("credential-secret"))
        ),
    )
    assert (
        shioaji_smoke_cli.main(["--target-date", date.today().isoformat(), "--symbol", "2330"]) == 3
    )
    output = capsys.readouterr().out
    assert "credential-secret" not in output
    assert json.loads(output) == {"status": "error", "code": "credentials_unavailable"}


def test_success_output_is_bounded_and_excludes_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(shioaji_smoke_cli.importlib.metadata, "version", lambda _: "1.7.1")
    monkeypatch.setattr(
        shioaji_smoke_cli.ShioajiSdkGateway, "from_env", classmethod(lambda cls, **_: object())
    )
    monkeypatch.setattr(shioaji_smoke_cli, "_run_child", lambda *_: ("a" * 64, 1, 12))
    assert (
        shioaji_smoke_cli.main(["--target-date", date.today().isoformat(), "--symbol", "2330"]) == 0
    )
    output = capsys.readouterr().out
    assert len(output.encode()) <= shioaji_smoke_cli.MAX_OUTPUT_BYTES
    assert json.loads(output)["content_sha256"] == "a" * 64
    assert json.loads(output)["usage_bytes_delta"] == 12


def test_worker_failures_and_malformed_child_messages_are_sanitized() -> None:
    assert shioaji_smoke_cli._decode(b'{"status":"error"}') is None
    assert (
        shioaji_smoke_cli._decode(b'{"status":"ok","checksum":"a","count":1,"usage_bytes_delta":0}')
        is None
    )
    assert shioaji_smoke_cli._decode(b"x" * (shioaji_smoke_cli.MAX_IPC_BYTES + 1)) is None


def test_contract_path_uses_explicit_fetcher_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FETCHER_CONTRACTS_DIR", "/safe/contracts")
    assert shioaji_smoke_cli._contracts_dir() == Path("/safe/contracts")


def test_contract_path_defaults_to_repository_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FETCHER_CONTRACTS_DIR", raising=False)
    assert shioaji_smoke_cli._contracts_dir() == Path(__file__).resolve().parents[2] / "contracts"


def test_child_timeout_terminates_then_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    class Receive:
        def poll(self, _timeout: float) -> bool:
            return False

        def close(self) -> None:
            pass

    class Send:
        def close(self) -> None:
            pass

    class Process:
        def __init__(self) -> None:
            self.actions: list[str] = []
            self.alive = True

        def start(self) -> None:
            self.actions.append("start")

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.actions.append("terminate")

        def join(self, timeout: float) -> None:
            self.actions.append(f"join:{timeout}")

        def kill(self) -> None:
            self.actions.append("kill")
            self.alive = False

    process = Process()

    class Context:
        @staticmethod
        def _pipe(*, duplex: bool) -> tuple[Receive, Send]:
            return Receive(), Send()

        @staticmethod
        def _process(**_kwargs: object) -> Process:
            return process

        Pipe = _pipe
        Process = _process

    monkeypatch.setattr(shioaji_smoke_cli.multiprocessing, "get_context", lambda _: Context())
    monkeypatch.setattr(shioaji_smoke_cli, "TIMEOUT_SECONDS", 0)
    assert shioaji_smoke_cli._run_child(date.today(), "2330") is None
    assert process.actions == ["start", "terminate", "join:1", "kill", "join:1"]


@pytest.mark.parametrize("fail", [False, True])
def test_child_discards_python_and_direct_fd_provider_output(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    fail: bool,
) -> None:
    class NoisyGateway:
        def fetch_kbars(self, _symbol: str, target: date) -> ShioajiKbarsSnapshot:
            print(SECRET_MARKER)
            print(SECRET_MARKER, file=__import__("sys").stderr)
            os.write(1, SECRET_MARKER.encode())
            os.write(2, SECRET_MARKER.encode())
            if fail:
                raise RuntimeError(SECRET_MARKER)
            timestamp = _wall_clock_ns(
                datetime.combine(target, datetime.min.time()).replace(hour=9)
            )
            return ShioajiKbarsSnapshot(
                {
                    "ts": (timestamp,),
                    "Open": (100,),
                    "High": (101,),
                    "Low": (99,),
                    "Close": (100,),
                    "Volume": (1,),
                    "Amount": (100,),
                },
                None,
                None,
            )

    monkeypatch.setattr(
        shioaji_smoke_cli.ShioajiSdkGateway,
        "from_env",
        classmethod(lambda cls, **_: NoisyGateway()),
    )
    monkeypatch.setenv(
        "FETCHER_CONTRACTS_DIR",
        str(Path(__file__).resolve().parents[2] / "contracts"),
    )
    result = shioaji_smoke_cli._run_child(date.today(), "2330")
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert SECRET_MARKER not in captured.out + captured.err
    assert (result is None) is fail
