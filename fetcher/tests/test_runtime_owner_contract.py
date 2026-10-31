"""Queue idleness is legitimate; member execution always uses its fenced owner."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher import provider_work
from test_full_market import _member, _plan, _runtime


@pytest.mark.parametrize("mode", ["cycle", "drain", "maintenance"])
def test_real_empty_claim_keeps_idle_runtime_non_acquiring(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account/governor.sqlite3"))
    runtime, source, _ = _runtime(tmp_path)
    calls = []

    def request(method, path, body=None):
        calls.append(path)
        if path.startswith("full-market/authorization"):
            return {"acquisition_allowed": False}
        if path == "provider-work/claim":
            return {"work": None}
        assert mode == "maintenance" and path == "provider-work/enqueue"
        return {}

    source.request = request
    runtime._ensure_proof = lambda: None
    runtime._acquire = lambda *args: pytest.fail("idle runtime acquired provider data")
    try:
        if mode == "maintenance":
            assert runtime.sync_queued_universes() == [{"status": "queued"}]
        elif mode == "drain":
            assert runtime.drain() is None
        else:
            assert runtime.cycle()["unresolved"] == []
        assert calls.count("provider-work/claim") == 1
        assert provider_work.active() is None and provider_work.inherited_fds() == ()
    finally:
        runtime.http.close()


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
def test_missing_owner_cannot_restore_deliver_or_change_retained_state(tmp_path, provider):
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    plan = _plan()
    key = "fp1:123:AAPL"
    runtime.state.record_plan(plan, provider)
    original = b'[{"idempotency_key":"retained"}]'
    runtime.state.prepare(key, original)
    runtime.state.save_sequence_receipt(key, "retained", "original-run")
    try:
        with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
            runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert runtime.state.prepared(key) == original
        assert runtime.state.sequence_receipt(key, "retained") == "original-run"
        assert source.outcomes == []
        with runtime.state.connection() as db:
            assert db.execute("SELECT SUM(attempts) FROM full_work").fetchone()[0] == 0
    finally:
        runtime.http.close()


def test_member_uses_returned_owner_for_restore_authorization_and_reservation(
    tmp_path, monkeypatch
):
    runtime, _, _ = _runtime(tmp_path)
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, runtime.provider)
    events = []
    owner = SimpleNamespace(
        dispatch_observations=0,
        journal=None,
        restore=lambda **kwargs: events.append("restore") or [],
        authorize=lambda: events.append("authorize"),
        reserve=lambda request: events.append("reserve"),
    )
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    monkeypatch.setattr(
        provider_work, "active", lambda: pytest.fail("validated owner was discarded")
    )
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: events.append("acquire") or [{"idempotency_key": "frozen"}]
    runtime.delivery = SimpleNamespace(
        prepare=lambda request: request,
        deliver=lambda request: events.append("deliver") or SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed",
            failed_records=0,
            success_records=1,
            total_records=1,
        ),
    )
    try:
        assert runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert events == ["restore", "authorize", "acquire", "reserve", "deliver"]
    finally:
        runtime.http.close()


def test_cycles_replay_each_acknowledged_stable_enqueue_once(tmp_path, monkeypatch):
    from datetime import date, datetime, timezone

    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account/governor.sqlite3"))
    runtime, source, _ = _runtime(tmp_path)
    runtime.installed_feeds = [
        {
            **runtime.feeds[0],
            "source_timezone": "UTC",
            "target_date_lag_days": 0,
            "scheduled_time": "00:00",
        }
    ]
    first = datetime.now(timezone.utc).date().toordinal() - 3
    enqueued = []
    failures = {"daily:full_market:us_equity_eod:" + date.fromordinal(first).isoformat()}

    def request(method, path, body=None):
        if path.startswith("full-market/authorization"):
            return {"acquisition_allowed": True, "admitted_dataset_keys": ["us_equity_eod"]}
        if path == "provider-work/claim":
            return {"work": None}
        assert path == "provider-work/enqueue"
        if body["stable_key"] in failures:
            failures.remove(body["stable_key"])
            raise RuntimeError("Source unavailable")
        enqueued.append(body["stable_key"])
        return {}

    source.request = request
    source.universes = lambda dataset, provider: {
        "activation": {"enabled": True, "activation_date": date.fromordinal(first).isoformat()},
        "published_release_id": "release",
    }
    runtime._ensure_proof = lambda: None
    runtime._acquire = lambda *args: pytest.fail("idle runtime acquired provider data")
    try:
        with pytest.raises(RuntimeError):
            runtime.cycle()
        runtime.cycle()
        runtime.drain()
        runtime.cycle()
        daily = [key for key in enqueued if key.startswith("daily:")]
        assert len(daily) == len(set(daily)) == 4
        assert len(enqueued) == len(set(enqueued))
    finally:
        runtime.http.close()
