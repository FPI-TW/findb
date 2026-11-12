"""Local journal and checkpoint rows retire only after Source closed the work a week ago."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from findb_fetcher import full_market_cli
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.provider_work import WorkJournal, prune_closed_journal

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()
OLD = "2026-11-12T00:00:00+00:00"
RECENT = "2026-11-18T00:00:00+00:00"
OLD_WORK = "019a0000-0000-7000-8000-000000000001"
UNRECEIPTED = "019a0000-0000-7000-8000-000000000002"
RECENT_WORK = "019a0000-0000-7000-8000-000000000003"
OPEN_WORK = "019a0000-0000-7000-8000-000000000004"
# A closed work that only ever journaled responses (no manifest, delivery or job).
RESPONSE_ONLY = "019a0000-0000-7000-8000-000000000005"
SNAPSHOT = "official_snapshot:" + "a" * 64


class _Source:
    def __init__(self, closed: dict[str, str]) -> None:
        self.closed = closed
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        assert (method, path) == ("POST", "provider-work/closed")
        assert 1 <= len(body["work_ids"]) + len(body["stable_keys"]) <= 500
        self.calls.append(body)
        return {
            "data": [
                {"work_id": key, "stable_key": key, "state": "completed", "completed_at": at}
                for key, at in self.closed.items()
                if key in body["work_ids"] or key in body["stable_keys"]
            ]
        }


def _journal(tmp_path: Path) -> Path:
    path = tmp_path / "provider-work.sqlite3"
    for key in (OLD_WORK, UNRECEIPTED, RECENT_WORK, OPEN_WORK):
        journal = WorkJournal(path, key)
        journal.freeze({"members": [{"key": "m"}]})
        with journal.connection() as db:
            db.execute(
                "INSERT INTO work_delivery VALUES(?,?,?,?,?,?)",
                (key, "m", b"{}", "0" * 64, None if key == UNRECEIPTED else "run", None),
            )
            db.execute("INSERT INTO work_budget VALUES(?,?,?)", (key, "op", 1))
            db.execute("INSERT INTO work_response VALUES(?,?,?,?)", (key, "r", b"{}", "0" * 64))
            db.execute(
                "INSERT INTO work_response VALUES(?,?,?,?)", (key, SNAPSHOT, b"x" * 4096, "0" * 64)
            )
    with WorkJournal(path, RESPONSE_ONLY).connection() as db:
        db.execute(
            "INSERT INTO work_response VALUES(?,?,?,?)",
            (RESPONSE_ONLY, SNAPSHOT, b"x" * 4096, "0" * 64),
        )
    return path


def _responses(path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as db:
        return {
            (row[0], row[1].split(":")[0])
            for row in db.execute("SELECT work_key,response_key FROM work_response")
        }


def _keys(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute(f"SELECT work_key FROM {table}")}


@pytest.mark.parametrize(
    ("provider", "responses_retired"), [("twelve_data", True), ("finlab", False)]
)
def test_journal_retires_only_week_old_closed_and_receipted_works(
    tmp_path: Path, provider: str, responses_retired: bool
) -> None:
    path = _journal(tmp_path)
    source = _Source({OLD_WORK: OLD, UNRECEIPTED: OLD, RECENT_WORK: RECENT, RESPONSE_ONLY: OLD})

    removed = prune_closed_journal(source, provider, root=tmp_path, now=NOW)

    assert removed is not None
    assert {key: removed[key] for key in ("works", "unreceipted")} == {
        "works": 2,
        "unreceipted": 1,
    }
    kept = {UNRECEIPTED, RECENT_WORK, OPEN_WORK}
    assert _keys(path, "work_delivery") == kept
    assert _keys(path, "work_budget") == kept
    assert _keys(path, "work_manifest") == (kept if responses_retired else kept | {OLD_WORK})
    # Official snapshots have an R2 copy whoever owns the work, so they always go;
    # the provider's other responses go only where every body is R2-backed.
    untouched = {(key, name) for key in kept for name in ("r", "official_snapshot")}
    assert _responses(path) == (untouched if responses_retired else untouched | {(OLD_WORK, "r")})
    assert removed["responses"] == (3 if responses_retired else 2)
    assert removed["file_bytes"] > 0 and 0 <= removed["free_bytes"] < removed["file_bytes"]
    # Once a day per provider: the next pass neither asks Source nor deletes.
    assert prune_closed_journal(source, provider, root=tmp_path, now=NOW + 60) is None
    assert len(source.calls) == 1
    assert prune_closed_journal(source, "taifex", root=tmp_path, now=NOW + 60) is not None


def test_missing_journal_is_never_created(tmp_path: Path) -> None:
    assert prune_closed_journal(_Source({}), "finlab", root=tmp_path, now=NOW) is None
    assert not (tmp_path / "provider-work.sqlite3").exists()


def _plan(state: FullMarketState, day: str, *, prepared: bool) -> str:
    plan_id = f"plan:{day}"
    state.record_plan(
        {
            "plan_id": plan_id,
            "dataset_key": "tw_equity_eod",
            "trade_date": day,
            "parts": [{"work_item_id": "part-" + day, "member_keys": ["2330", "2317"]}],
        },
        "finlab",
    )
    key = f"part-{day}:2330"
    if prepared:
        state.prepare(key, b"[]")
    else:
        state.finish(key, status="complete", receipt="run")
        state.save_sequence_receipt(key, "idem", "run")
        state.record_diagnostic(key, provider="finlab", category="member:non_finite")
    with state.connection() as db:
        db.execute("INSERT INTO full_no_data_round VALUES(?,?,?)", (plan_id, 2, NOW))
    return plan_id


def test_checkpoint_retires_closed_plans_but_keeps_undelivered_bodies(tmp_path: Path) -> None:
    state = FullMarketState(tmp_path / "state.sqlite3", clock=lambda: NOW)
    closed = _plan(state, "2026-11-02", prepared=False)
    undelivered = _plan(state, "2026-11-03", prepared=True)
    recent = _plan(state, "2026-11-17", prepared=False)
    source = _Source(
        {
            "daily:full_market:tw_equity_eod:2026-11-02": OLD,
            "daily:full_market:tw_equity_eod:2026-11-03": OLD,
            "daily:full_market:tw_equity_eod:2026-11-17": RECENT,
        }
    )
    from findb_fetcher.provider_work import closed_work_times

    removed = state.prune_closed_plans(
        "finlab", lambda keys: closed_work_times(source, stable_keys=keys)
    )

    assert removed == {"plans": 1, "undelivered": 1}
    with state.connection() as db:
        plans = {row[0] for row in db.execute("SELECT plan_id FROM full_plan")}
        work = {row[0] for row in db.execute("SELECT DISTINCT plan_id FROM full_work")}
        receipts = {row[0] for row in db.execute("SELECT key FROM full_work_receipt")}
        diagnostics = {row[0] for row in db.execute("SELECT key FROM full_work_diagnostic")}
        rounds = {row[0] for row in db.execute("SELECT plan_id FROM full_no_data_round")}
    assert plans == work == rounds == {undelivered, recent}
    assert receipts == diagnostics == {"part-2026-11-17:2330"}
    assert state.prune_closed_plans("finlab", lambda keys: pytest.fail("throttled")) is None
    assert closed not in plans


def test_retention_failure_does_not_stop_the_worker(monkeypatch, capsys) -> None:
    events = []

    def failing():
        raise RuntimeError("Source unavailable")

    monkeypatch.setattr(full_market_cli, "prune_ledger", lambda: None)
    runtime = SimpleNamespace(
        prune_closed_state=failing,
        drain=lambda: events.append("drain"),
        pause=lambda: events.append("pause"),
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
    out = capsys.readouterr().out
    assert '"closed_work_retention": {"failed": "RuntimeError"}' in out
    assert "Source unavailable" not in out
