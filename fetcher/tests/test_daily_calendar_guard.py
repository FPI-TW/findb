"""Legacy planned daily work never acquires a date the published calendar now closes."""

from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from findb_fetcher import provider_work
from findb_fetcher.full_market_runtime import CalendarDecisionRepairRequiredError
from test_full_market import _member, _plan, _runtime

PROVIDERS = ["twelve_data", "finlab", "shioaji", "taifex"]


def _owner(events, *, kind="daily", repair_required=False, probe=None):
    def request(action):
        assert action == "calendar-status"
        events.append("calendar-status")
        if probe is not None:
            return probe()
        return {"work_id": "work", "trade_date": "2026-10-01", "repair_required": repair_required}

    return SimpleNamespace(
        work={"work_id": "work", "trade_date": "2026-10-01", "kind": kind},
        dispatch_observations=0,
        journal=None,
        request=request,
        restore=lambda **kwargs: [],
        authorize=lambda: events.append("authorize"),
        reserve=lambda request: events.append("reserve"),
    )


def _release():
    return {"members": [_member(), {**_member(), "symbol": "MSFT", "member_key": "MSFT"}]}


def _attempts(runtime):
    with runtime.state.connection() as db:
        return {
            row["member_key"]: (row["status"], row["reason"], row["attempts"])
            for row in db.execute("SELECT member_key,status,reason,attempts FROM full_work")
        }


@pytest.mark.parametrize("provider", PROVIDERS)
def test_closed_legacy_plan_stops_before_claim_authorization_or_provider_io(
    tmp_path, monkeypatch, provider
):
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    events = []
    owner = _owner(events, repair_required=True)
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: pytest.fail("closed date acquired provider data")
    plan = _plan()
    runtime.state.record_plan(plan, provider)
    try:
        with pytest.raises(CalendarDecisionRepairRequiredError):
            runtime._deliver_plan(plan, _release(), runtime.feeds[0])
        assert events == ["calendar-status"]
        assert _attempts(runtime) == {
            "AAPL": ("pending", None, 0),
            "MSFT": ("pending", None, 0),
        }
        assert source.outcomes == []
    finally:
        runtime.http.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_closed_date_probe_skips_finished_members_and_keeps_their_evidence(
    tmp_path, monkeypatch, provider
):
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    events = []
    owner = _owner(events, repair_required=True)
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: pytest.fail("closed date acquired provider data")
    plan = _plan()
    runtime.state.record_plan(plan, provider)
    runtime.state.finish("fp1:123:AAPL", status="complete", reason="source_proven_no_trade")
    try:
        with pytest.raises(CalendarDecisionRepairRequiredError):
            runtime._deliver_plan(plan, _release(), runtime.feeds[0])
        assert events == ["calendar-status"]
        assert _attempts(runtime) == {
            "AAPL": ("complete", "source_proven_no_trade", 0),
            "MSFT": ("pending", None, 0),
        }
        assert source.outcomes == []
    finally:
        runtime.http.close()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_round_without_provider_io_never_probes_calendar(tmp_path, monkeypatch, provider):
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    events = []
    owner = _owner(events, repair_required=True)
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: pytest.fail("finished round acquired provider data")
    plan = _plan()
    runtime.state.record_plan(plan, provider)
    runtime.state.finish("fp1:123:AAPL", status="complete", reason="source_proven_no_trade")
    release = {
        "members": [_member(), {**_member(), "member_key": "MSFT", "mapping_status": "unmapped"}]
    }
    try:
        runtime._deliver_plan(plan, release, runtime.feeds[0])
        assert "calendar-status" not in events
        assert _attempts(runtime)["AAPL"] == ("complete", "source_proven_no_trade", 0)
        assert _attempts(runtime)["MSFT"][:2] == ("manual", "mapping_gap")
    finally:
        runtime.http.close()


def test_open_plan_probes_calendar_once_per_round(tmp_path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path)
    events = []
    owner = _owner(events)
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda member, *args: (
        events.append("acquire") or [{"idempotency_key": "frozen:" + member["member_key"]}]
    )
    runtime.delivery = SimpleNamespace(
        prepare=lambda request: request,
        deliver=lambda request: SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed", failed_records=0, success_records=1, total_records=1
        ),
    )
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        assert runtime._deliver_plan(plan, _release(), runtime.feeds[0])
        assert events == [
            "calendar-status",
            "authorize",
            "acquire",
            "reserve",
            "authorize",
            "acquire",
            "reserve",
        ]
    finally:
        runtime.http.close()


def test_unavailable_calendar_probe_spends_no_member_budget(tmp_path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path)
    events = []

    def unavailable():
        raise httpx.ReadError("Source unavailable")

    owner = _owner(events, probe=unavailable)
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: pytest.fail("probe failure acquired provider data")
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with pytest.raises(httpx.ReadError):
            runtime._deliver_plan(plan, _release(), runtime.feeds[0])
        assert events == ["calendar-status"]
        assert {attempts for _, _, attempts in _attempts(runtime).values()} == {0}
    finally:
        runtime.http.close()


def test_retained_prepared_drain_and_historical_rounds_skip_calendar_probe(tmp_path, monkeypatch):
    runtime, _, _ = _runtime(tmp_path)
    events = []
    owner = _owner(events, kind="historical")
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: events.append("acquire") or [{"idempotency_key": "h"}]
    runtime.delivery = SimpleNamespace(
        prepare=lambda request: request,
        deliver=lambda request: SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed", failed_records=0, success_records=1, total_records=1
        ),
    )
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, runtime.provider)
    try:
        assert runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert "calendar-status" not in events
    finally:
        runtime.http.close()

    runtime, _, _ = _runtime(tmp_path / "drain")
    events.clear()
    owner = _owner(events, repair_required=True)
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    runtime._acquire = lambda *args: pytest.fail("drain acquired provider data")
    runtime.delivery = SimpleNamespace(
        prepare=lambda request: request,
        deliver=lambda request: SimpleNamespace(run_id=UUID(int=1)),
        get_run_status=lambda *args, **kwargs: SimpleNamespace(
            status="completed", failed_records=0, success_records=1, total_records=1
        ),
    )
    runtime.state.record_plan(plan, runtime.provider)
    runtime.state.prepare("fp1:123:AAPL", b'[{"idempotency_key":"retained"}]')
    try:
        # Retained bytes drain through acceptance; only new acquisition is guarded.
        runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0], drain_only=True)
        assert "calendar-status" not in events
    finally:
        runtime.http.close()
