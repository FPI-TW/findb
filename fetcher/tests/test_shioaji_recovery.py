"""Empty observations survive retries without turning absence into no_data."""

import hashlib
import json
from dataclasses import asdict
from datetime import date

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


def test_repair_ack_loss_does_not_replenish_grant_or_reset_checkpoint(tmp_path):
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

            def request(action, **kwargs):
                if action == "shioaji-repairs":
                    return {
                        "work_id": owner.work["work_id"],
                        "plan_id": plan["plan_id"],
                        "repairs": [repair],
                    }
                if action == "prepared":
                    return {"bodies": []}
                ack.append(1)
                if len(ack) == 1:
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
