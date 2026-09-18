from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from findb_fetcher import shioaji_scheduler_cli as cli
from findb_fetcher.shioaji_scheduler import SchedulerRun
from findb_fetcher.shioaji_staging import Result

MANIFEST = Path(__file__).resolve().parents[1] / "configs" / "shioaji_tw_pilot.v1.json"


def test_parser_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        cli.build_parser().parse_args(["--check", "--run-forever"])
    with pytest.raises(ValueError):
        cli.build_parser().parse_args(["--initialize-state", "--require-stopped"])
    with pytest.raises(ValueError):
        cli.build_parser().parse_args(["--as-of", "2026-08-03T06:30:00Z"])


def test_check_is_offline_and_secret_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "validate_production_runtime",
        lambda: (SimpleNamespace(contracts_dir=tmp_path), object(), object()),
    )
    monkeypatch.setattr(cli, "validate_contracts", lambda _path: object())
    monkeypatch.setattr(
        cli,
        "open_production_state",
        lambda _path: (_ for _ in ()).throw(AssertionError("check opened state")),
    )
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "never-output")
    result = cli.main(
        ["--check", "--manifest", str(MANIFEST), "--state-path", str(tmp_path / "state.sqlite3")]
    )
    assert result == cli.EXIT_OK
    value = json.loads(capsys.readouterr().out)
    assert value == {"code": "CHECK_OK", "stage": "manifest", "count": 4}
    assert "never-output" not in json.dumps(value)


def test_require_stopped_rejects_missing_state_without_control_or_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "missing" / "state.sqlite3"
    monkeypatch.setattr(
        cli,
        "require_scheduler_stopped",
        lambda *_args, **_kwargs: pytest.fail("missing state reached scheduler control"),
    )

    result = cli.main(
        [
            "--require-stopped",
            "--manifest",
            str(MANIFEST),
            "--state-path",
            str(state_path),
        ]
    )

    assert result == cli.EXIT_CONFIG_ERROR
    assert not state_path.exists()
    assert not state_path.parent.exists()
    assert json.loads(capsys.readouterr().out) == {
        "code": "SAFE_FAILURE",
        "stage": "setup",
        "count": 0,
    }


def test_initialize_state_is_offline_idempotent_and_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "production" / "state.sqlite3"
    monkeypatch.setattr(
        cli,
        "validate_production_runtime",
        lambda: pytest.fail("state bootstrap validated runtime secrets"),
    )
    monkeypatch.setattr(
        cli,
        "validate_contracts",
        lambda _path: pytest.fail("state bootstrap validated runtime contracts"),
    )
    monkeypatch.setattr(
        cli,
        "require_scheduler_stopped",
        lambda *_args, **_kwargs: pytest.fail("state bootstrap reached scheduler control"),
    )

    for _ in range(2):
        result = cli.main(
            [
                "--initialize-state",
                "--manifest",
                str(MANIFEST),
                "--state-path",
                str(state_path),
            ]
        )
        assert result == cli.EXIT_OK
        assert json.loads(capsys.readouterr().out) == {
            "code": "STATE_READY",
            "stage": "state",
            "count": 0,
        }

    assert state_path.is_file()
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_bounded_summary_and_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    completed = SchedulerRun(
        date(2026, 8, 3),
        "completed",
        tuple(Result("completed", "source", count=1, symbol="2330") for _ in range(4)),
    )
    assert cli._emit_run(completed) == cli.EXIT_OK
    assert len(capsys.readouterr().out.encode()) <= cli.MAX_OUTPUT_BYTES

    retry = SchedulerRun(
        date(2026, 8, 3),
        "retry_pending",
        (Result("SOURCE_TERMINAL_TIMEOUT", "source", retryable=True, symbol="2330"),),
    )
    assert cli._exit_code(retry) == cli.EXIT_RETRY_PENDING
    failed = SchedulerRun(
        date(2026, 8, 3),
        "failed",
        (Result("ACQUISITION_ATTEMPTS_EXHAUSTED", "state", symbol="2330"),),
    )
    assert cli._exit_code(failed) == cli.EXIT_SCHEDULE_FAILED
