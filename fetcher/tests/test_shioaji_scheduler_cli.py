from __future__ import annotations

from datetime import date
from pathlib import Path

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
