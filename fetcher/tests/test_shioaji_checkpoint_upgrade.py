"""Schema-v9 upgrade retains complete immutable state through a process crash."""

import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from findb_fetcher.shioaji_staging import EXPECTED, Coordinator
from findb_fetcher.shioaji_staging_state import (
    EXPECTED_COLUMNS,
    ShioajiStagingState,
    ShioajiStagingStateError,
)
from test_shioaji_staging import _Gateway, _RawStore, _single_manifest, _Source, _taipei_now


def rows(path):
    with sqlite3.connect(path) as db:
        return {
            table: sorted(db.execute(f"SELECT * FROM {table}").fetchall())
            for table in EXPECTED_COLUMNS
        }


def test_v9_upgrade_crash_reopen_preserves_rows_prepared_receipts_pins_and_real_failure_budget(
    tmp_path,
):
    generated = tmp_path / "seed.sqlite3"
    state = ShioajiStagingState(generated)
    assert (
        Coordinator(
            state,
            _single_manifest(),
            _Gateway(),
            raw_store=_RawStore(),
            source=_Source(),
            now=_taipei_now,
        )
        .run(date(2026, 7, 29), deliver=True)[0]
        .code
        == "completed"
    )
    state.db.execute("INSERT INTO meta VALUES('credential_pin','original-pin-sha256')")
    state.ensure(
        "budget",
        "2026-07-29",
        "budget-universe",
        "budget-update",
        [dict(EXPECTED[0])],
        "2026-07-30",
    )
    ok, attempt = state.acquire_attempt(
        "budget",
        "2330",
        datetime(2026, 7, 29, tzinfo=timezone.utc),
        max_requests=50,
        rolling_seconds=60,
        max_attempts=3,
    )
    assert ok
    state.finish_attempt("budget", "2330", attempt, "failed", "ACQUISITION_FAILED", retryable=True)
    state.close()
    original = rows(generated)
    original["meta"] = sorted(
        (key, "9" if key == "schema_version" else value) for key, value in original["meta"]
    )
    legacy = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(legacy) as db:
        db.executescript((Path(__file__).parent / "fixtures/shioaji_state_v9.sql").read_text())
        db.execute("DELETE FROM meta")
        for table, values in original.items():
            if values:
                db.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in values[0])})", values
                )
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sqlite3, sys
from pathlib import Path
from findb_fetcher.shioaji_staging_state import ShioajiStagingState
connect = sqlite3.connect
class Crash(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql.startswith('UPDATE meta SET value='):
            os._exit(91)
        return super().execute(sql, parameters)
sqlite3.connect = lambda *args, **kwargs: connect(*args, **{**kwargs, 'factory': Crash})
ShioajiStagingState(Path(sys.argv[1]))
""",
            str(legacy),
        ],
        env=os.environ.copy(),
        timeout=10,
    )
    assert crashed.returncode == 91
    assert rows(legacy) == original, (
        "abrupt process death must leave the complete v9 transaction recoverable"
    )
    upgraded = ShioajiStagingState(legacy)
    assert upgraded.budget_used("budget", "2330") == 1
    upgraded.close()
    expected = {
        **original,
        "meta": sorted(
            (key, "10" if key == "schema_version" else value) for key, value in original["meta"]
        ),
    }
    assert rows(legacy) == expected
    reopened = ShioajiStagingState(legacy)
    assert reopened.budget_used("budget", "2330") == 1
    for index in range(4):
        ok, audit = reopened.acquire_attempt(
            "budget",
            "2330",
            datetime(2026, 7, 30, 0, index, tzinfo=timezone.utc),
            max_requests=50,
            rolling_seconds=60,
            max_attempts=3,
        )
        assert ok
        reopened.finish_attempt("budget", "2330", audit, "waiting", "QUOTA_WAIT", retryable=True)
        assert reopened.budget_used("budget", "2330") == 1
    for index in range(2):
        ok, audit = reopened.acquire_attempt(
            "budget",
            "2330",
            datetime(2026, 7, 30, 1, index, tzinfo=timezone.utc),
            max_requests=50,
            rolling_seconds=60,
            max_attempts=3,
        )
        assert ok
        reopened.finish_attempt(
            "budget", "2330", audit, "failed", "ACQUISITION_FAILED", retryable=True
        )
    assert not reopened.acquire_attempt(
        "budget",
        "2330",
        datetime(2026, 7, 30, 2, tzinfo=timezone.utc),
        max_requests=50,
        rolling_seconds=60,
        max_attempts=3,
    )[0]
    assert (
        reopened.db.execute(
            "SELECT count(*) FROM provider_attempts WHERE daily_id='budget'"
        ).fetchone()[0]
        == 7
    )
    reopened.close()


def test_quota_wait_audit_preserves_real_rolling_allowance_and_failure_ceiling_but_unknown_scope_fails(
    tmp_path,
):
    state = ShioajiStagingState(tmp_path / "limits.sqlite3")
    state.ensure("d", "2026-07-29", "u", "update", [dict(EXPECTED[0])], "2026-07-30")
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)

    def reserve(offset):
        return state.acquire_attempt(
            "d",
            "2330",
            now + timedelta(seconds=offset),
            max_requests=1,
            rolling_seconds=60,
            max_attempts=3,
            terminal_on_exhaustion=True,
        )

    ok, audit = reserve(0)
    assert ok
    state.finish_attempt("d", "2330", audit, "failed", "ACQUISITION_FAILED", retryable=True)
    assert not reserve(1)[0], "a real dispatch still consumes the rolling allowance"
    ok, audit = reserve(61)
    assert ok
    state.finish_attempt("d", "2330", audit, "waiting", "QUOTA_WAIT", retryable=True)
    ok, audit = reserve(62)
    assert ok, "only the proven predispatch wait is excluded; its audit remains"
    state.finish_attempt("d", "2330", audit, "failed", "ACQUISITION_FAILED", retryable=True)
    assert not reserve(63)[0]
    ok, audit = reserve(123)
    assert ok
    state.finish_attempt("d", "2330", audit, "failed", "ACQUISITION_FAILED", retryable=True)
    assert not reserve(184)[0]
    assert state.get_terminal("d", "2330")[0] == "ACQUISITION_ATTEMPTS_EXHAUSTED"
    assert state.db.execute(
        "SELECT outcome,code FROM provider_attempts ORDER BY attempt_no"
    ).fetchall() == [
        ("failed", "ACQUISITION_FAILED"),
        ("waiting", "QUOTA_WAIT"),
        ("failed", "ACQUISITION_FAILED"),
        ("failed", "ACQUISITION_FAILED"),
    ]
    with pytest.raises(ShioajiStagingStateError, match="frozen checkpoint scope"):
        state.acquire_attempt(
            "d", "unfrozen-symbol", now, max_requests=1, rolling_seconds=60, max_attempts=3
        )
    state.close()
    ShioajiStagingState(tmp_path / "limits.sqlite3").close()
