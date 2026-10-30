"""Empty observations survive retries without turning absence into no_data."""

import hashlib
import json
from dataclasses import asdict
from datetime import date
from unittest.mock import Mock

import httpx
import pytest

from findb_fetcher import provider_work as queue
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.providers.shioaji import ShioajiKbarsSnapshot, kbars_target_valid
from findb_fetcher.shioaji_recovery import (
    ShioajiObservationWaitError,
    install_grant,
    observation_guard,
    observation_status,
    observe_snapshot,
    record_kbars_not_dispatched,
)
from provider_work_support import owned_unit_work
from test_shioaji import _kbars

DAY = date(2026, 7, 29)


def snapshot(*, empty=False):
    return ShioajiKbarsSnapshot(
        {key: () if empty else tuple(value) for key, value in _kbars().items()}, 10, 20
    )


def native_fetch(owner, values, calls):
    @queue.durable_response(
        "shioaji_kbars",
        ("symbol", "target_date"),
        target_valid=kbars_target_valid,
        native_guard=observation_guard,
    )
    def fetch(symbol, target_date):
        owner.dispatch_observations += 1
        calls.append((symbol, target_date))
        value = next(values)
        observe_snapshot(value)
        return value

    return fetch


def test_empty_restart_wait_then_valid_replay_preserves_original_bytes(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    calls = []
    with owned_unit_work("shioaji", tmp_path) as owner:
        fetch = native_fetch(owner, iter([snapshot(empty=True), snapshot()]), calls)
        assert not fetch("2330", DAY).kbars["ts"]
        owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
        with pytest.raises(ShioajiObservationWaitError) as wait:
            fetch("2330", DAY)
        assert wait.value.delay == 900 and not wait.value.exhausted
        with owner.journal.connection() as db:
            observed = db.execute("SELECT body,sha256 FROM work_response_observation").fetchall()
        assert len(observed) == 1 and hashlib.sha256(observed[0][0]).hexdigest() == observed[0][1]
        clock[0] += 900
        assert fetch("2330", DAY).kbars["ts"]
        assert fetch("2330", DAY).kbars["ts"]
        assert len(calls) == 2
        with owner.journal.connection() as db:
            assert (
                db.execute("SELECT body,sha256 FROM work_response_observation").fetchall()
                == observed
            )


@pytest.mark.parametrize("legacy_valid", [False, True])
def test_legacy_cache_is_immutable_and_empty_counts_only_once(tmp_path, monkeypatch, legacy_valid):
    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    calls = []
    identity = json.dumps(
        {"symbol": "2330", "target_date": str(DAY)}, sort_keys=True, separators=(",", ":")
    )
    key = "shioaji_kbars:" + hashlib.sha256(identity.encode()).hexdigest()
    raw = json.dumps(
        {"type": "ShioajiKbarsSnapshot", "data": asdict(snapshot(empty=not legacy_valid))},
        sort_keys=True,
    ).encode()
    with owned_unit_work("shioaji", tmp_path) as owner:
        owner.journal.response(key, raw)
        fetch = native_fetch(owner, iter([snapshot(empty=True)] * 5), calls)
        if legacy_valid:
            assert fetch("2330", DAY).kbars["ts"]
            assert not calls
        else:
            with pytest.raises(ShioajiObservationWaitError) as legacy_wait:
                fetch("2330", DAY)
            assert legacy_wait.value.delay == 900 and not calls
            owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
            assert observation_status(owner.journal, "2330", DAY) == (1, 3)
            clock[0] += 900
            fetch("2330", DAY)
            assert observation_status(owner.journal, "2330", DAY) == (2, 3)
            clock[0] += 900
            fetch("2330", DAY)
            clock[0] += 900
            with pytest.raises(ShioajiObservationWaitError) as exhausted:
                fetch("2330", DAY)
            assert exhausted.value.exhausted and len(calls) == 2
            repair = {"provider_symbol": "2330", "repair_id": "approved", "observation_limit": 3}
            install_grant(owner.journal, repair, DAY)
            for _ in range(3):
                fetch("2330", DAY)
                clock[0] += 900
            install_grant(owner.journal, repair, DAY)
            owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
            with pytest.raises(ShioajiObservationWaitError):
                fetch("2330", DAY)
            assert len(calls) == 5
        assert owner.journal.response(key) == raw


@pytest.mark.parametrize("wait", [False, True])
def test_only_proven_predispatch_wait_releases_reservation(tmp_path, wait):
    with owned_unit_work("shioaji", tmp_path) as owner:
        error = (
            QuotaBlockedError("wait", quota_wait=True) if wait else RuntimeError("unknown dispatch")
        )
        with pytest.raises(type(error)):
            with observation_guard({"symbol": "2330", "target_date": DAY}, None):
                raise error
        assert observation_status(owner.journal, "2330", DAY) == (0 if wait else 1, 3)


def test_recovery_keeps_eight_old_failures_and_ack_loss_continuation(tmp_path):
    from test_full_market import _plan

    state = FullMarketState(tmp_path / "state.sqlite3")
    state.record_plan(_plan(), "shioaji")
    key = "fp1:123:AAPL"
    with state.connection() as db:
        db.execute("UPDATE full_work SET attempts=8,status='manual',reason='retry_exhausted'")
    state.apply_member_repair(key, "approved")
    assert state.claim(key, now=1000)
    state.stage_empty_response(key, now=1000, delay=900, exhausted=False)
    state = FullMarketState(state.path)
    assert state.pending_source_error_keys("p") == {key}
    state.acknowledge_empty(key)
    state.apply_member_repair(key, "approved")
    with state.connection() as db:
        assert tuple(db.execute("SELECT attempts,status,next_at FROM full_work").fetchone()) == (
            8,
            "pending",
            1900,
        )
    assert not state.claim(key, now=1899)
    assert state.claim(key, now=1900)
    state.stage_empty_response(key, now=1900, delay=900, exhausted=True)
    state.acknowledge_empty(key)
    state.apply_member_repair(key, "approved")
    assert not state.claim(key, now=4000)


def test_empty_outcome_source_outage_replays_without_native_or_budget_loss(tmp_path):
    from findb_fetcher.providers.shioaji import ShioajiEmptyResponseError
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path, provider="shioaji")
    runtime._acquisition_allowed = lambda: True
    runtime._calendar_repair_required = lambda owner: False
    calls = []

    def acquire(*args):
        calls.append(1)
        raise ShioajiEmptyResponseError("unknown")

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, "shioaji")
    outcomes = []

    def outcome(*args, **kwargs):
        outcomes.append(kwargs["reason"])
        if len(outcomes) == 1:
            raise httpx.ReadError("source unavailable")

    source.outcome = outcome
    try:
        with owned_unit_work("shioaji", tmp_path):
            with pytest.raises(httpx.ReadError):
                runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
            runtime.state = FullMarketState(runtime.state.path)
            assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert calls == [1] and outcomes == ["empty_response", "empty_response"]
        with runtime.state.connection() as db:
            assert tuple(db.execute("SELECT attempts,status,reason FROM full_work").fetchone()) == (
                0,
                "pending",
                "empty_response",
            )
    finally:
        runtime.close()


