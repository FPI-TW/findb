"""Historical decisions and recovery use the session returned by require_owner."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher import provider_work
from findb_fetcher.client import SourceAPIResponseError
from findb_fetcher.historical_backfill import (
    HistoricalWorkDeferredError,
    _run_once,
)

RUN_ID = UUID("01982886-b452-7c71-b166-cb74cfd1d002")


def harness(monkeypatch, **work):
    events = []
    owner = SimpleNamespace(
        work={"state": "open", **work},
        drain_prepared=lambda: events.append("drain") or [],
        settle=lambda outcome, **kwargs: events.append(("settle", outcome, kwargs)),
    )
    monkeypatch.setattr(provider_work, "require_owner", lambda provider: owner)
    monkeypatch.setattr(
        provider_work, "active", lambda: pytest.fail("validated owner was discarded")
    )
    item = SimpleNamespace(provider="finlab")
    control = SimpleNamespace(
        provider="finlab",
        claim=lambda: events.append("claim") or item,
        authorize=lambda item: pytest.fail("fake runner should not dispatch"),
        complete=lambda item, run_id: events.append(("complete", run_id)),
        fail=lambda item, code: events.append(("fail", code)),
    )
    runner = SimpleNamespace(run=lambda item: events.append("run") or RUN_ID)
    return owner, control, runner, events


@pytest.mark.parametrize("has_receipt", [False, True])
def test_stopped_scope_drains_or_completes_without_acquisition(monkeypatch, has_receipt):
    owner, control, runner, events = harness(monkeypatch, scope_stopped=True)
    if has_receipt:
        owner.drain_prepared = lambda: events.append("drain") or [SimpleNamespace(run_id=RUN_ID)]
    assert _run_once(control, runner)
    assert events == [
        "claim",
        "drain",
        ("complete", RUN_ID)
        if has_receipt
        else ("settle", "cancelled", {"proof": {"reason": "historical_request_stopped"}}),
    ]


def test_open_drain_only_scope_retries_without_acquisition(monkeypatch):
    _, control, runner, events = harness(monkeypatch, drain_only=True)
    assert _run_once(control, runner)
    assert events == [
        "claim",
        "drain",
        ("settle", "retry", {"failure_code": "PROVIDER_ACCOUNT_COOLDOWN", "delay": 60}),
    ]


@pytest.mark.parametrize("state", ["closed", "draining"])
def test_closed_round_reuses_receipts_or_safely_retries(monkeypatch, state):
    _, control, runner, events = harness(monkeypatch, state=state)
    assert _run_once(control, runner)
    assert events == [
        "claim",
        "drain",
        ("settle", "retry", {"failure_code": "RECOVERED_CLOSED_ROUND", "delay": 60}),
    ]


@pytest.mark.parametrize(
    "fault,expected",
    [
        (HistoricalWorkDeferredError(), "DEFERRED"),
        (SourceAPIResponseError(429, retry_after=90), "SOURCE_TEMPORARILY_UNAVAILABLE"),
        (SourceAPIResponseError(503), "SOURCE_TEMPORARILY_UNAVAILABLE"),
    ],
)
def test_exception_settlement_uses_same_validated_owner(monkeypatch, fault, expected):
    _, control, runner, events = harness(monkeypatch)

    def fail(item):
        raise fault

    runner.run = fail
    assert _run_once(control, runner)
    assert events == [
        "claim",
        (
            "settle",
            "retry",
            {"failure_code": expected, "delay": max(60, int(getattr(fault, "retry_after", 0)))},
        ),
    ]


@pytest.mark.parametrize("failure_status", [None, 429, 503, 403])
def test_suspension_recovery_never_drops_owner(monkeypatch, failure_status):
    _, control, runner, events = harness(monkeypatch)

    def stopped(item):
        raise provider_work.ProviderWorkAcquisitionStopped()

    runner.run = stopped

    def fail(item, code):
        events.append(("fail", code))
        if failure_status and code == "ACQUISITION_SUSPENDED":
            raise SourceAPIResponseError(failure_status)

    control.fail = fail
    assert _run_once(control, runner)
    expected = ["claim", ("fail", "ACQUISITION_SUSPENDED")]
    if failure_status in {429, 503}:
        expected.append(("settle", "retry", {"failure_code": "ACQUISITION_SUSPENDED", "delay": 60}))
    elif failure_status == 403:
        expected.append(("fail", "PREPARED_DELIVERY_MANUAL_REVIEW"))
    assert events == expected


def test_empty_item_remains_a_legitimate_idle_result(monkeypatch):
    _, control, runner, events = harness(monkeypatch)
    control.claim = lambda: None
    assert not _run_once(control, runner)
    assert events == []


def test_source_fence_error_retains_owner_for_recovery(monkeypatch):
    _, control, runner, events = harness(monkeypatch)

    def fenced(item):
        raise SourceAPIResponseError(409, "PROVIDER_WORK_FENCE")

    runner.run = fenced
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        _run_once(control, runner)
    assert events == ["claim"]


def test_missing_owner_is_rejected_before_item_claim(monkeypatch):
    control = SimpleNamespace(provider="finlab", claim=lambda: pytest.fail("unowned claim"))
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        _run_once(control, SimpleNamespace())
