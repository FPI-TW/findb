from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from findb_fetcher.schedule import load_schedule_config
from findb_fetcher.scheduler_state import SchedulerState, SchedulerStateError
from findb_fetcher.universe import load_symbol_universe

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
SCHEDULE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks_daily.v1.json"
UNIVERSE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks.v1.json"
NOW = datetime(2026, 7, 25, 2, 5, tzinfo=timezone.utc)


def test_state_persists_jobs_retries_and_checkpoint(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "scheduler.sqlite3"
    state = SchedulerState(state_path)
    schedule = load_schedule_config(SCHEDULE_PATH)
    universe = load_symbol_universe(UNIVERSE_PATH)

    assert state.enqueue_due(schedule, universe, date(2026, 7, 25), now=NOW) == 3
    assert state.enqueue_due(schedule, universe, date(2026, 7, 25), now=NOW) == 0
    jobs = state.claim_due(schedule, universe, now=NOW, limit=3)
    assert [job.symbol for job in jobs] == ["AAPL", "MSFT", "NVDA"]
    assert all(job.attempt_count == 1 for job in jobs)

    state.save_prepared_request(
        jobs[0],
        {
            "request_key": "completed-request",
            "idempotency_key": "completed-idempotency",
            "payload": {"data": [{"symbol": "AAPL"}]},
        },
        now=NOW,
    )
    state.complete(
        jobs[0],
        now=NOW,
        outcome="completed",
        record_count=4,
        checkpoint_after=date(2026, 7, 24),
    )
    state.save_prepared_request(
        jobs[1],
        {
            "request_key": "stable-request",
            "idempotency_key": "stable-idempotency",
            "payload": {"data": []},
        },
        now=NOW,
    )
    state.save_prepared_request(
        jobs[2],
        {
            "request_key": "failed-request",
            "idempotency_key": "failed-idempotency",
            "payload": {"data": [{"symbol": "NVDA"}]},
        },
        now=NOW,
    )
    assert (
        state.record_failure(
            jobs[1],
            schedule,
            now=NOW,
            outcome="provider_rate_limited",
            retryable=True,
        )
        == "retry_wait"
    )
    assert (
        state.record_failure(
            jobs[2],
            schedule,
            now=NOW,
            outcome="mapping_failed",
            retryable=False,
        )
        == "failed"
    )

    reopened = SchedulerState(state_path)
    assert reopened.checkpoint(universe, "AAPL") == date(2026, 7, 24)
    assert reopened.checkpoint(universe, "MSFT") is None
    assert reopened.status_counts(schedule.schedule_id, date(2026, 7, 25)) == {
        "completed": 1,
        "failed": 1,
        "retry_wait": 1,
    }
    with sqlite3.connect(state_path) as connection:
        retained = dict(
            connection.execute("SELECT symbol, prepared_request FROM scheduled_job").fetchall()
        )
    assert retained["AAPL"] is None
    assert retained["NVDA"] is None
    assert retained["MSFT"] is not None
    assert reopened.claim_due(schedule, universe, now=NOW, limit=3) == ()

    retried = reopened.claim_due(
        schedule,
        universe,
        now=NOW + timedelta(seconds=60),
        limit=3,
    )
    assert len(retried) == 1
    assert retried[0].symbol == "MSFT"
    assert retried[0].attempt_count == 2
    assert retried[0].prepared_request == {
        "request_key": "stable-request",
        "idempotency_key": "stable-idempotency",
        "payload": {"data": []},
    }


def test_expired_lease_is_reclaimed_after_restart(tmp_path: Path) -> None:
    state = SchedulerState(tmp_path / "state.sqlite3")
    schedule = load_schedule_config(SCHEDULE_PATH)
    universe = load_symbol_universe(UNIVERSE_PATH)
    state.enqueue_due(schedule, universe, date(2026, 7, 25), now=NOW)

    first = state.claim_due(schedule, universe, now=NOW, limit=1)
    assert len(first) == 1
    assert state.claim_due(schedule, universe, now=NOW + timedelta(seconds=1), limit=1)

    after_lease = NOW + timedelta(seconds=schedule.lease_seconds + 1)
    reclaimed = state.claim_due(schedule, universe, now=after_lease, limit=3)
    assert any(job.symbol == first[0].symbol and job.attempt_count == 2 for job in reclaimed)


def test_retry_exhaustion_becomes_terminal_and_checkpoint_never_regresses(
    tmp_path: Path,
) -> None:
    state = SchedulerState(tmp_path / "state.sqlite3")
    schedule = replace(
        load_schedule_config(SCHEDULE_PATH),
        max_attempts=2,
        retry_base_seconds=1,
    )
    universe = load_symbol_universe(UNIVERSE_PATH)
    state.enqueue_due(schedule, universe, date(2026, 7, 25), now=NOW)
    first = state.claim_due(schedule, universe, now=NOW, limit=1)[0]
    assert (
        state.record_failure(
            first,
            schedule,
            now=NOW,
            outcome="delivery_failed",
            retryable=True,
        )
        == "retry_wait"
    )
    second = state.claim_due(schedule, universe, now=NOW + timedelta(seconds=1), limit=1)[0]
    assert (
        state.record_failure(
            second,
            schedule,
            now=NOW + timedelta(seconds=1),
            outcome="delivery_failed",
            retryable=True,
        )
        == "failed"
    )

    state.enqueue_due(schedule, universe, date(2026, 7, 26), now=NOW)
    next_jobs = state.claim_due(schedule, universe, now=NOW + timedelta(days=1), limit=3)
    aapl = next(job for job in next_jobs if job.symbol == "AAPL")
    state.complete(
        aapl,
        now=NOW + timedelta(days=1),
        outcome="completed",
        record_count=2,
        checkpoint_after=date(2026, 7, 24),
    )
    assert state.checkpoint(universe, "AAPL") == date(2026, 7, 24)

    state.enqueue_due(schedule, universe, date(2026, 7, 27), now=NOW)
    later_jobs = state.claim_due(schedule, universe, now=NOW + timedelta(days=2), limit=3)
    later_aapl = next(job for job in later_jobs if job.symbol == "AAPL")
    state.complete(
        later_aapl,
        now=NOW + timedelta(days=2),
        outcome="completed",
        record_count=1,
        checkpoint_after=date(2026, 7, 23),
    )
    assert state.checkpoint(universe, "AAPL") == date(2026, 7, 24)


def test_versioned_empty_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 1")

    with pytest.raises(SchedulerStateError, match="schema"):
        SchedulerState(path)


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP TABLE symbol_checkpoint",
        "ALTER TABLE scheduled_job DROP COLUMN completed_at",
        (
            "ALTER TABLE scheduled_job ADD COLUMN generated_symbol TEXT "
            "GENERATED ALWAYS AS (symbol) VIRTUAL"
        ),
        "DROP INDEX ix_scheduled_job_due",
        """
        DROP INDEX ix_scheduled_job_due;
        CREATE INDEX ix_scheduled_job_due
        ON scheduled_job (
            schedule_id, status DESC, next_attempt_at, scheduled_date
        );
        """,
        """
        DROP INDEX ix_scheduled_job_due;
        CREATE INDEX ix_scheduled_job_due
        ON scheduled_job (
            schedule_id COLLATE NOCASE, status, next_attempt_at, scheduled_date
        );
        """,
    ],
)
def test_malformed_versioned_schema_is_rejected(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / "malformed.sqlite3"
    SchedulerState(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(mutation)

    with pytest.raises(SchedulerStateError, match="schema|index"):
        SchedulerState(path)


def test_corrupt_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(SchedulerStateError):
        SchedulerState(path)
