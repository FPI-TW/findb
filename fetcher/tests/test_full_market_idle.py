"""Stopped consumers remain cheap while immutable deliveries finish safely."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher.full_market_state import FullMarketState, FullMarketStateError
from findb_fetcher.full_market_universe import canonical_bytes
from test_full_market import _member
from test_full_market_recovery import _cycle_runtime, _frozen_plan, _release


def _setup(tmp_path, *, count=1, members=2):
    day = date(2026, 10, 1)
    release = _release(
        "frozen", "us_equity_eod", [{**_member(), "member_key": f"S{i}"} for i in range(members)]
    )
    runtime, source = _cycle_runtime(tmp_path, [release], [day])
    plans = []
    for offset in range(count):
        plan = _frozen_plan("us_equity_eod", day + timedelta(days=offset), release)
        source.plans[(plan["dataset_key"], plan["trade_date"])] = plan
        runtime.state.record_plan(plan, "twelve_data")
        plans.append(plan)
    runtime.can_acquire = lambda: False
    runtime._acquire = lambda *a: pytest.fail("stopped runtime acquired provider data")
    return runtime, source, plans


def _key(plan, member="S0"):
    return plan["parts"][0]["work_item_id"] + ":" + member


def test_large_stopped_backlog_performs_bounded_queries_without_health_or_remote_work(
    tmp_path, monkeypatch
):
    runtime, source, _ = _setup(tmp_path, count=40, members=500)
    original = runtime.state.connection
    queries = []

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(queries.append)
            yield db

    monkeypatch.setattr(runtime.state, "connection", traced)
    for method in ("health", "pending_plans", "prepared"):
        monkeypatch.setattr(runtime.state, method, lambda *a: pytest.fail("historical expansion"))
    source.release = source.evaluate = lambda *a: pytest.fail("idle remote work")
    try:
        for _ in range(3):
            runtime.drain()
        selects = [q for q in queries if q.startswith("SELECT")]
        assert len(selects) == 6  # Two indexed selectors per poll, regardless of member count.
        assert not any("SELECT * FROM full_plan" in q for q in queries)
        with original() as db:
            assert tuple(
                db.execute(
                    "SELECT count(*),sum(attempts),sum(lease_until) FROM full_work"
                ).fetchone()
            ) == (20000, 0, 0)
    finally:
        runtime.close()


@pytest.mark.parametrize("daily_policy", [False, True])
def test_stopped_drain_only_reads_live_prepared_members_and_keeps_receipts_across_restart(
    tmp_path, monkeypatch, daily_policy
):
    runtime, source, plans = _setup(tmp_path, count=5, members=100)
    if daily_policy:
        runtime.daily_policy = {"max_members": 25, "max_seconds": 60}
    plan = plans[0]
    key = _key(plan)
    body = canonical_bytes([{"idempotency_key": "seq1"}, {"idempotency_key": "seq2"}])
    runtime.state.prepare(key, body)
    terminal = _key(plan, "S1")
    runtime.state.prepare(terminal, b"retained evidence")
    runtime.state.finish(terminal, status="manual", reason="source_rejected")
    # Even corrupt terminal bytes must not be re-read/re-delivered on every poll.
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET body_sha256='bad' WHERE key=?", (terminal,))
    releases, evaluations, delivered, statuses = [], [], [], []
    release, evaluate = source.release, source.evaluate
    source.release = lambda identity: (releases.append(identity), release(identity))[1]
    source.evaluate = lambda identity: (evaluations.append(identity), evaluate(identity))[1]
    prepared = runtime.state.prepared

    def selected_only(identity):
        assert identity == key
        return prepared(identity)

    monkeypatch.setattr(runtime.state, "prepared", selected_only)

    def deliver(request):
        delivered.append(request["idempotency_key"])
        return SimpleNamespace(run_id=UUID(int=len(delivered)))

    pending = True

    def status(identity, **kwargs):
        statuses.append(identity)
        return SimpleNamespace(
            status="processing" if pending else "completed",
            failed_records=0,
            success_records=1,
            total_records=1,
        )

    runtime.delivery = SimpleNamespace(
        prepare=lambda request: request, deliver=deliver, get_run_status=status
    )
    try:
        runtime.drain()
        assert delivered == ["seq1"]
        assert releases == ["frozen"] and evaluations == [plan["plan_id"]]
        # Respect the persisted normalization retry deadline without re-reading releases.
        runtime.drain()
        assert releases == ["frozen"]
        runtime.state = FullMarketState(runtime.state.path)
        with runtime.state.connection() as db:
            db.execute("UPDATE full_work SET next_at=0 WHERE key=?", (key,))
        pending = False
        runtime.drain()
        assert delivered == ["seq1", "seq2"]  # Accepted seq1 was never re-delivered.
        assert len(statuses) == 3
        with runtime.state.connection() as db:
            assert (
                db.execute("SELECT status FROM full_work WHERE key=?", (key,)).fetchone()[0]
                == "complete"
            )
            assert (
                db.execute("SELECT status FROM full_work WHERE key=?", (terminal,)).fetchone()[0]
                == "manual"
            )
            assert (
                db.execute("SELECT count(*) FROM full_work_receipt WHERE key=?", (key,)).fetchone()[
                    0
                ]
                == 2
            )
        before = len(releases), len(evaluations)
        runtime.drain()
        assert (len(releases), len(evaluations)) == before
        assert plan["summary"]["missing"] == 98 and plan["summary"]["blocked"] == 1
    finally:
        runtime.close()


def test_terminal_progress_survives_crash_and_failed_final_evaluation(tmp_path):
    runtime, source, plans = _setup(tmp_path, members=1)
    plan = plans[0]
    key = _key(plan)
    body = canonical_bytes([{"idempotency_key": "stable"}])
    runtime.state.prepare(key, body)
    runtime.state.finish(key, status="complete", receipt=str(UUID(int=1)))
    runtime.state = FullMarketState(runtime.state.path)  # Crash before Source evaluation.
    source.release = lambda *a: pytest.fail("terminal delivery should only need evaluation")
    evaluate = source.evaluate
    source.evaluate = lambda *a: (_ for _ in ()).throw(ConnectionError("offline"))
    try:
        with pytest.raises(ConnectionError):
            runtime.drain()
        runtime.state = FullMarketState(runtime.state.path)
        source.evaluate = evaluate
        runtime.drain()
        assert runtime.state.pending_plans("twelve_data") == []
        assert runtime.state.prepared(key) == body
        assert runtime.state.receipt(key) == str(UUID(int=1))
        source.evaluate = lambda *a: pytest.fail("completed plan was re-evaluated")
        runtime.drain()
    finally:
        runtime.close()


def test_legacy_checkpoint_gets_one_time_completion_reconciliation_without_deleting_rows(tmp_path):
    runtime, source, plans = _setup(tmp_path, count=3)
    with runtime.state.connection() as db:
        db.execute("DROP TABLE full_plan_reconcile")
    runtime.state = FullMarketState(runtime.state.path)
    evaluate = source.evaluate
    seen = []
    source.evaluate = lambda identity: (seen.append(identity), evaluate(identity))[1]
    source.release = lambda *a: pytest.fail("no prepared work")
    try:
        runtime.drain()
        assert seen == [plan["plan_id"] for plan in plans]
        runtime.state = FullMarketState(runtime.state.path)
        runtime.drain()
        assert len(seen) == 3
        with runtime.state.connection() as db:
            assert db.execute("SELECT count(*) FROM full_work").fetchone()[0] == 6
    finally:
        runtime.close()


def test_corrupt_live_prepared_body_still_blocks_drain(tmp_path):
    runtime, _, plans = _setup(tmp_path)
    key = _key(plans[0])
    runtime.state.prepare(key, b"immutable")
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET body_sha256='bad' WHERE key=?", (key,))
    try:
        with pytest.raises(FullMarketStateError, match="checksum"):
            runtime.drain()
    finally:
        runtime.close()


@pytest.mark.parametrize("deadline", ["lease_until", "next_at"])
def test_stopped_drain_respects_persisted_lease_and_retry_deadline(tmp_path, monkeypatch, deadline):
    runtime, source, plans = _setup(tmp_path, members=1)
    key = _key(plans[0])
    runtime.state.prepare(key, canonical_bytes([{"idempotency_key": "stable"}]))
    now = time.time()
    with runtime.state.connection() as db:
        db.execute(f"UPDATE full_work SET {deadline}=? WHERE key=?", (now + 600, key))
    release = source.release
    source.release = lambda *a: pytest.fail("deferred/leased body was drained early")
    try:
        runtime.drain()
        with runtime.state.connection() as db:
            row = db.execute(
                f"SELECT attempts,{deadline} FROM full_work WHERE key=?", (key,)
            ).fetchone()
            assert tuple(row) == (0, now + 600)
        monkeypatch.setattr(time, "time", lambda: now + 601)
        source.release = release
        runtime.drain()
        assert runtime.state.pending_plans("twelve_data") == []
    finally:
        runtime.close()


@pytest.mark.parametrize("recovering", [False, True])
def test_queue_idle_drain_still_claims_recovery_but_skips_health(tmp_path, monkeypatch, recovering):
    from findb_fetcher import provider_work

    runtime, source, _ = _setup(tmp_path)
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    events = []
    settlements = []
    source.request = lambda *a, **kw: {"acquisition_allowed": False}
    owner = SimpleNamespace(
        work={"kind": "daily", "dataset_key": "us_equity_eod", "state": "closed"},
        settle=lambda *a, **kw: settlements.append((a, kw)),
    )

    @contextmanager
    def claim(*args, **kwargs):
        events.append(kwargs)
        yield owner if recovering else None

    monkeypatch.setattr(provider_work, "claimed_work", claim)
    runtime.health = lambda: pytest.fail("idle health expansion")
    try:
        runtime.drain()
        assert events == [
            {
                "accepted_kinds": ["daily", "maintenance", "rerun"],
                "execution_profile": "full_market",
            }
        ]
        assert settlements == (
            [(("retry",), {"failure_code": "RECOVERED_CLOSED_ROUND"})] if recovering else []
        )
    finally:
        runtime.close()
