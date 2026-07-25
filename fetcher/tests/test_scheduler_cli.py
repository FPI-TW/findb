from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from findb_fetcher import scheduler_cli
from findb_fetcher.twelve_data_scheduler import ScheduledJobResult, SchedulerRun


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


def test_missing_schedule_fails_without_loading_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unexpected_config() -> object:
        raise AssertionError("runtime secrets must not load after local schedule rejection")

    monkeypatch.setattr(scheduler_cli.FetcherConfig, "from_env", unexpected_config)

    result = scheduler_cli.main(
        [
            "--schedule-file",
            str(tmp_path / "missing.json"),
            "--state-path",
            str(tmp_path / "state.sqlite3"),
        ]
    )

    assert result == scheduler_cli.EXIT_CONFIG_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "scheduler_configuration_failed"}


def test_as_of_requires_timezone() -> None:
    with pytest.raises(SystemExit):
        scheduler_cli.build_parser().parse_args(["--as-of", "2026-07-25T02:00:00"])