def test_calendar_revision_stops_refetch_before_native_io(tmp_path, monkeypatch):
    from findb_fetcher.provider_wait import CalendarDecisionRepairRequiredError

    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    calls = []
    with owned_unit_work("shioaji", tmp_path) as owner:
        fetch = native_fetch(owner, iter([snapshot(empty=True), snapshot()]), calls)
        fetch("2330", DAY)
        clock[0] += 900
        owner.request = lambda action: {
            "work_id": owner.work["work_id"],
            "trade_date": None,
            "repair_required": True,
        }
        with pytest.raises(CalendarDecisionRepairRequiredError):
            fetch("2330", DAY)
        assert len(calls) == 1 and observation_status(owner.journal, "2330", DAY) == (1, 3)


def test_checksum_failure_never_authorizes_replacement_fetch(tmp_path):
    calls = []
    with owned_unit_work("shioaji", tmp_path) as owner:
        fetch = native_fetch(owner, iter([snapshot()]), calls)
        fetch("2330", DAY)
        with owner.journal.connection() as db:
            db.execute("UPDATE work_response SET sha256=?", ("0" * 64,))
        with pytest.raises(queue.ProviderWorkRecoveryBlockedError, match="checksum"):
            fetch("2330", DAY)
        assert len(calls) == 1


