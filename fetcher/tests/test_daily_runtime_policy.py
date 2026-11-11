"""Bounded daily turns, durable continuation and clock boundaries."""

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from findb_fetcher import full_market_runtime as module
from findb_fetcher.daily_runtime import window
from findb_fetcher.full_market_state import (
    FULL_WORK_MAX_ATTEMPTS,
    FullMarketState,
    QuotaBlockedError,
)
from test_full_market import _member, _runtime

POLICY = {
    "timezone": "Asia/Taipei",
    "start": "18:00",
    "end": "06:00",
    "max_members": 25,
    "max_seconds": 60,
}


@pytest.mark.parametrize(
    ("clock", "opened", "next_open"),
    [
        ("2026-10-28T17:59:59+08:00", False, "2026-10-28T18:00:00+08:00"),
        ("2026-10-28T18:00:00+08:00", True, None),
        ("2026-10-28T23:00:00+08:00", True, None),
        ("2026-10-29T00:00:00+08:00", True, None),
        ("2026-10-29T05:59:59+08:00", True, None),
        ("2026-10-29T06:00:00+08:00", False, "2026-10-29T18:00:00+08:00"),
    ],
)
def test_window_boundaries(clock, opened, next_open):
    result = window(POLICY, datetime.fromisoformat(clock))
    assert result["open"] is opened
    assert result["next_open_at"] == next_open


def harness(tmp_path, monkeypatch, count=100):
    runtime, source, _ = _runtime(tmp_path, provider="finlab")
    runtime.daily_policy = dict(POLICY)
    clock = [datetime(2026, 10, 28, 12, tzinfo=timezone.utc)]
    elapsed = [0.0]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(time=lambda: clock[0].timestamp(), monotonic=lambda: elapsed[0]),
    )
    release = {
        "release_id": "frozen",
        "members": [{**_member(), "member_key": f"S{i}"} for i in range(count)],
    }
    plan = {
        "plan_id": "daily-equity",
        "dataset_key": "tw_equity_eod",
        "trade_date": "2026-10-28",
        "release_id": "frozen",
        "status": "incomplete",
        "parts": [
            {"work_item_id": "part", "member_keys": [m["member_key"] for m in release["members"]]}
        ],
    }
    runtime.state.record_plan(plan, "finlab")
    acquired = []
    runtime._acquire = lambda member, *args: acquired.append(member["member_key"]) or []
    return runtime, source, release, plan, acquired, clock, elapsed


def test_large_stock_plan_yields_and_resumes_after_checkpoint_restart(tmp_path, monkeypatch):
    runtime, _, release, plan, acquired, _, _ = harness(tmp_path, monkeypatch)
    frozen = copy.deepcopy(plan)
    runtime._deliver_plan(plan, release, {})
    assert acquired == [f"S{i}" for i in range(25)]
    assert runtime.last_batch["reason"] in {"scanned", "batch_yield"}
    runtime.state = FullMarketState(runtime.state.path)
    runtime._deliver_plan(plan, release, {})
    assert acquired == [f"S{i}" for i in range(50)]
    assert plan == frozen
    assert runtime.state.plan_progress(plan["plan_id"])["counts"] == {"complete": 50, "pending": 50}


def test_slow_or_failed_member_yields_before_next_member_and_cursor_survives(tmp_path, monkeypatch):
    runtime, _, release, plan, acquired, _, elapsed = harness(tmp_path, monkeypatch)

    def slow(member, *args):
        acquired.append(member["member_key"])
        elapsed[0] += 61
        raise RuntimeError("private provider details")

    runtime._acquire = slow
    runtime._deliver_plan(plan, release, {})
    assert acquired == ["S0"]
    assert runtime.last_batch["reason"] in {"scanned", "batch_yield"}
    assert runtime.state.schedule_cursor(plan["plan_id"]) == 1
    progress = runtime.state.plan_progress(plan["plan_id"])
    assert progress["failures"] == [{"member_key": "S0", "reason": "source_error", "attempts": 1}]
    assert "private provider" not in json.dumps(progress)


