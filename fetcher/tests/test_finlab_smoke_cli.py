from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from datetime import date

import pytest

from findb_fetcher import finlab_smoke_cli
from findb_fetcher.providers.finlab import FinLabConfigError, FinLabDatasetTable, FinLabSdkError

TARGET_DATE = date(2026, 7, 29)
TOKEN = "token-that-must-not-appear"


class _Gateway:
    def __init__(self, table: FinLabDatasetTable | Exception) -> None:
        self.table = table
        self.calls: list[tuple[str, date, tuple[str, ...]]] = []

    def fetch_dataset(
        self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
    ) -> FinLabDatasetTable:
        self.calls.append((dataset, target_date, symbols))
        if isinstance(self.table, Exception):
            raise self.table
        return self.table


class _NoisyGateway(_Gateway):
    def fetch_dataset(
        self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
    ) -> FinLabDatasetTable:
        print(TOKEN)
        print(TOKEN, file=sys.stderr)
        return super().fetch_dataset(dataset, target_date=target_date, symbols=symbols)


class _FdNoisyGateway(_Gateway):
    def __init__(self, table: FinLabDatasetTable | Exception, *, fail: bool = False) -> None:
        super().__init__(table)
        self.fail = fail

    def fetch_dataset(
        self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
    ) -> FinLabDatasetTable:
        print(TOKEN)
        print(TOKEN, file=sys.stderr)
        os.write(1, TOKEN.encode("utf-8"))
        os.write(2, TOKEN.encode("utf-8"))
        ctypes.CDLL(None).printf(TOKEN.encode("utf-8"))
        thread = threading.Thread(target=lambda: os.write(1, TOKEN.encode("utf-8")), daemon=True)
        thread.start()
        time.sleep(0.01)
        if self.fail:
            raise FinLabSdkError(TOKEN)
        return super().fetch_dataset(dataset, target_date=target_date, symbols=symbols)


def _table(*, values: tuple[object, ...] = (2200, 237)) -> FinLabDatasetTable:
    return FinLabDatasetTable(dates=("2026-07-29",), symbols=("2330", "2317"), values=(values,))


def _configure_success(monkeypatch: pytest.MonkeyPatch, gateway: _Gateway) -> None:
    monkeypatch.setattr(finlab_smoke_cli.importlib.metadata, "version", lambda _: "1.5.7")
    monkeypatch.setattr(
        finlab_smoke_cli.FinLabSdkGateway,
        "from_env",
        classmethod(lambda cls: gateway),
    )


def test_smoke_success_emits_only_bounded_non_secret_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = _Gateway(_table())
    _configure_success(monkeypatch, gateway)

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["sdk_version"] == "1.5.7"
    assert payload["date"] == "2026-07-29"
    assert payload["symbols"] == ["2330", "2317"]
    assert payload["count"] == 2
    assert len(payload["content_sha256"]) == 64
    assert "2200" not in json.dumps(payload)


def test_smoke_discards_provider_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = _NoisyGateway(_table())
    _configure_success(monkeypatch, gateway)

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 0

    captured = capsys.readouterr()
    assert TOKEN not in captured.out
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["status"] == "ok"


def test_smoke_discards_provider_direct_fd_output_on_success(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    gateway = _FdNoisyGateway(_table())
    _configure_success(monkeypatch, gateway)

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 0

    captured = capfd.readouterr()
    assert TOKEN not in captured.out
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["status"] == "ok"


def test_smoke_discards_provider_direct_fd_output_on_failure(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    gateway = _FdNoisyGateway(_table(), fail=True)
    _configure_success(monkeypatch, gateway)

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 4

    captured = capfd.readouterr()
    assert TOKEN not in captured.out
    assert captured.err == ""
    assert captured.out == '{"code":"acquisition_failed","status":"error"}\n'


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"status":"ok","checksum":"not-a-checksum","count":2}',
        b"x" * (finlab_smoke_cli.MAX_IPC_BYTES + 1),
    ],
)
def test_smoke_rejects_malformed_or_oversized_private_ipc(payload: bytes) -> None:
    assert finlab_smoke_cli._decode_worker_result(payload) is None