def test_initial_unknown_dispatches_are_delayed_capped_and_survive_restart(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    arguments = {"symbol": "2330", "target_date": DAY}
    with owned_unit_work("shioaji", tmp_path) as owner:
        for count in range(1, 4):
            with pytest.raises(RuntimeError, match="uncertain"):
                with observation_guard(arguments, None):
                    owner.dispatch_observations += 1
                    raise RuntimeError("uncertain dispatch")
            assert observation_status(owner.journal, "2330", DAY) == (count, 3)
            owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
            with pytest.raises(ShioajiObservationWaitError) as wait:
                with observation_guard(arguments, None):
                    pytest.fail("unknown reservation authorized another native call")
            assert not wait.value.observed_empty
            assert wait.value.exhausted is (count == 3)
            if count < 3:
                assert wait.value.delay == 900
            clock[0] += 900
        with pytest.raises(ShioajiObservationWaitError) as exhausted:
            with observation_guard(arguments, None):
                pytest.fail("fourth unknown dispatch")
        assert exhausted.value.exhausted


def test_known_malformed_observations_keep_general_failure_policy(tmp_path):
    from dataclasses import replace

    malformed = replace(snapshot(), kbars={**snapshot().kbars, "Open": ()})
    with owned_unit_work("shioaji", tmp_path) as owner:
        for _ in range(4):
            with observation_guard({"symbol": "2330", "target_date": DAY}, None):
                owner.dispatch_observations += 1
                observe_snapshot(malformed)
        assert observation_status(owner.journal, "2330", DAY) == (0, 3)


@pytest.mark.parametrize("malformation", ["missing_column", "unequal_lengths", "bad_scalar"])
def test_gateway_decoder_failure_keeps_original_eight_attempt_policy(
    tmp_path, monkeypatch, malformation
):
    from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
    from test_full_market import _member, _plan, _runtime

    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    bars = _kbars()
    if malformation == "missing_column":
        bars.pop("Volume")
    elif malformation == "unequal_lengths":
        bars["Open"] = []
    else:
        bars["ts"] = [True]
    wire = {"code": "OK", "stage": "payload", "kbars": bars, "before": 10, "after": 20}
    gateway = PersistentIsolatedShioajiGateway.__new__(PersistentIsolatedShioajiGateway)
    monkeypatch.setattr(gateway, "_bootstrap", lambda: None)
    runtime, source, _ = _runtime(tmp_path, provider="shioaji")
    runtime._acquisition_allowed = lambda: True
    plan = _plan()
    runtime.state.record_plan(plan, "shioaji")
    calls = []
    try:
        with owned_unit_work("shioaji", tmp_path) as owner:

            def run(kind, **command):
                assert kind == "kbars"
                owner.dispatch_observations += 1
                calls.append(command)
                return wire, len(calls) - 1, len(calls)

            monkeypatch.setattr(gateway, "_run", run)
            runtime._acquire = lambda member, *args: gateway.fetch_kbars(
                member["provider_symbol"], DAY
            )
            for attempt in range(1, 9):
                assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
                assert len(calls) == owner.dispatch_observations == attempt
                assert observation_status(owner.journal, "AAPL", DAY) == (0, 3)
                with runtime.state.connection() as db:
                    row = db.execute("SELECT attempts,status,reason FROM full_work").fetchone()
                    assert tuple(row) == (
                        attempt,
                        "manual" if attempt == 8 else "pending",
                        "retry_exhausted" if attempt == 8 else "source_error",
                    )
                # Restart the journal between ordinary one-minute retries.
                owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
                clock[0] += 61
            with owner.journal.connection() as db:
                probes = db.execute(
                    "SELECT outcome,diagnostic FROM work_shioaji_probe ORDER BY probe_id"
                ).fetchall()
            assert len(probes) == 8 and all(row[0] == "invalid" for row in probes)
            assert all(json.loads(row[1])["error_reason"] == "ipc_schema" for row in probes)
        assert all(row["reason"] == "source_error" for row in source.outcomes)
    finally:
        runtime.close()


@pytest.mark.parametrize("reason", ["ipc_transport", "child_boundary", "contract_access", None])
def test_payload_error_without_format_evidence_retains_unknown_reservation(tmp_path, reason):
    from findb_fetcher.providers.shioaji import ShioajiSdkError

    error = ShioajiSdkError("private provider detail", code="PAYLOAD", reason=reason)
    with owned_unit_work("shioaji", tmp_path) as owner:
        with pytest.raises(ShioajiSdkError) as caught:
            with observation_guard({"symbol": "2330", "target_date": DAY}, None):
                owner.dispatch_observations += 1
                raise error
        assert caught.value is error
        assert observation_status(owner.journal, "2330", DAY) == (1, 3)
        with owner.journal.connection() as db:
            assert (
                "private provider detail"
                not in db.execute("SELECT diagnostic FROM work_shioaji_probe").fetchone()[0]
            )


@pytest.mark.parametrize("stage", ["sdk_create", "sdk_login", "TSE", "OTC"])
def test_bootstrap_failures_keep_eight_failure_attempts_without_kbars_debt(
    tmp_path, monkeypatch, stage
):
    from findb_fetcher.providers.shioaji import ShioajiSdkError
    from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
    from test_full_market import _member, _plan, _runtime

    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    gateway = PersistentIsolatedShioajiGateway.__new__(PersistentIsolatedShioajiGateway)
    gateway._constructed = gateway._logged_in = gateway._contracts_loaded = False
    runtime, source, _ = _runtime(tmp_path, provider="shioaji")
    runtime._acquisition_allowed = lambda: True
    plan = _plan()
    runtime.state.record_plan(plan, "shioaji")
    calls = []
    try:
        with owned_unit_work("shioaji", tmp_path) as owner:

            def run(kind, **command):
                assert kind != "kbars"
                owner.dispatch_observations += 1
                calls.append((kind, command))
                if command.get("exchange", kind) == stage:
                    raise ShioajiSdkError("bootstrap failed", code="ACQUISITION")
                return {}, len(calls) - 1, len(calls)

            monkeypatch.setattr(gateway, "_run", run)
            runtime._acquire = lambda member, *args: gateway.fetch_kbars(
                member["provider_symbol"], DAY
            )
            for attempt in range(1, 9):
                assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
                assert observation_status(owner.journal, "AAPL", DAY) == (0, 3)
                assert owner.dispatch_observations == len(calls) >= attempt
                with runtime.state.connection() as db:
                    assert tuple(
                        db.execute("SELECT attempts,status,reason FROM full_work").fetchone()
                    ) == (
                        attempt,
                        "manual" if attempt == 8 else "pending",
                        "retry_exhausted" if attempt == 8 else "source_error",
                    )
                owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
                clock[0] += 61
            with owner.journal.connection() as db:
                assert [row[0] for row in db.execute("SELECT outcome FROM work_shioaji_probe")] == [
                    "predispatch"
                ] * 8
        assert len(source.outcomes) == 8
    finally:
        runtime.close()


def test_bootstrap_predispatch_proof_preserves_prior_unknown_and_allows_later_data(
    tmp_path, monkeypatch
):
    from findb_fetcher.providers.shioaji import ShioajiSdkError
    from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway

    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    gateway = PersistentIsolatedShioajiGateway.__new__(PersistentIsolatedShioajiGateway)
    with owned_unit_work("shioaji", tmp_path) as owner:
        with pytest.raises(RuntimeError):
            with observation_guard({"symbol": "2330", "target_date": DAY}, None):
                raise RuntimeError("uncertain dispatch")
        clock[0] += 900

        def failed_bootstrap():
            owner.dispatch_observations += 1
            raise ShioajiSdkError("bootstrap failed", code="ACQUISITION")

        monkeypatch.setattr(gateway, "_bootstrap", failed_bootstrap)
        for _ in range(3):
            with pytest.raises(ShioajiSdkError):
                gateway.fetch_kbars("2330", DAY)
            assert observation_status(owner.journal, "2330", DAY) == (1, 3)
            owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
        record_kbars_not_dispatched()  # No active probe must not touch prior debt.
        with owner.journal.connection() as db:
            assert [row[0] for row in db.execute("SELECT outcome FROM work_shioaji_probe")] == [
                "unknown",
                "predispatch",
                "predispatch",
                "predispatch",
            ]
        monkeypatch.setattr(gateway, "_bootstrap", lambda: None)

        def run(kind, **command):
            owner.dispatch_observations += 1
            return (
                {"code": "OK", "stage": "payload", "kbars": _kbars(), "before": 10, "after": 20},
                3,
                4,
            )

        monkeypatch.setattr(gateway, "_run", run)
        assert gateway.fetch_kbars("2330", DAY).kbars["ts"]
        assert owner.dispatch_observations == 4
        assert observation_status(owner.journal, "2330", DAY) == (1, 3)


@pytest.mark.parametrize(
    "failure", ["wait", "exhausted", "calendar_closed", "calendar_outage", "calendar_invalid"]
)
def test_runtime_preserves_logged_in_gateway_on_observation_gate(tmp_path, monkeypatch, failure):
    from findb_fetcher.provider_wait import CalendarDecisionRepairRequiredError
    from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
    from test_full_market import _runtime

    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    runtime, _, _ = _runtime(tmp_path, provider="shioaji")
    runtime.runtime_window = lambda: {"open": True}
    gateway = PersistentIsolatedShioajiGateway.__new__(PersistentIsolatedShioajiGateway)
    gateway.is_alive = Mock(return_value=True)
    gateway.close = Mock()
    gateway._bootstrap = Mock()
    runtime.sdk = gateway
    identity = json.dumps(
        {"symbol": "2330", "target_date": str(DAY)}, sort_keys=True, separators=(",", ":")
    )
    key = "shioaji_kbars:" + hashlib.sha256(identity.encode()).hexdigest()
    raw = json.dumps(
        {"type": "ShioajiKbarsSnapshot", "data": asdict(snapshot(empty=True))}
    ).encode()
    try:
        with owned_unit_work("shioaji", tmp_path) as owner:
            owner.journal.response(key, raw)
            with pytest.raises(ShioajiObservationWaitError):
                runtime._shioaji_request("fetch_kbars", "2330", DAY)
            assert runtime.sdk is gateway
            if failure == "exhausted":
                for _ in range(2):
                    clock[0] += 900
                    with observation_guard({"symbol": "2330", "target_date": DAY}, None):
                        observe_snapshot(snapshot(empty=True))
            elif failure.startswith("calendar"):
                clock[0] += 900
                request = owner.request

                def calendar(action, **kwargs):
                    if action != "calendar-status":
                        return request(action, **kwargs)
                    if failure == "calendar_outage":
                        raise httpx.ReadError("unavailable")
                    return {
                        "work_id": "wrong"
                        if failure == "calendar_invalid"
                        else owner.work["work_id"],
                        "trade_date": owner.work.get("trade_date"),
                        "repair_required": failure == "calendar_closed",
                    }

                owner.request = calendar
            error = {
                "wait": ShioajiObservationWaitError,
                "exhausted": ShioajiObservationWaitError,
                "calendar_closed": CalendarDecisionRepairRequiredError,
                "calendar_outage": queue.ProviderWorkSourceUnavailableError,
                "calendar_invalid": queue.ProviderWorkRecoveryBlockedError,
            }[failure]
            for _ in range(3):
                with pytest.raises(error):
                    runtime._shioaji_request("fetch_kbars", "2330", DAY)
                assert runtime.sdk is gateway
                gateway.close.assert_not_called()
                gateway._bootstrap.assert_not_called()
            # A different eligible member uses the same authenticated gateway.
            gateway._run = Mock(
                return_value=(
                    {
                        "code": "OK",
                        "stage": "payload",
                        "kbars": _kbars(),
                        "before": 10,
                        "after": 20,
                    },
                    0,
                    1,
                )
            )
            assert runtime._shioaji_request("fetch_kbars", "2317", DAY).kbars["ts"]
            gateway._bootstrap.assert_called_once()
            assert runtime.sdk is gateway and owner.journal.response(key) == raw
            gateway.close.assert_not_called()
    finally:
        runtime.close()
    gateway.close.assert_called_once()


def test_runtime_replaces_dead_gateway_and_unknown_kbars_is_still_reserved(tmp_path, monkeypatch):
    from findb_fetcher import full_market_runtime as module
    from findb_fetcher.providers.shioaji import ShioajiSdkError
    from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
    from test_full_market import _runtime

    runtime, _, _ = _runtime(tmp_path, provider="shioaji")
    runtime.runtime_window = lambda: {"open": True}
    gateway = PersistentIsolatedShioajiGateway.__new__(PersistentIsolatedShioajiGateway)
    gateway.is_alive = Mock(return_value=True)
    gateway.close = Mock()
    gateway._bootstrap = Mock()
    replacement = Mock()
    factory = Mock(return_value=replacement)
    monkeypatch.setattr(module, "PersistentIsolatedShioajiGateway", factory)
    runtime.sdk = gateway
    try:
        with owned_unit_work("shioaji", tmp_path) as owner:

            def failed_kbars(*args, **kwargs):
                owner.dispatch_observations += 1
                # Native gateway failure cleanup reaps its child before returning.
                gateway.is_alive.return_value = False
                raise ShioajiSdkError("unknown native result", code="ACQUISITION")

            gateway._run = Mock(side_effect=failed_kbars)
            with pytest.raises(ShioajiSdkError):
                runtime._shioaji_request("fetch_kbars", "2330", DAY)
            assert observation_status(owner.journal, "2330", DAY) == (1, 3)
            runtime._shioaji_request("fetch_kbars", "2317", DAY)
            gateway.close.assert_called_once()
            factory.assert_called_once()
            replacement.fetch_kbars.assert_called_once_with("2317", DAY)
            assert runtime.sdk is replacement
    finally:
        runtime.close()


def test_calendar_outage_preserves_empty_report_and_original_attempts(tmp_path):
    from findb_fetcher.providers.shioaji import ShioajiEmptyResponseError
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path, provider="shioaji")
    runtime._acquisition_allowed = lambda: True
    acquired = []

    def acquire(*args):
        acquired.append(1)
        raise ShioajiEmptyResponseError("unknown")

    runtime._acquire = acquire

    def unavailable(owner):
        raise httpx.ReadError("calendar unavailable")

    runtime._calendar_repair_required = unavailable
    plan = _plan()
    runtime.state.record_plan(plan, "shioaji")
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=3")
    try:
        with owned_unit_work("shioaji", tmp_path):
            with pytest.raises(httpx.ReadError):
                runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
            runtime.state = FullMarketState(runtime.state.path)
            assert runtime.state.pending_source_error_keys("p") == {"fp1:123:AAPL"}
            assert runtime.state.empty_outcome("fp1:123:AAPL")
            with runtime.state.connection() as db:
                assert tuple(
                    db.execute("SELECT attempts,reason,lease_until FROM full_work").fetchone()
                ) == (3, "source_error_pending", 0)
            assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert acquired == [1] and source.outcomes[0]["reason"] == "empty_response"
    finally:
        runtime.close()


