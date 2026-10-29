from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from findb_fetcher import scheduler_cli
from findb_fetcher.twelve_data_scheduler import ScheduledJobResult, SchedulerRun

CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs" / "daily_scheduler.v2.json"


def _check_args(state_path: Path, *extra: str) -> list[str]:
    return [
        "--schedule-file",
        str(CONFIG_PATH),
        "--slot-id",
        "western_markets_window",
        "--dataset-key",
        "us_equity_eod",
        "--state-path",
        str(state_path),
        *extra,
    ]


def _run(status_counts: dict[str, int]) -> SchedulerRun:
    return SchedulerRun(
        schedule_id="s" * 64,
        scheduled_date=date(2026, 7, 24),
        enqueued=5,
        claimed=5,
        credits_used=5,
        status_counts=status_counts,
        results=tuple(
            ScheduledJobResult(
                symbol=f"SYM{index:02d}",
                outcome="completed",
                state_status="completed",
                attempt_count=10,
                record_count=5000,
                checkpoint_after=date(2026, 7, 24),
            )
            for index in range(5)
        ),
    )


def test_maximum_scheduler_summary_is_bounded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scheduler_cli._emit_summary(_run({"completed": 5}))

    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "completed"
    assert len(captured.out.encode()) < scheduler_cli.MAX_OUTPUT_BYTES
    assert captured.err == ""


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"completed": 3}, scheduler_cli.EXIT_OK),
        ({"completed": 2, "retry_wait": 1}, scheduler_cli.EXIT_RETRY_PENDING),
        ({"completed": 2, "pending": 1}, scheduler_cli.EXIT_RETRY_PENDING),
        ({"completed": 2, "failed": 1}, scheduler_cli.EXIT_SCHEDULE_FAILED),
    ],
)
def test_scheduler_exit_codes_are_stable(
    counts: dict[str, int],
    expected: int,
) -> None:
    assert scheduler_cli._exit_code(_run(counts)) == expected


def test_as_of_requires_timezone() -> None:
    with pytest.raises(SystemExit):
        scheduler_cli.build_parser().parse_args(["--as-of", "2026-07-25T02:00:00"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["--check", "--run-forever"],
        ["--check", "--as-of", "2026-07-25T02:00:00Z"],
        ["--run-forever", "--as-of", "2026-07-25T02:00:00Z"],
    ],
)
def test_scheduler_modes_are_mutually_exclusive(arguments: list[str]) -> None:
    with pytest.raises(SystemExit):
        scheduler_cli.build_parser().parse_args(arguments)


def test_v1_schedule_is_rejected_before_runtime_identity_selection(tmp_path: Path) -> None:
    path = tmp_path / "v1.json"
    path.write_text('{"schedule_version":1}')
    with pytest.raises(scheduler_cli.ScheduleError, match="v2 schedule keys"):
        scheduler_cli._load_selected_schedule(path, None, None)


def test_twelve_data_runtime_definition_uses_0815_without_changing_slot() -> None:
    root = Path(__file__).resolve().parents[1]
    schedule = scheduler_cli._load_selected_schedule(
        root / "configs" / "daily_scheduler.v2.json",
        "western_markets_window",
        "us_equity_eod",
    )

    assert scheduler_cli._expected_definition(schedule) == (
        "twelve_data",
        ("us_equity_eod",),
        "western_markets_window",
        "08:15:00",
        "Asia/Taipei",
    )


def _set_check_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_API_URL", "https://source.example.test")
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "source-client-key")
    monkeypatch.setenv("FINDB_SERVE_BASE_URL", "https://serve.example.test")
    monkeypatch.setenv("FETCHER_CALENDAR_SERVE_API_KEY", "calendar-read-key")
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "provider-key")
    monkeypatch.setenv("CLOUDFLARE_R2_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("CLOUDFLARE_R2_RAW_BUCKET", "findb-fetcher-raw-prod")
    monkeypatch.setenv("CLOUDFLARE_R2_RAW_ACCESS_KEY_ID", "r2-access-key")
    monkeypatch.setenv("CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY", "r2-secret-key")
    monkeypatch.setenv(
        "FETCHER_CONTRACTS_DIR",
        str(Path(__file__).resolve().parents[2] / "contracts"),
    )