def test_smoke_timeout_terminates_child_without_public_output(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    class BlockingGateway(_Gateway):
        def fetch_dataset(
            self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
        ) -> FinLabDatasetTable:
            time.sleep(5)
            return super().fetch_dataset(dataset, target_date=target_date, symbols=symbols)

    _configure_success(monkeypatch, BlockingGateway(_table()))
    monkeypatch.setattr(finlab_smoke_cli, "ACQUISITION_TIMEOUT_SECONDS", 0.01)

    started = time.monotonic()
    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 4
    assert time.monotonic() - started < 2
    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out == '{"code":"acquisition_failed","status":"error"}\n'


def test_smoke_sanitizes_isolation_setup_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure_success(monkeypatch, _Gateway(_table()))
    monkeypatch.setattr(
        finlab_smoke_cli,
        "_run_acquisition",
        lambda target_date, symbols: (_ for _ in ()).throw(RuntimeError(TOKEN)),
    )

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 4
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == '{"code":"acquisition_failed","status":"error"}\n'
    assert TOKEN not in captured.out


def test_smoke_sanitizes_child_output_setup_failure(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    _configure_success(monkeypatch, _Gateway(_table()))
    monkeypatch.setattr(
        finlab_smoke_cli,
        "_silence_child_output",
        lambda: (_ for _ in ()).throw(RuntimeError(f"path/token: {TOKEN}")),
    )

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 4
    captured = capfd.readouterr()
    assert captured.err == ""
    assert captured.out == '{"code":"acquisition_failed","status":"error"}\n'
    assert TOKEN not in captured.out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--target-date", "not-a-date"],
        ["--target-date", "2026-07-29", "--symbols", "2330,9999"],
        ["--target-date", "2026-07-29", "--symbols", "2330,2330"],
        ["--target-date", "2026-07-29", "--symbols", "2330,2317,2330"],
    ],
)
def test_smoke_rejects_arguments_without_calling_sdk(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert finlab_smoke_cli.main(argv) == finlab_smoke_cli.EXIT_ARGUMENT_ERROR
    assert json.loads(capsys.readouterr().out) == {"code": "invalid_arguments", "status": "error"}


def test_smoke_rejects_missing_token_without_provider_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(finlab_smoke_cli.importlib.metadata, "version", lambda _: "1.5.7")
    monkeypatch.setattr(
        finlab_smoke_cli.FinLabSdkGateway,
        "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(FinLabConfigError(TOKEN))),
    )

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 3
    output = capsys.readouterr().out
    assert json.loads(output) == {"code": "token_unavailable", "status": "error"}
    assert TOKEN not in output


def test_smoke_rejects_version_mismatch_without_provider_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(finlab_smoke_cli.importlib.metadata, "version", lambda _: "2.0.0")

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "code": "sdk_version_mismatch",
        "status": "error",
    }


def test_smoke_sanitizes_provider_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = _Gateway(FinLabSdkError(f"provider failed with {TOKEN}"))
    _configure_success(monkeypatch, gateway)

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 4
    output = capsys.readouterr().out
    assert json.loads(output) == {"code": "acquisition_failed", "status": "error"}
    assert TOKEN not in output


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "not-a-number"])
def test_smoke_rejects_non_finite_or_invalid_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value: object
) -> None:
    gateway = _Gateway(_table(values=(value, 237)))
    _configure_success(monkeypatch, gateway)

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 4
    assert json.loads(capsys.readouterr().out) == {
        "code": "acquisition_failed",
        "status": "error",
    }


def test_smoke_output_is_bounded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gateway = _Gateway(_table())
    _configure_success(monkeypatch, gateway)

    assert finlab_smoke_cli.main(["--target-date", "2026-07-29"]) == 0
    output = capsys.readouterr().out
    assert len(output.encode("utf-8")) <= finlab_smoke_cli.MAX_OUTPUT_BYTES
    assert set(json.loads(output)) == {
        "content_sha256",
        "count",
        "dataset",
        "date",
        "sdk_version",
        "status",
        "symbols",
    }
