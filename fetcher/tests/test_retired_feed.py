"""Compatibility scope drains old work without reinstalling its acquisition feed."""

import json

import pytest

from findb_fetcher import provider_work as queue
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.full_market_universe import canonical_bytes
from test_full_market import _plan, _runtime


def _retired_runtime(
    tmp_path, monkeypatch, *, profile="pilot", state="open", scope=None, kind="daily"
):
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    runtime, source, _ = _runtime(tmp_path, execution_profile=profile)
    runtime._ensure_proof = lambda: None
    runtime._plan_for_date = lambda *args: pytest.fail("retired feed cannot create a plan")
    runtime._acquire = lambda *args: pytest.fail("retired feed cannot acquire")
    runtime.sync_universes = lambda **kwargs: pytest.fail("retired feed cannot refresh")
    events, settlements, bodies = [], [], []
    work = {
        "work_id": "original",
        "stable_key": "original-stable",
        "generation": 1,
        "token": "fence",
        "kind": kind,
        "dataset_key": "hk_equity_eod",
        "state": state,
        "trade_date": "2026-10-15",
        "scope": scope or {"legacy_id": "original-plan"},
    }

    def request(method, route, body=None):
        events.append(route.rsplit("/", 1)[-1])
        if route.startswith("full-market/authorization"):
            return {"acquisition_allowed": False}
        if route == "provider-work/claim":
            return {"work": work}
        if route.endswith("/prepared"):
            return {"bodies": bodies}
        if route.endswith("/settle"):
            settlements.append(body)
            if len(settlements) == 1:
                raise FullMarketProtocolError(
                    "accepted normalization pending", status_code=409, code="PROVIDER_WORK_DRAINING"
                )
        return {}

    def drain(owner):
        assert queue.inherited_fds()
        assert owner.restore() == bodies
        events.append("drain-originals")

    def wait(_):
        with pytest.raises(queue.ProviderWorkWaitingError):
            with queue.host_lock("twelve_data", root=tmp_path):
                pytest.fail("released owner before accepted runs terminal")

    source.request = request
    monkeypatch.setattr(queue.ProviderWorkSession, "drain_prepared", drain)
    monkeypatch.setattr(queue.time, "sleep", wait)
    return runtime, work, bodies, events, settlements


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("state", ["open", "closed"])
@pytest.mark.parametrize(
    "scope", [{"legacy_id": "original-plan"}, {"feed": {"dataset_key": "hk_equity_eod"}}]
)
def test_retired_daily_settles_without_planning_rewriting_or_reacquiring(
    tmp_path, monkeypatch, profile, state, scope
):
    runtime, work, _, events, settlements = _retired_runtime(
        tmp_path, monkeypatch, profile=profile, state=state, scope=scope
    )
    plan = {**_plan(), "dataset_key": work["dataset_key"], "trade_date": work["trade_date"]}
    runtime.state.record_plan(plan, runtime.provider)
    runtime.state.finish("fp1:123:AAPL", status="manual", reason="original-gap")
    with runtime.state.connection() as db:
        original = [tuple(row) for row in db.execute("SELECT * FROM full_work")]
    try:
        runtime._queued_cycle()
        assert events.index("drain-originals") < events.index("close") < events.index("settle")
        assert len(settlements) == 2
        assert all(row["outcome"] == "failed" for row in settlements)
        assert settlements[-1]["failure_code"] == "RETIRED_FEED_MANUAL_REVIEW"
        assert "manual_pending_prepared" not in settlements[-1]["proof"]
        assert "no_data" not in settlements[-1]["proof"]
        with runtime.state.connection() as db:
            assert [tuple(row) for row in db.execute("SELECT * FROM full_work")] == original
        with queue.host_lock("twelve_data", root=tmp_path):
            pass
    finally:
        runtime.close()


def test_retired_maintenance_does_not_spend_attempt_or_refresh_current_sibling(
    tmp_path, monkeypatch
):
    runtime, _, _, events, settlements = _retired_runtime(tmp_path, monkeypatch, kind="maintenance")
    try:
        runtime._queued_cycle()
        assert "drain-originals" in events
        assert settlements[-1]["failure_code"] == "RETIRED_FEED_MANUAL_REVIEW"
        journal = queue.WorkJournal(tmp_path / "provider-work.sqlite3", "original-stable")
        with journal.connection() as db:
            assert db.execute("SELECT count(*) FROM work_budget").fetchone()[0] == 0
    finally:
        runtime.close()


@pytest.mark.parametrize("registered", [False, True])
def test_retired_checkpoint_requires_every_original_sequence_and_preserves_receipts(
    tmp_path, monkeypatch, registered
):
    runtime, work, bodies, _, settlements = _retired_runtime(tmp_path, monkeypatch)
    plan = {**_plan(), "dataset_key": work["dataset_key"], "trade_date": work["trade_date"]}
    runtime.state.record_plan(plan, runtime.provider)
    key = "fp1:123:AAPL"
    requests = [
        {"idempotency_key": "first", "sequence": 1},
        {"idempotency_key": "second", "sequence": 2},
    ]
    original = canonical_bytes(requests)
    runtime.state.prepare(key, original)
    runtime.state.save_sequence_receipt(key, "first", "original-run")
    bodies.extend(canonical_bytes(row).decode() for row in requests[: 2 if registered else 1])
    try:
        if registered:
            runtime._queued_cycle()
            assert settlements[-1]["outcome"] == "failed"
        else:
            with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
                runtime._queued_cycle()
            assert not settlements
        assert runtime.state.prepared(key) == original
        assert runtime.state.sequence_receipt(key, "first") == "original-run"
        assert runtime.state.sequence_receipt(key, "second") is None
        assert json.loads(original) == requests
    finally:
        runtime.close()


def test_retired_drain_failure_keeps_fence_for_recovery(tmp_path, monkeypatch):
    runtime, _, _, _, settlements = _retired_runtime(tmp_path, monkeypatch)

    def fail(owner):
        raise FullMarketProtocolError("unavailable", status_code=503)

    monkeypatch.setattr(queue.ProviderWorkSession, "drain_prepared", fail)
    try:
        with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
            runtime._queued_cycle()
        assert not settlements
    finally:
        runtime.close()


@pytest.mark.parametrize("body", [b"[]", b"{invalid", b'{"unexpected":"shape"}'])
def test_invalid_retired_checkpoint_never_uses_manual_prepared_waiver(tmp_path, monkeypatch, body):
    runtime, work, _, events, settlements = _retired_runtime(tmp_path, monkeypatch)
    runtime.state.record_plan(
        {**_plan(), "dataset_key": work["dataset_key"], "trade_date": work["trade_date"]},
        runtime.provider,
    )
    runtime.state.prepare("fp1:123:AAPL", body)
    try:
        with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
            runtime._queued_cycle()
        assert not settlements and "drain-originals" not in events
        assert runtime.state.prepared("fp1:123:AAPL") == body
    finally:
        runtime.close()
