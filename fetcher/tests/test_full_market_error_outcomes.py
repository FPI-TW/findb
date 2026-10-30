"""Source error reports survive retries without another provider acquisition."""

import json

import httpx
import pytest

from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.full_market_state import FULL_WORK_MAX_ATTEMPTS, FullMarketState
from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError
from provider_work_support import owned_unit_work
from test_full_market import _member, _plan, _runtime


@pytest.mark.parametrize("window_open", [True, False])
def test_snapshot_inner_stop_race_never_reports_or_spends_failure_budget(tmp_path, window_open):
    runtime, source, _ = _runtime(tmp_path, provider="taifex")
    checks = iter([True, True, False])
    runtime._acquisition_allowed = lambda: next(checks)
    runtime.runtime_window = lambda: {"open": window_open}
    runtime._acquire = lambda *args: runtime._snapshot("https://official.example.test")
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert source.outcomes == []
        assert not runtime.state.pending_source_error_keys("p")
        with runtime.state.connection() as db:
            row = db.execute(
                "SELECT attempts,reason FROM full_work WHERE member_key='AAPL'"
            ).fetchone()
            assert tuple(row) == (0, "suspended")
    finally:
        runtime.close()


def test_fence_failure_is_never_reported_as_provider_source_error(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True

    def acquire(*args):
        raise FullMarketProtocolError("stale owner", status_code=409, code="PROVIDER_WORK_FENCE")

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            with pytest.raises(ProviderWorkRecoveryBlockedError):
                runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert source.outcomes == []
        assert not runtime.state.pending_source_error_keys("p")
    finally:
        runtime.close()


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
@pytest.mark.parametrize("network", [False, True])
def test_generic_failure_reports_source_error_and_stops_before_next_member(
    tmp_path, provider, network
):
    runtime, source, _ = _runtime(tmp_path, provider=provider)
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        raise (
            httpx.ReadError("private SDK detail") if network else RuntimeError("private SDK detail")
        )

    runtime._acquire = acquire
    runtime._acquisition_allowed = lambda: True
    plan = _plan()
    runtime.state.record_plan(plan, provider)
    # A TAIFEX network failure is an exchange-report outage; the round still stops
    # but the member is neither charged nor reported.
    outage = provider == "taifex" and network
    try:
        with owned_unit_work(provider, tmp_path):
            assert not runtime._deliver_plan(
                plan,
                {"members": [_member(), {**_member(), "member_key": "MSFT"}]},
                runtime.feeds[0],
            )
        assert acquired == ["AAPL"]
        assert source.outcomes == (
            []
            if outage
            else [{"work_item_id": "fp1:123", "member_key": "AAPL", "reason": "source_error"}]
        )
        assert not runtime.state.pending_source_error_keys("p")
        assert "private SDK detail" not in json.dumps(runtime.state.plan_progress("p"))
    finally:
        runtime.close()


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("network", [False, True])
def test_last_attempt_report_failure_restarts_before_any_new_acquisition(
    tmp_path, prepared, network
):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    plan = _plan()
    key = "fp1:123:AAPL"
    runtime.state.record_plan(plan, runtime.provider)
    body = b'[{"idempotency_key":"original"}]'
    if prepared:
        runtime.state.prepare(key, body)
        runtime.state.save_sequence_receipt(key, "original", "original-run")
        runtime.state.finish(key, status="prepared", receipt="original-run")
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=? WHERE key=?", (FULL_WORK_MAX_ATTEMPTS - 1, key))
    acquisitions = []

    def acquire(member, *args):
        acquisitions.append(member["member_key"])
        raise RuntimeError("private provider detail")

    def fail_prepare(*args):
        raise RuntimeError("private delivery detail")

    runtime._acquire = acquire
    runtime.delivery.prepare = fail_prepare
    reports = []

    def outcome(plan_id, **kwargs):
        reports.append(kwargs)
        if len(reports) <= 2:
            if network:
                raise httpx.ReadError("private report detail")
            raise FullMarketProtocolError("Source unavailable", status_code=503)
        return {}

    source.outcome = outcome
    release = {"members": [_member(), {**_member(), "member_key": "MSFT"}]}
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            for _ in range(2):
                with pytest.raises((FullMarketProtocolError, httpx.ReadError)):
                    runtime._deliver_plan(plan, release, runtime.feeds[0])
                runtime.state = FullMarketState(runtime.state.path)
                assert runtime.state.pending_source_error_keys("p") == {key}
                with runtime.state.connection() as db:
                    row = db.execute(
                        "SELECT attempts,status FROM full_work WHERE key=?", (key,)
                    ).fetchone()
                    assert tuple(row) == (
                        FULL_WORK_MAX_ATTEMPTS,
                        "prepared" if prepared else "pending",
                    )
            assert not runtime._deliver_plan(plan, release, runtime.feeds[0], drain_only=True)
        assert acquisitions == ([] if prepared else ["AAPL"])
        assert (
            reports
            == [{"work_item_id": "fp1:123", "member_key": "AAPL", "reason": "source_error"}] * 3
        )
        assert not runtime.state.pending_source_error_keys("p")
        with runtime.state.connection() as db:
            row = db.execute(
                "SELECT attempts,status,reason FROM full_work WHERE key=?", (key,)
            ).fetchone()
            assert tuple(row) == (FULL_WORK_MAX_ATTEMPTS, "manual", "retry_exhausted")
        if prepared:
            assert runtime.state.prepared(key) == body
            assert runtime.state.sequence_receipt(key, "original") == "original-run"
            assert runtime.state.receipt(key) == "original-run"
    finally:
        runtime.close()