def test_unknown_wait_does_not_claim_an_empty_observation(tmp_path):
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path, provider="shioaji")
    runtime._acquisition_allowed = lambda: True
    runtime._calendar_repair_required = lambda owner: False

    def acquire(*args):
        raise ShioajiObservationWaitError(delay=900, observed_empty=False)

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, "shioaji")
    try:
        with owned_unit_work("shioaji", tmp_path):
            runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert source.outcomes[0]["reason"] == "source_error"
        with runtime.state.connection() as db:
            assert tuple(db.execute("SELECT attempts,reason FROM full_work").fetchone()) == (
                0,
                "unknown_dispatch",
            )
    finally:
        runtime.close()


def test_calendar_authorization_outage_before_refetch_spends_no_observation(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("findb_fetcher.shioaji_recovery.time.time", lambda: clock[0])
    with owned_unit_work("shioaji", tmp_path) as owner:
        with observation_guard({"symbol": "2330", "target_date": DAY}, None):
            owner.dispatch_observations += 1
            observe_snapshot(snapshot(empty=True))
        clock[0] += 900

        def unavailable(action):
            raise httpx.ReadError("calendar unavailable")

        owner.request = unavailable
        with pytest.raises(queue.ProviderWorkSourceUnavailableError):
            with observation_guard({"symbol": "2330", "target_date": DAY}, None):
                pytest.fail("calendar outage allowed native fetch")
        assert observation_status(owner.journal, "2330", DAY) == (1, 3)


@pytest.mark.parametrize("failure", ["ack", "grant"])
def test_repair_ack_loss_does_not_replenish_grant_or_reset_checkpoint(
    tmp_path, monkeypatch, failure
):
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path, provider="shioaji")
    plan = _plan()
    runtime.state.record_plan(plan, "shioaji")
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=8,status='manual',reason='retry_exhausted'")
    repair = {
        "repair_id": "approved",
        "member_key": "AAPL",
        "provider_symbol": "AAPL",
        "mapping_repaired": True,
        "observation_limit": 3,
    }
    source.request = lambda *args: {
        "work_id": "owned-unit-work",
        "plan_id": plan["plan_id"],
        "execution_profile": runtime.execution_profile,
        "dataset_key": plan["dataset_key"],
        "trade_date": plan["trade_date"],
        "members": [{"member_key": "AAPL", "status": "blocked"}],
    }
    try:
        with owned_unit_work("shioaji", tmp_path) as owner:
            ack = []
            grants = []
            original = install_grant

            def grant(*args):
                grants.append(1)
                if failure == "grant" and len(grants) == 1:
                    raise queue.ProviderWorkRecoveryBlockedError("grant interrupted")
                return original(*args)

            monkeypatch.setattr("findb_fetcher.shioaji_recovery.install_grant", grant)

            def request(action, **kwargs):
                if action == "shioaji-repairs":
                    return {
                        "work_id": owner.work["work_id"],
                        "plan_id": plan["plan_id"],
                        "repairs": [repair],
                    }
                if action == "prepared":
                    return {"bodies": []}
                assert action == "shioaji-repair-ack" and kwargs["repair_ids"] == ["approved"]
                ack.append(1)
                if failure == "ack" and len(ack) == 1:
                    raise queue.ProviderWorkRecoveryBlockedError("ACK lost")
                return {}

            owner._preparation_request = request
            with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
                runtime._install_shioaji_repairs(owner, plan, {"members": [_member()]})
            runtime.state = FullMarketState(runtime.state.path)
            result = runtime._install_shioaji_repairs(owner, plan, {"members": [_member()]})
            assert result["members"][0]["mapping_status"] == "mapped"
            with owner.journal.connection() as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM work_shioaji_grant WHERE grant_id='approved'"
                    ).fetchone()[0]
                    == 1
                )
            with runtime.state.connection() as db:
                assert tuple(db.execute("SELECT attempts,status FROM full_work").fetchone()) == (
                    8,
                    "pending",
                )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "progress",
    [
        "local_complete",
        "source_complete",
        "both_complete",
        "prepared",
        "receipt",
        "restored",
        "all_complete",
    ],
)
def test_repair_skips_terminal_and_prepared_members_and_only_acks_installed(tmp_path, progress):
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path, provider="shioaji")
    plan = _plan()
    runtime.state.record_plan(plan, "shioaji")
    keys = {symbol: "fp1:123:" + symbol for symbol in ["AAPL", "MSFT"]}
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=8,status='manual',reason='retry_exhausted'")
    if progress in {"local_complete", "both_complete", "receipt", "all_complete"}:
        runtime.state.finish(
            keys["AAPL"],
            status="complete",
            reason="source_proven_no_trade",
            receipt="original-receipt" if progress == "receipt" else None,
        )
    if progress == "all_complete":
        runtime.state.finish(keys["MSFT"], status="complete", reason="source_proven_no_trade")
    if progress == "prepared":
        runtime.state.prepare(keys["AAPL"], b"original prepared bytes")
    runtime.state.save_sequence_receipt(keys["AAPL"], "original-key", "original-run")
    with runtime.state.connection() as db:
        original = dict(
            db.execute("SELECT * FROM full_work WHERE key=?", (keys["AAPL"],)).fetchone()
        )
    repairs = [
        {
            "repair_id": "repair-" + symbol,
            "member_key": symbol,
            "provider_symbol": symbol,
            "mapping_repaired": True,
            "observation_limit": 3,
        }
        for symbol in keys
    ]
    members = [
        {**_member(), "member_key": symbol, "symbol": symbol, "provider_symbol": symbol}
        for symbol in keys
    ]
    source.request = lambda *args: {
        "work_id": "owned-unit-work",
        "plan_id": plan["plan_id"],
        "execution_profile": runtime.execution_profile,
        "dataset_key": plan["dataset_key"],
        "trade_date": plan["trade_date"],
        "members": [
            {
                "member_key": symbol,
                "status": "resolved"
                if symbol == "AAPL" and progress in {"source_complete", "both_complete"}
                else "blocked",
            }
            for symbol in keys
        ],
    }
    try:
        with owned_unit_work("shioaji", tmp_path) as owner:
            acknowledgements = []

            def request(action, **kwargs):
                if action == "shioaji-repairs":
                    return {
                        "work_id": owner.work["work_id"],
                        "plan_id": plan["plan_id"],
                        "repairs": repairs,
                    }
                assert action == "shioaji-repair-ack"
                acknowledgements.append(kwargs["repair_ids"])
                return {}

            owner._preparation_request = request
            owner.restore = lambda *, member_key: (
                ["original descriptor"] if progress == "restored" and member_key == "AAPL" else []
            )
            for _ in range(2):
                runtime.state = FullMarketState(runtime.state.path)
                owner.journal = queue.WorkJournal(owner.journal.path, owner.journal.key)
                result = runtime._install_shioaji_repairs(owner, plan, {"members": members})
                with runtime.state.connection() as db:
                    current = dict(
                        db.execute(
                            "SELECT * FROM full_work WHERE key=?", (keys["AAPL"],)
                        ).fetchone()
                    )
                    assert [
                        row[0] for row in db.execute("SELECT repair_id FROM full_member_repair")
                    ] == ([] if progress == "all_complete" else ["repair-MSFT"])
                if progress == "source_complete":
                    assert current["status"] == "complete" and current["attempts"] == 8
                else:
                    assert current == original
                assert (
                    runtime.state.sequence_receipt(keys["AAPL"], "original-key") == "original-run"
                )
                with owner.journal.connection() as db:
                    assert [
                        row[0]
                        for row in db.execute(
                            "SELECT grant_id FROM work_shioaji_grant WHERE grant_id!='initial'"
                        )
                    ] == ([] if progress == "all_complete" else ["repair-MSFT"])
            assert acknowledgements == (
                [] if progress == "all_complete" else [["repair-MSFT"], ["repair-MSFT"]]
            )
            if progress == "local_complete":
                acquired = []
                runtime._acquisition_allowed = lambda: True
                runtime._acquire = lambda member, *args: acquired.append(member["member_key"]) or []
                runtime._deliver_plan(plan, result, runtime.feeds[0])
                assert acquired == ["MSFT"]
    finally:
        runtime.close()
