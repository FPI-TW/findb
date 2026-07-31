from __future__ import annotations

import json
import sqlite3
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


def test_check_validates_without_constructing_external_clients_or_enqueuing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_check_env(monkeypatch)
    state_path = tmp_path / "state" / "scheduler.sqlite3"

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("check mode must not construct external clients")

    monkeypatch.setattr(scheduler_cli, "TwelveDataClient", unexpected)
    monkeypatch.setattr(scheduler_cli, "SourceAPIClient", unexpected)
    monkeypatch.setattr(scheduler_cli, "R2RawPayloadStore", unexpected)
    monkeypatch.setattr(scheduler_cli.SchedulerState, "enqueue_due", unexpected)

    result = scheduler_cli.main(["--check", "--state-path", str(state_path)])

    assert result == scheduler_cli.EXIT_OK
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"mode": "scheduler_check", "status": "ok"}
    assert captured.err == ""
    assert state_path.is_file()
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_check_fails_safely_when_runtime_config_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("SOURCE_API_URL", raising=False)
    state_path = tmp_path / "state.sqlite3"

    result = scheduler_cli.main(["--check", "--state-path", str(state_path)])

    assert result == scheduler_cli.EXIT_CONFIG_ERROR
    assert not state_path.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "scheduler_configuration_failed"}


def test_check_fails_when_published_calendar_configuration_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_check_env(monkeypatch)
    monkeypatch.delenv("FINDB_SERVE_BASE_URL")

    result = scheduler_cli.main(["--check", "--state-path", str(tmp_path / "state.sqlite3")])

    assert result == scheduler_cli.EXIT_CONFIG_ERROR
    assert json.loads(capsys.readouterr().err) == {"error": "scheduler_configuration_failed"}


@pytest.mark.parametrize(
    "source_url",
    [
        "not-a-url",
        "http://source.example.test",
        "https://user:password@source.example.test",
        "https://@source.example.test",
        "https://source.example.test/api/v1",
        "https://source.example.test?query=value",
        "https://source.example.test?",
        "https://source.example.test#fragment",
        "https://source.example.test#",
        "https://source.example.test:",
        "https://source.example.test%2f.evil.test",
        "https://source.example.test%00",
        "https://source.example.test%3A443",
        "https://.example",
        "https://foo_bar.example",
        "https://source.example.test:65536",
    ],
)
def test_check_rejects_unsafe_source_origins_with_generic_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_url: str,
) -> None:
    _set_check_env(monkeypatch)
    monkeypatch.setenv("SOURCE_API_URL", source_url)
    state_path = tmp_path / "state.sqlite3"

    result = scheduler_cli.main(["--check", "--state-path", str(state_path)])

    assert result == scheduler_cli.EXIT_CONFIG_ERROR
    assert not state_path.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "scheduler_configuration_failed"}


def test_check_does_not_change_existing_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_check_env(monkeypatch)
    state_path = tmp_path / "scheduler.sqlite3"
    scheduler_cli.SchedulerState(state_path)
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            """
            INSERT INTO scheduled_job (
                job_key, schedule_id, scheduled_date, universe_id,
                universe_version, symbol, canonical_symbol, exchange,
                status, attempt_count, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sentinel-job",
                "sentinel-schedule",
                "2026-07-24",
                "sentinel-universe",
                1,
                "TEST",
                "TEST",
                "NASDAQ",
                "pending",
                0,
                "2026-07-24T00:00:00+00:00",
                "2026-07-24T00:00:00+00:00",
                "2026-07-24T00:00:00+00:00",
            ),
        )
        connection.commit()
        before = connection.execute(
            "SELECT job_key, status, attempt_count, updated_at FROM scheduled_job"
        ).fetchall()

    result = scheduler_cli.main(["--check", "--state-path", str(state_path)])

    assert result == scheduler_cli.EXIT_OK
    with sqlite3.connect(state_path) as connection:
        after = connection.execute(
            "SELECT job_key, status, attempt_count, updated_at FROM scheduled_job"
        ).fetchall()
    assert after == before


def test_check_rejects_malformed_versioned_state_with_generic_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_check_env(monkeypatch)
    state_path = tmp_path / "malformed.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute("PRAGMA user_version = 1")

    result = scheduler_cli.main(["--check", "--state-path", str(state_path)])

    assert result == scheduler_cli.EXIT_CONFIG_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "scheduler_configuration_failed"}


def test_check_rejects_corrupt_state_with_generic_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_check_env(monkeypatch)
    state_path = tmp_path / "corrupt.sqlite3"
    state_path.write_bytes(b"not a sqlite database")

    result = scheduler_cli.main(["--check", "--state-path", str(state_path)])

    assert result == scheduler_cli.EXIT_CONFIG_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "scheduler_configuration_failed"}
