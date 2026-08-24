from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from findb_fetcher.schedule import load_schedule_manifest
from findb_fetcher.scheduler_state import SchedulerState, SchedulerStateError
from findb_fetcher.universe import load_symbol_universe

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
SCHEDULE_PATH = CONFIG_DIR / "daily_scheduler.v2.json"
UNIVERSE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks.v1.json"
NOW = datetime(2026, 7, 25, 2, 5, tzinfo=timezone.utc)


def _schedule():
    return load_schedule_manifest(SCHEDULE_PATH).feeds[0]


def _insert_current_job(
    connection: sqlite3.Connection,
    *,
    job_key: str,
    slot_id: str,
    schedule_id: str,
    work_item: str = "AAPL",
    target_data_date: str = "2026-07-24",
    scheduled_date: str = "2026-07-25",
    status: str = "retry_wait",
) -> None:
    connection.execute(
        """
        INSERT INTO scheduled_job (
            job_key, slot_id, provider, dataset_key, work_item, target_data_date,
            schedule_id, scheduled_date, universe_id, universe_version, symbol,
            canonical_symbol, exchange, status, attempt_count, next_attempt_at,
            lease_until, last_outcome, record_count, checkpoint_before,
            checkpoint_after, attempt_id, run_id, request_key, idempotency_key,
            prepared_request, created_at, updated_at, completed_at
        ) VALUES (?, ?, 'twelve_data', 'us_equity_eod', ?, ?, ?, ?,
                  'twelve_data_us_common_stocks_v1', 1, ?, ?, 'NASDAQ', ?, 3,
                  '2026-07-25T02:06:00Z', '2026-07-25T02:45:00Z',
                  'provider_rate_limited', 7, '2026-07-23', '2026-07-24',
                  'attempt-id', 'run-id', 'request-key', 'idempotency-key',
                  '{"payload":{"data":[]}}', '2026-07-25T02:00:00Z',
                  '2026-07-25T02:05:00Z', NULL)
        """,
        (
            job_key,
            slot_id,
            work_item,
            target_data_date,
            schedule_id,
            scheduled_date,
            work_item,
            work_item,
            status,
        ),
    )


def test_state_persists_jobs_retries_and_checkpoint(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "scheduler.sqlite3"
    state = SchedulerState(state_path)
    schedule = _schedule()
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
    schedule = _schedule()
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
        _schedule(),
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


def test_v2_retryable_failure_cannot_exhaust_before_grace_deadline(
    tmp_path: Path,
) -> None:
    state = SchedulerState(tmp_path / "state.sqlite3")
    schedule = replace(
        _schedule(),
        schedule_version=2,
        schedule_id="twelve_data_western_markets_window_us_equity_eod",
        slot_id="western_markets_window",
        timezone_name="Asia/Taipei",
        scheduled_local_time=time(8, 15),
        target_date_lag_days=1,
        grace_seconds=3600,
        max_attempts=1,
        retry_base_seconds=1,
    )
    universe = load_symbol_universe(UNIVERSE_PATH)
    before_grace = datetime(2026, 7, 25, 1, 14, tzinfo=timezone.utc)
    state.enqueue_due(
        schedule,
        universe,
        date(2026, 7, 25),
        target_data_date=date(2026, 7, 24),
        now=before_grace,
    )
    first = state.claim_due(schedule, universe, now=before_grace, limit=1)[0]

    assert (
        state.record_failure(
            first,
            schedule,
            now=before_grace,
            outcome="target_not_ready",
            retryable=True,
        )
        == "retry_wait"
    )

    after_grace = datetime(2026, 7, 25, 1, 16, tzinfo=timezone.utc)
    second = state.claim_due(schedule, universe, now=after_grace, limit=1)[0]
    assert (
        state.record_failure(
            second,
            schedule,
            now=after_grace,
            outcome="target_not_ready",
            retryable=True,
        )
        == "failed"
    )


@pytest.mark.parametrize("version", (1, 2))
def test_unsupported_scheduler_state_versions_fail_closed_without_mutation(
    tmp_path: Path,
    version: int,
) -> None:
    path = tmp_path / f"unsupported-{version}.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()

    with pytest.raises(SchedulerStateError, match="unsupported scheduler state schema"):
        SchedulerState(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version


@pytest.mark.parametrize("version", (1, 2))
def test_empty_unsupported_scheduler_state_versions_fail_closed(
    tmp_path: Path,
    version: int,
) -> None:
    path = tmp_path / f"empty-{version}.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA user_version = {version}")
    before = path.read_bytes()

    with pytest.raises(SchedulerStateError, match="unsupported scheduler state schema"):
        SchedulerState(path)

    assert path.read_bytes() == before


@pytest.mark.parametrize("slot_id", ("legacy", "us_0600"))
def test_non_current_slot_state_fails_closed_without_mutation(
    tmp_path: Path,
    slot_id: str,
) -> None:
    path = tmp_path / f"{slot_id}.sqlite3"
    SchedulerState(path)
    with sqlite3.connect(path) as connection:
        _insert_current_job(
            connection,
            job_key=f"opaque-{slot_id}-job",
            slot_id=slot_id,
            schedule_id=f"twelve_data_{slot_id}_us_equity_eod",
        )
        connection.commit()
    before = path.read_bytes()

    with pytest.raises(SchedulerStateError, match="non-current slot identity"):
        SchedulerState(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT slot_id FROM scheduled_job").fetchone()[0] == slot_id


def test_incompatible_current_schedule_identity_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "incompatible.sqlite3"
    SchedulerState(path)
    with sqlite3.connect(path) as connection:
        _insert_current_job(
            connection,
            job_key="opaque-incompatible-job",
            slot_id="western_markets_window",
            schedule_id="other_schedule",
        )
        connection.commit()
    before = path.read_bytes()

    with pytest.raises(SchedulerStateError, match="schedule identity is incompatible"):
        SchedulerState(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT schedule_id FROM scheduled_job").fetchone()[0]
            == "other_schedule"
        )


def test_malformed_scheduled_date_fails_closed_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "malformed-scheduled-date.sqlite3"
    SchedulerState(path)
    with sqlite3.connect(path) as connection:
        _insert_current_job(
            connection,
            job_key="opaque-malformed-scheduled-date-job",
            slot_id="western_markets_window",
            schedule_id="twelve_data_western_markets_window_us_equity_eod",
            scheduled_date="not-a-date",
        )
        connection.commit()
    before = path.read_bytes()

    with pytest.raises(SchedulerStateError, match="scheduled date is invalid"):
        SchedulerState(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT scheduled_date FROM scheduled_job").fetchone()[0]
            == "not-a-date"
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "CREATE TABLE unexpected (value TEXT)",
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
