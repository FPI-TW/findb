"""Proven account aliases retain debt with literal allocation association."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from findb_fetcher import acquisition
from findb_fetcher.acquisition import Acquisition
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.platform_limits import account_identity


def _merge(path, provider, raw, queue):
    state = FullMarketState(Path(path))
    queue.put(account_identity(provider, state, credential=raw))


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji"])
@pytest.mark.parametrize(
    "alias,unrelated",
    [("legacy_raw", "legacyXraw"), ("legacy%raw", "legacyZraw"), ("legacy\\raw", "legacyraw")],
)
def test_proven_merge_preserves_literal_allocations_and_excludes_other_accounts(
    tmp_path, provider, alias, unrelated
):
    raw = " shared-key "
    state = FullMarketState(tmp_path / "governor.sqlite3")
    with state.connection() as db:
        for key, account in [(raw, alias), (raw.strip(), "normalized")]:
            db.execute(
                "INSERT INTO full_account_identity VALUES(?,?,?)",
                (provider, hashlib.sha256(key.encode()).hexdigest(), account),
            )
        for account, count, bytes_, cooldown in [
            (alias, 4, 50, 0),
            ("normalized", 3, 25, 0),
            (alias + ":allocation:pilot", 2, 100, 10),
            (unrelated + ":allocation:pilot", 19, 456, 9999),
        ]:
            db.execute(
                "INSERT INTO full_quota VALUES(?,?,?,?,?)",
                (account, "2026-10-13", count, bytes_, cooldown),
            )
        db.execute("INSERT INTO full_pacing VALUES(?,?)", (alias, time.time() + 300))
    expected = account_identity(provider, state, credential=raw)
    assert expected == "normalized"
    assert account_identity(provider, state, credential=raw) == expected
    with state.connection() as db:
        assert tuple(
            db.execute(
                "SELECT requests,bytes FROM full_quota WHERE account='normalized'"
            ).fetchone()
        ) == (7, 75)
        assert tuple(
            db.execute(
                "SELECT requests,bytes,blocked_until FROM full_quota WHERE account='normalized:allocation:pilot'"
            ).fetchone()
        ) == (2, 100, 10)
        assert tuple(
            db.execute(
                "SELECT requests,bytes,blocked_until FROM full_quota WHERE account=?",
                (unrelated + ":allocation:pilot",),
            ).fetchone()
        ) == (19, 456, 9999)
        audit = json.loads(
            db.execute("SELECT original_quota FROM account_alias_audit").fetchone()[0]
        )
        assert {r["account"] for r in audit} == {alias, alias + ":allocation:pilot"}
        assert (
            db.execute("SELECT next_at FROM full_pacing WHERE account='normalized'").fetchone()[0]
            > time.time() + 290
        )
        assert db.execute("SELECT COUNT(*) FROM account_alias_audit").fetchone()[0] == 1


@pytest.mark.parametrize("provider", ["twelve_data", "finlab"])
def test_actual_processes_merge_proven_debt_exactly_once(tmp_path, provider, monkeypatch):
    state = FullMarketState(tmp_path / "governor.sqlite3")
    raw = " shared-key "
    # Keep seeded quota and the parent admission check in the same UTC minute;
    # real spawned processes may otherwise cross a legitimate quota rollover.
    quota_time = datetime(2026, 10, 13, 12, 0, 30, tzinfo=timezone.utc)
    with state.connection() as db:
        for key, account, count in [(raw, "raw", 1000), (raw.strip(), "normalized", 597)]:
            db.execute(
                "INSERT INTO full_account_identity VALUES(?,?,?)",
                (provider, hashlib.sha256(key.encode()).hexdigest(), account),
            )
            db.execute(
                "INSERT INTO full_quota VALUES(?,? ,?,0,0)",
                (account, quota_time.strftime("%Y-%m-%dT%H:%M"), count),
            )
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_merge, args=(str(state.path), provider, raw, queue)) for _ in range(3)
    ]
    for process in processes:
        process.start()
    assert [queue.get(timeout=10) for _ in processes] == ["normalized"] * 3
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    with state.connection() as db:
        assert (
            db.execute("SELECT requests FROM full_quota WHERE account='normalized'").fetchone()[0]
            == 1597
        )
        assert db.execute("SELECT COUNT(*) FROM account_alias_audit").fetchone()[0] == 1
    if provider == "twelve_data":
        monkeypatch.setattr(
            acquisition,
            "time",
            SimpleNamespace(time=lambda: quota_time.timestamp()),
        )
        with Acquisition(provider, credential=raw, state_path=state.path) as operation:
            with pytest.raises(QuotaBlockedError, match="quota"):
                operation.prepare()