def test_midnight_close_and_reopen_keep_retry_exhaustion_and_receipts(tmp_path, monkeypatch):
    runtime, _, release, plan, acquired, clock, _ = harness(tmp_path, monkeypatch)
    key = "part:S0"
    with runtime.state.connection() as db:
        db.execute(
            "UPDATE full_work SET status='manual',attempts=?,reason='retry_exhausted' WHERE key=?",
            (FULL_WORK_MAX_ATTEMPTS, key),
        )
    runtime.state.save_sequence_receipt(key, "immutable", "saved-run")
    clock[0] = datetime.fromisoformat("2026-10-29T06:00:00+08:00")
    runtime._deliver_plan(plan, release, {})
    assert acquired == []
    clock[0] = datetime.fromisoformat("2026-10-29T18:00:00+08:00")
    runtime.state = FullMarketState(runtime.state.path)
    runtime._deliver_plan(plan, release, {})
    assert acquired == [f"S{i}" for i in range(1, 26)]
    assert runtime.state.sequence_receipt(key, "immutable") == "saved-run"
    assert (
        runtime.state.plan_progress(plan["plan_id"])["failures"][0]["attempts"]
        == FULL_WORK_MAX_ATTEMPTS
    )


def test_quota_wait_rotates_without_spending_failure_budget(tmp_path, monkeypatch):
    runtime, source, release, plan, _, _, _ = harness(tmp_path, monkeypatch)

    def quota(*args):
        raise QuotaBlockedError("account waiting", quota_wait=True)

    runtime._acquire = quota
    assert runtime._deliver_plan(plan, release, {}) is False
    assert runtime.last_batch["reason"] == "quota_wait"
    assert source.outcomes == []
    with runtime.state.connection() as db:
        assert tuple(
            db.execute("SELECT attempts,reason FROM full_work WHERE key='part:S0'").fetchone()
        ) == (0, "rate_limited")
    assert runtime.state.schedule_cursor(plan["plan_id"]) == 1


def test_window_closed_prepared_replay_never_reacquires(tmp_path, monkeypatch):
    from uuid import UUID

    runtime, _, release, plan, acquired, clock, _ = harness(tmp_path, monkeypatch, count=1)
    from findb_fetcher.full_market_universe import canonical_bytes

    runtime.state.prepare("part:S0", canonical_bytes([{"idempotency_key": "original"}]))
    bodies = []
    runtime.delivery = SimpleNamespace(
        prepare=lambda body: body,
        deliver=lambda body: bodies.append(body) or SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed", failed_records=0, total_records=1, success_records=1
        ),
    )
    clock[0] = datetime.fromisoformat("2026-10-29T06:00:00+08:00")
    runtime._deliver_plan(plan, release, {}, drain_only=True)
    assert acquired == []
    assert bodies == [{"idempotency_key": "original"}]
    assert runtime.state.sequence_receipt("part:S0", "original") == str(UUID(int=1))


def test_failed_member_does_not_terminate_pending_siblings_and_settlement_drains(
    tmp_path, monkeypatch
):
    runtime, source, release, plan, _, _, _ = harness(tmp_path, monkeypatch)
    runtime.state.finish("part:S0", status="manual", reason="mapping_gap")
    source.evaluate = lambda identity: {
        "plan_id": identity,
        "status": "incomplete",
        "summary": {
            "expected": 100,
            "data": 0,
            "no_data": 0,
            "missing": 99,
            "blocked": 1,
            "is_late": True,
        },
    }
    runtime.state.schedule_save(
        plan["plan_id"], batch={"processed": 25, "reason": "batch_yield", "elapsed_seconds": 1.0}
    )
    events = []
    settlements = []
    owner = SimpleNamespace(
        work={"kind": "daily", "manifest": {"members": []}},
        journal=SimpleNamespace(manifest=lambda: {"plan_id": plan["plan_id"]}),
        request=lambda *args, **kwargs: events.append(args[0]),
        drain_prepared=lambda **kwargs: events.append("drain"),
        prepared_keys=lambda: [],
        settle=lambda outcome, **kwargs: settlements.append((outcome, kwargs)),
    )
    runtime._settle_queued_round(owner)
    assert events == ["close", "drain"]
    assert settlements[0][0] == "retry"
    assert settlements[0][1]["failure_code"] is None
    assert settlements[0][1]["delay"] == 1
    assert settlements[0][1]["proof"]["scheduling"]["counts"] == {"manual": 1, "pending": 99}


