"""Shared-ledger retention keeps every open debt and only drops settled history."""

from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import pytest

from findb_fetcher import full_market_cli
from findb_fetcher.account_governor import prune_ledger
from findb_fetcher.full_market_state import (
    ACCOUNT_LEDGER_PRUNE_INTERVAL_SECONDS,
    ACCOUNT_LEDGER_RETENTION_SECONDS,
    FullMarketState,
)

NOW = 1_800_000_000.0
OLD = NOW - ACCOUNT_LEDGER_RETENTION_SECONDS - 60
RECENT = NOW - 3600


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ledger(tmp_path):
    clock = Clock()
    return FullMarketState(tmp_path / "governor.sqlite3", clock=clock), clock


def _intent(db, operation_id, *, account="twelve_data:a", provider="twelve_data", status, at):
    db.execute(
        "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,actual_start,completed_at) VALUES(?,?,?,?,?,300,?,?)",
        (operation_id, account, provider, at - 1, status, at - 1, at),
    )


def _rows(state, sql):
    with state.connection() as db:
        return {tuple(row) for row in db.execute(sql)}


def test_hot_path_queries_use_status_index(ledger):
    state, _ = ledger
    with state.connection() as db:
        for sql, parameters in (
            (
                "SELECT * FROM acquisition_intent WHERE account=? AND status='armed'",
                ("twelve_data:a",),
            ),
            (
                "SELECT COALESCE(SUM(request_units),0) FROM acquisition_intent WHERE account=? AND status='unknown' AND armed_at<? AND completed_at>=?",
                ("twelve_data:a", NOW, NOW - 86400),
            ),
        ):
            plan = " ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + sql, parameters))
            assert "acquisition_intent_status (account=? AND status=?" in plan


class _TempStoreRecorder(sqlite3.Connection):
    builds: list[tuple[str, int]] = []

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("CREATE INDEX"):
            self.builds.append((sql, super().execute("PRAGMA temp_store").fetchone()[0]))
        return super().execute(sql, *args)


def test_index_builds_on_an_existing_ledger_never_spill_to_tmp(ledger, monkeypatch):
    # Production consumers have a 16 MiB /tmp; sorting a large existing ledger
    # there failed every governed acquisition with "database or disk is full".
    state, clock = ledger
    with state.connection() as db:
        db.execute("DROP INDEX acquisition_intent_status")
        for index in range(50):
            _intent(db, f"settled-{index}", status="finalized", at=OLD)
    connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: connect(*a, factory=_TempStoreRecorder, **k)
    )
    _TempStoreRecorder.builds = []

    FullMarketState(state.path, clock=clock)

    built = {sql.split(" ON ")[0].split()[-1]: store for sql, store in _TempStoreRecorder.builds}
    assert "acquisition_intent_status" in built
    assert set(built.values()) == {2}  # 2 = MEMORY


def test_prune_drops_settled_history_and_keeps_open_debt(ledger):
    state, _ = ledger
    with state.connection() as db:
        _intent(db, "old-finalized", status="finalized", at=OLD)
        _intent(db, "old-cancelled", status="cancelled", at=OLD)
        _intent(db, "recent-finalized", status="finalized", at=RECENT)
        _intent(db, "old-unknown", status="unknown", at=OLD)
        _intent(db, "old-armed", status="armed", at=OLD)
        _intent(
            db, "old-shioaji", account="shioaji:s", provider="shioaji", status="finalized", at=OLD
        )
        for operation_id in ("old-finalized", "old-shioaji"):
            db.execute(
                "INSERT INTO acquisition_usage(operation_id,observed_at,bytes,limit_bytes,remaining_bytes) VALUES(?,?,1,10,9)",
                (operation_id, OLD),
            )
        db.execute("INSERT INTO full_account_identity VALUES('shioaji','digest','shioaji:quota')")
        for account, window, blocked in (
            ("twelve_data:a", "2026-12-01", 0),
            ("twelve_data:a", "2026-12-01T10:00", 0),
            ("twelve_data:a", "2026-12-02", NOW + 600),
            ("twelve_data:a", "2027-01-15", 0),
            ("twelve_data:a", "2027-01-15T08:00", 0),
            ("shioaji:quota", "2026-12-01", 0),
        ):
            db.execute(
                "INSERT INTO full_quota(account,window,requests,bytes,blocked_until) VALUES(?,?,1,0,?)",
                (account, window, blocked),
            )

    assert state.prune_account_ledger() == {"intents": 2, "quota_windows": 2}

    assert _rows(state, "SELECT operation_id FROM acquisition_intent") == {
        ("recent-finalized",),
        ("old-unknown",),
        ("old-armed",),
        ("old-shioaji",),
    }
    assert _rows(state, "SELECT operation_id FROM acquisition_usage") == {("old-shioaji",)}
    # Current windows hold today's usage; a future cooldown floor is debt.
    assert _rows(state, "SELECT account,window FROM full_quota") == {
        ("twelve_data:a", "2026-12-02"),
        ("twelve_data:a", "2027-01-15"),
        ("twelve_data:a", "2027-01-15T08:00"),
        ("shioaji:quota", "2026-12-01"),
    }


def test_prune_runs_once_per_interval_across_processes(ledger, tmp_path):
    state, clock = ledger
    other = FullMarketState(state.path, clock=clock)
    assert state.prune_account_ledger() == {"intents": 0, "quota_windows": 0}
    assert other.prune_account_ledger() is None
    with state.connection() as db:
        _intent(db, "later", status="finalized", at=OLD)
    clock.now += ACCOUNT_LEDGER_PRUNE_INTERVAL_SECONDS
    assert other.prune_account_ledger() == {"intents": 1, "quota_windows": 0}


def test_prune_batches_until_every_expired_row_is_gone(ledger):
    state, _ = ledger
    with state.connection() as db:
        for index in range(7):
            _intent(db, f"old-{index}", status="finalized", at=OLD)
    assert state.prune_account_ledger(batch=3) == {"intents": 7, "quota_windows": 0}
    assert _rows(state, "SELECT COUNT(*) FROM acquisition_intent") == {(0,)}


def test_missing_ledger_is_never_created(tmp_path, monkeypatch):
    path = tmp_path / "absent" / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    assert prune_ledger() is None
    assert not path.parent.exists()


def test_retention_failure_does_not_stop_the_worker(monkeypatch, capsys):
    events = []

    def failing():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(full_market_cli, "prune_ledger", failing)
    runtime = SimpleNamespace(
        drain=lambda: events.append("drain"), pause=lambda: events.append("pause")
    )
    control = SimpleNamespace(poll=lambda **_: SimpleNamespace(desired_state="stopped"))
    assert (
        full_market_cli.run_worker(
            runtime,
            control,
            lambda: True,
            stopped=threading.Event(),
            run_forever=False,
            poll_seconds=1,
        )
        == 0
    )
    assert events == ["drain", "pause"]
    assert '"account_ledger_retention": {"failed": 1}' in capsys.readouterr().out
