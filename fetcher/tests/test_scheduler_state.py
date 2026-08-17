from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from findb_fetcher.schedule import load_schedule_config
from findb_fetcher.scheduler_state import SchedulerState, SchedulerStateError
from findb_fetcher.universe import load_symbol_universe

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
SCHEDULE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks_daily.v1.json"
UNIVERSE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks.v1.json"
NOW = datetime(2026, 7, 25, 2, 5, tzinfo=timezone.utc)


def _insert_v2_job(
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


def test_v2_retryable_failure_cannot_exhaust_before_grace_deadline(
    tmp_path: Path,
) -> None:
    state = SchedulerState(tmp_path / "state.sqlite3")
    schedule = replace(
        load_schedule_config(SCHEDULE_PATH),
        schedule_version=2,
        schedule_id="twelve_data_western_markets_window_us_equity_eod",
        slot_id="western_markets_window",
        timezone_name="Asia/Taipei",
        scheduled_local_time=time(10, 30),
        target_date_lag_days=1,
        grace_seconds=3600,
        max_attempts=1,
        retry_base_seconds=1,
    )
    universe = load_symbol_universe(UNIVERSE_PATH)
    before_grace = datetime(2026, 7, 25, 3, 29, tzinfo=timezone.utc)
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

    after_grace = datetime(2026, 7, 25, 3, 31, tzinfo=timezone.utc)
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


def test_v1_migration_preserves_jobs_leases_retries_requests_and_checkpoints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v1.sqlite3"
    prepared = '{"idempotency_key":"stable","payload":{"data":[]}}'
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE scheduled_job (
                job_key TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                scheduled_date TEXT NOT NULL,
                universe_id TEXT NOT NULL,
                universe_version INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                canonical_symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                next_attempt_at TEXT,
                lease_until TEXT,
                last_outcome TEXT,
                record_count INTEGER,
                checkpoint_before TEXT,
                checkpoint_after TEXT,
                attempt_id TEXT,
                run_id TEXT,
                request_key TEXT,
                idempotency_key TEXT,
                prepared_request TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE symbol_checkpoint (
                universe_id TEXT NOT NULL,
                universe_version INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (universe_id, universe_version, symbol)
            );
            PRAGMA user_version = 1;
        """)
        connection.execute(
            """
            INSERT INTO scheduled_job (
                job_key, schedule_id, scheduled_date, universe_id,
                universe_version, symbol, canonical_symbol, exchange, status,
                attempt_count, next_attempt_at, lease_until, last_outcome,
                checkpoint_before, request_key, idempotency_key,
                prepared_request, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-job",
                "twelve_data_us_common_stocks_daily_v1",
                "2026-07-25",
                "twelve_data_us_common_stocks_v1",
                1,
                "AAPL",
                "AAPL",
                "NASDAQ",
                "retry_wait",
                2,
                "2026-07-25T02:06:00Z",
                "2026-07-25T02:45:00Z",
                "delivery_failed",
                "2026-07-24",
                "request-key",
                "idempotency-key",
                prepared,
                "2026-07-25T02:00:00Z",
                "2026-07-25T02:05:00Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO symbol_checkpoint (
                universe_id, universe_version, symbol, trade_date, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "twelve_data_us_common_stocks_v1",
                1,
                "AAPL",
                "2026-07-24",
                "2026-07-25T02:05:00Z",
            ),
        )
        connection.commit()

    state = SchedulerState(path)

    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        migrated = connection.execute(
            """
            SELECT slot_id, provider, dataset_key, work_item, target_data_date,
                   status, attempt_count, next_attempt_at, lease_until,
                   last_outcome, checkpoint_before, request_key,
                   idempotency_key, prepared_request
            FROM scheduled_job
            """
        ).fetchone()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert dict(migrated) == {
        "slot_id": "legacy",
        "provider": "twelve_data",
        "dataset_key": "us_equity_eod",
        "work_item": "AAPL",
        "target_data_date": "2026-07-25",
        "status": "retry_wait",
        "attempt_count": 2,
        "next_attempt_at": "2026-07-25T02:06:00Z",
        "lease_until": "2026-07-25T02:45:00Z",
        "last_outcome": "delivery_failed",
        "checkpoint_before": "2026-07-24",
        "request_key": "request-key",
        "idempotency_key": "idempotency-key",
        "prepared_request": prepared,
    }
    universe = load_symbol_universe(UNIVERSE_PATH)
    assert state.checkpoint(universe, "AAPL") == date(2026, 7, 24)
    v2_schedule = replace(
        load_schedule_config(SCHEDULE_PATH),
        schedule_version=2,
        schedule_id="twelve_data_western_markets_window_us_equity_eod",
        slot_id="western_markets_window",
        timezone_name="Asia/Taipei",
        scheduled_local_time=time(6, 30),
        target_date_lag_days=1,
        legacy_schedule_id="twelve_data_us_common_stocks_daily_v1",
    )
    assert state.enqueue_due(v2_schedule, universe, date(2026, 7, 25), now=NOW) == 2
    resumed = state.claim_due(
        v2_schedule,
        universe,
        now=NOW + timedelta(minutes=1),
        limit=3,
    )
    aapl = next(job for job in resumed if job.symbol == "AAPL")
    assert aapl.slot_id == "western_markets_window"
    assert aapl.schedule_id == "twelve_data_western_markets_window_us_equity_eod"
    assert aapl.scheduled_date == date(2026, 7, 26)
    assert aapl.target_data_date == date(2026, 7, 25)
    assert aapl.attempt_count == 3
    assert aapl.prepared_request == {
        "idempotency_key": "stable",
        "payload": {"data": []},
    }


def test_v2_slot_migration_preserves_evidence_and_canonical_enqueue_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v2.sqlite3"
    SchedulerState(path)
    with sqlite3.connect(path) as connection:
        _insert_v2_job(
            connection,
            job_key="opaque-retry-job",
            slot_id="us_0600",
            schedule_id="twelve_data_us_0600_us_equity_eod",
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()

    state = SchedulerState(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        migrated = connection.execute(
            """
            SELECT job_key, slot_id, schedule_id, status, attempt_count,
                   next_attempt_at, lease_until, last_outcome, record_count,
                   checkpoint_before, checkpoint_after, attempt_id, run_id,
                   request_key, idempotency_key, prepared_request,
                   created_at, updated_at
            FROM scheduled_job
            WHERE job_key = 'opaque-retry-job'
            """
        ).fetchone()
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert dict(migrated) == {
        "job_key": "opaque-retry-job",
        "slot_id": "western_markets_window",
        "schedule_id": "twelve_data_western_markets_window_us_equity_eod",
        "status": "retry_wait",
        "attempt_count": 3,
        "next_attempt_at": "2026-07-25T02:06:00Z",
        "lease_until": "2026-07-25T02:45:00Z",
        "last_outcome": "provider_rate_limited",
        "record_count": 7,
        "checkpoint_before": "2026-07-23",
        "checkpoint_after": "2026-07-24",
        "attempt_id": "attempt-id",
        "run_id": "run-id",
        "request_key": "request-key",
        "idempotency_key": "idempotency-key",
        "prepared_request": '{"payload":{"data":[]}}',
        "created_at": "2026-07-25T02:00:00Z",
        "updated_at": "2026-07-25T02:05:00Z",
    }

    universe = load_symbol_universe(UNIVERSE_PATH)
    schedule = replace(
        load_schedule_config(SCHEDULE_PATH),
        schedule_version=2,
        schedule_id="twelve_data_western_markets_window_us_equity_eod",
        slot_id="western_markets_window",
        timezone_name="Asia/Taipei",
        scheduled_local_time=time(6, 30),
        target_date_lag_days=1,
    )
    assert (
        state.enqueue_due(
            schedule,
            universe,
            date(2026, 7, 25),
            target_data_date=date(2026, 7, 24),
            now=NOW,
        )
        == 2
    )
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """
            SELECT work_item, target_data_date, COUNT(*)
            FROM scheduled_job
            WHERE slot_id = 'western_markets_window'
            GROUP BY work_item, target_data_date
            ORDER BY work_item
            """
        ).fetchall()
        assert rows == [
            ("AAPL", "2026-07-24", 1),
            ("MSFT", "2026-07-24", 1),
            ("NVDA", "2026-07-24", 1),
        ]
        assert connection.execute(
            "SELECT job_key, status, attempt_count FROM scheduled_job WHERE work_item = 'AAPL'"
        ).fetchone() == ("opaque-retry-job", "retry_wait", 3)


def test_legacy_state_requires_explicit_owner_for_same_provider_dataset_feed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unauthorized.sqlite3"
    state = SchedulerState(path)
    with sqlite3.connect(path) as connection:
        _insert_v2_job(
            connection,
            job_key="opaque-legacy-job",
            slot_id="legacy",
            schedule_id="twelve_data_us_common_stocks_daily_v1",
        )
        connection.commit()

    universe = load_symbol_universe(UNIVERSE_PATH)
    unauthorized_feed = replace(
        load_schedule_config(SCHEDULE_PATH),
        schedule_version=2,
        schedule_id="twelve_data_global_markets_window_us_equity_eod",
        slot_id="global_markets_window",
        timezone_name="Asia/Taipei",
        scheduled_local_time=time(8, 15),
        target_date_lag_days=0,
        legacy_schedule_id=None,
    )
    with pytest.raises(SchedulerStateError, match="does not declare ownership"):
        state.enqueue_due(
            unauthorized_feed,
            universe,
            date(2026, 7, 25),
            target_data_date=date(2026, 7, 25),
            now=NOW,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduled_job").fetchone() == (1,)
        assert connection.execute(
            "SELECT job_key, slot_id, schedule_id, updated_at FROM scheduled_job"
        ).fetchone() == (
            "opaque-legacy-job",
            "legacy",
            "twelve_data_us_common_stocks_daily_v1",
            "2026-07-25T02:05:00Z",
        )


def test_legacy_state_rejects_mismatched_schedule_id_without_partial_writes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mismatched.sqlite3"
    state = SchedulerState(path)
    with sqlite3.connect(path) as connection:
        _insert_v2_job(
            connection,
            job_key="opaque-mismatched-job",
            slot_id="legacy",
            schedule_id="other_legacy_schedule_v1",
        )
        connection.commit()

    universe = load_symbol_universe(UNIVERSE_PATH)
    western_feed = replace(
        load_schedule_config(SCHEDULE_PATH),
        schedule_version=2,
        schedule_id="twelve_data_western_markets_window_us_equity_eod",
        slot_id="western_markets_window",
        timezone_name="Asia/Taipei",
        scheduled_local_time=time(6, 30),
        target_date_lag_days=1,
        legacy_schedule_id="twelve_data_us_common_stocks_daily_v1",
    )
    with pytest.raises(SchedulerStateError, match="does not match"):
        state.enqueue_due(
            western_feed,
            universe,
            date(2026, 7, 25),
            target_data_date=date(2026, 7, 24),
            now=NOW,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scheduled_job").fetchone() == (1,)
        assert connection.execute(
            "SELECT job_key, slot_id, schedule_id, updated_at FROM scheduled_job"
        ).fetchone() == (
            "opaque-mismatched-job",
            "legacy",
            "other_legacy_schedule_v1",
            "2026-07-25T02:05:00Z",
        )


def test_v2_slot_migration_fails_closed_on_old_canonical_collision(tmp_path: Path) -> None:
    path = tmp_path / "collision.sqlite3"
    SchedulerState(path)
    with sqlite3.connect(path) as connection:
        _insert_v2_job(
            connection,
            job_key="opaque-old-job",
            slot_id="us_0600",
            schedule_id="twelve_data_us_0600_us_equity_eod",
        )
        _insert_v2_job(
            connection,
            job_key="opaque-canonical-job",
            slot_id="western_markets_window",
            schedule_id="twelve_data_western_markets_window_us_equity_eod",
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()

    with pytest.raises(SchedulerStateError, match="logical-job collision"):
        SchedulerState(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT slot_id, schedule_id, job_key FROM scheduled_job ORDER BY job_key"
        ).fetchall() == [
            (
                "western_markets_window",
                "twelve_data_western_markets_window_us_equity_eod",
                "opaque-canonical-job",
            ),
            ("us_0600", "twelve_data_us_0600_us_equity_eod", "opaque-old-job"),
        ]


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