def test_crash_after_claim_rotates_then_wraps_to_original_member(tmp_path, monkeypatch):
    runtime, _, release, plan, acquired, clock, _ = harness(tmp_path, monkeypatch, count=3)

    def crash(*args):
        raise KeyboardInterrupt

    runtime._acquire = crash
    with pytest.raises(KeyboardInterrupt):
        runtime._deliver_plan(plan, release, {})
    runtime.state = FullMarketState(runtime.state.path)
    runtime._acquire = lambda member, *args: acquired.append(member["member_key"]) or []
    runtime._deliver_plan(plan, release, {})
    assert acquired == ["S1", "S2"]
    from datetime import timedelta

    clock[0] += timedelta(seconds=601)
    runtime._deliver_plan(plan, release, {})
    assert acquired == ["S1", "S2", "S0"]
    assert runtime.state.plan_progress(plan["plan_id"])["counts"] == {"complete": 3}


def test_window_closes_between_members_without_claiming_another(tmp_path, monkeypatch):
    runtime, _, release, plan, acquired, clock, _ = harness(tmp_path, monkeypatch)

    def closing(member, *args):
        acquired.append(member["member_key"])
        clock[0] = datetime.fromisoformat("2026-10-29T06:00:00+08:00")
        return []

    runtime._acquire = closing
    runtime._deliver_plan(plan, release, {})
    assert acquired == ["S0"]
    with runtime.state.connection() as db:
        assert db.execute("SELECT SUM(attempts) FROM full_work").fetchone()[0] == 1


def test_terminal_members_never_issue_restore_rpc_on_later_slices(tmp_path, monkeypatch):
    from findb_fetcher import provider_work

    runtime, _, release, plan, acquired, _, _ = harness(tmp_path, monkeypatch, count=2)
    runtime.state.finish("part:S0", status="manual", reason="mapping_gap")
    restored = []
    owner = SimpleNamespace(
        work={"kind": "daily"},
        dispatch_observations=0,
        restore=lambda **kwargs: restored.append(kwargs["member_key"]) or [],
        authorize=lambda: None,
        request=lambda action: {"work_id": None, "trade_date": None, "repair_required": False},
    )
    monkeypatch.setattr(provider_work, "active", lambda: owner)
    runtime._deliver_plan(plan, release, {})
    assert restored == acquired == ["S1"]


def test_enabled_queue_freezes_both_daily_plans_before_delivery(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from findb_fetcher import provider_work

    runtime, source, release, plan, _, _, _ = harness(tmp_path, monkeypatch)
    feeds = [
        {
            "provider": "finlab",
            "dataset_key": key,
            "source_timezone": "Asia/Taipei",
            "scheduled_time": "18:00",
            "target_date_lag_days": 0,
        }
        for key in ("tw_equity_eod", "tw_etf_eod")
    ]
    runtime.installed_feeds = feeds
    planned = []
    etf = {
        **copy.deepcopy(plan),
        "plan_id": "etf",
        "dataset_key": "tw_etf_eod",
        "parts": [{"work_item_id": "etf-part", "member_keys": ["E0"]}],
    }
    plans = {plan["dataset_key"]: plan, etf["dataset_key"]: etf}
    source.request = lambda method, path, body=None: (
        {
            "acquisition_allowed": True,
            "admitted_dataset_keys": [feed["dataset_key"] for feed in feeds],
        }
        if method == "GET"
        else {}
    )
    source.universes = lambda *args, **kwargs: {
        "activation": {"enabled": True, "activation_date": "2026-10-28"},
        "published_release_id": "frozen",
    }
    source.release = lambda identity: release
    runtime.state.refresh("finlab", now=0, result=[], next_at=9999999999)
    runtime._ensure_proof = lambda: None
    runtime._plan_for_date = lambda feed, day: (
        planned.append(feed["dataset_key"]) or plans[feed["dataset_key"]]
    )
    owner = SimpleNamespace(
        work={
            "work_id": "frozen-daily-work",
            "kind": "daily",
            "dataset_key": plan["dataset_key"],
            "state": "open",
            "scope": {"feed": feeds[0]},
            "trade_date": plan["trade_date"],
        },
        freeze=lambda manifest: None,
        _preparation_request=lambda action: {
            "work_id": "frozen-daily-work",
            "plan_id": plan["plan_id"],
            "repairs": [],
        },
    )

    @contextmanager
    def claim(*args, **kwargs):
        yield owner

    monkeypatch.setattr(provider_work, "claimed_work", claim)
    delivered = []

    def deliver(*args, **kwargs):
        assert set(planned) == {"tw_equity_eod", "tw_etf_eod"}
        assert len(runtime.state.pending_plans("finlab")) == 2
        delivered.append(args[0]["dataset_key"])

    runtime._deliver_plan = deliver
    runtime._settle_queued_round = lambda *args: None
    from findb_fetcher.execution_config import ExecutionSnapshot

    runtime._queued_cycle(snapshot=ExecutionSnapshot("fixture", tuple(feeds)))
    assert delivered == ["tw_equity_eod"]


def test_authoritative_window_denial_preserves_budget_even_if_local_clock_is_open(
    tmp_path, monkeypatch
):
    from findb_fetcher.acquisition import _policy
    from findb_fetcher.full_market_client import FullMarketProtocolError

    runtime, source, release, plan, _, _, _ = harness(tmp_path, monkeypatch, count=1)
    runtime._acquire = module._governed(lambda self, *args: _policy.get()()).__get__(runtime)
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=7")

    def denied(*args, **kwargs):
        raise FullMarketProtocolError(
            "daily acquisition window closed", code="DAILY_ACQUISITION_WINDOW_CLOSED"
        )

    source.authorization = denied
    runtime._deliver_plan(plan, release, {"slot_id": "taiwan_market_window"})
    with runtime.state.connection() as db:
        assert tuple(db.execute("SELECT attempts,reason FROM full_work").fetchone()) == (
            7,
            "acquisition_window_closed",
        )
    assert runtime.last_batch["reason"] == "acquisition_window_closed"
    assert source.outcomes == []


def test_status_only_revisits_do_not_spend_member_budget(tmp_path, monkeypatch, capsys):
    from uuid import UUID

    from findb_fetcher.full_market_universe import canonical_bytes

    runtime, _, release, plan, acquired, _, _ = harness(tmp_path, monkeypatch, count=40)
    statuses = []

    class Delivery:
        def prepare(self, value):
            pytest.fail("an accepted body is not prepared again")

        def deliver(self, value):
            pytest.fail("an accepted body is not delivered again")

        def get_run_status(self, run_id, *, expected):
            statuses.append(expected.idempotency_key)
            return SimpleNamespace(
                status="processing", failed_records=0, total_records=1, success_records=1
            )

    runtime.delivery = Delivery()
    for i in range(10):
        key = f"part:S{i}"
        request = {
            "idempotency_key": f"full:S{i}",
            "dataset_key": "tw_equity_eod",
            "schema_id": "market_eod",
            "schema_version": 1,
        }
        runtime.state.prepare(key, canonical_bytes([request]))
        runtime.state.save_sequence_receipt(key, request["idempotency_key"], str(UUID(int=i + 1)))
    runtime._deliver_plan(plan, release, {})
    # Ten normalizing members only read their runs; all 25 budget slots stay
    # with members that still need a provider acquisition.
    assert statuses == [f"full:S{i}" for i in range(10)]
    assert acquired == [f"S{i}" for i in range(10, 35)]
    assert runtime.last_batch["processed"] == 25
    # Timing is logged per batch but never enters the durable batch/proof.
    assert "timing" not in runtime.state.plan_progress(plan["plan_id"])["last_batch"]
    logged = [
        json.loads(line)["delivery_batch"]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"delivery_batch"')
    ]
    assert logged[-1]["processed"] == 25 and isinstance(logged[-1]["timing"], dict)
