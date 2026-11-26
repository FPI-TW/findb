"""Source error reports survive retries without another provider acquisition."""

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.full_market_state import (
    FULL_WORK_MAX_ATTEMPTS,
    FULL_WORK_NO_DATA_BREAKER,
    FULL_WORK_NO_DATA_BREAKER_HOLD_SECONDS,
    FULL_WORK_NO_DATA_FINAL_ROUND_LEAD_SECONDS,
    FULL_WORK_NO_DATA_MAX_ATTEMPTS,
    FULL_WORK_NO_DATA_RETRY_DELAYS,
    FullMarketState,
    QuotaBlockedError,
)
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


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "taifex"])
@pytest.mark.parametrize("network", [False, True])
def test_generic_failure_reports_source_error_and_only_a_network_failure_stops_the_round(
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
    plan = {**_plan(), "deadline_at": "2999-01-01T00:00:00+00:00"}
    runtime.state.record_plan(plan, provider)
    # A TAIFEX network failure is an exchange-report outage; the round still stops
    # but the member is neither charged nor reported.
    outage = provider == "taifex" and network
    try:
        with owned_unit_work(provider, tmp_path):
            scanned = runtime._deliver_plan(
                plan,
                {"members": [_member(), {**_member(), "member_key": "MSFT"}]},
                runtime.feeds[0],
            )
        # A network failure is no member's own and stops the round at once; any other
        # failure is reported and the next member still runs (two in a row stay
        # below the breaker).
        stops = outage or network
        assert scanned is not stops
        assert acquired == (["AAPL"] if stops else ["AAPL", "MSFT"])
        assert source.outcomes == (
            []
            if outage
            else [
                {"work_item_id": "fp1:123", "member_key": key, "reason": "source_error"}
                for key in (["AAPL"] if network else ["AAPL", "MSFT"])
            ]
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


@pytest.mark.parametrize(
    ("error", "category", "http_status", "provider_code"),
    [
        (
            "no_data",
            "no_data",
            400,
            400,
        ),
        ("not_found", "not_found", 404, 404),
        ("plan", "plan_restricted", 200, 403),
        ("generic", "exception:RuntimeError", None, None),
    ],
)
def test_member_failure_appends_bounded_diagnostic_without_provider_text(
    tmp_path, error, category, http_status, provider_code
):
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True

    def acquire(*args):
        if error == "no_data":
            raise TwelveDataResponseError(
                "No data is available private-detail",
                status_code=400,
                provider_code=400,
                dispatched=True,
                category="no_data",
            )
        if error == "not_found":
            raise TwelveDataResponseError(
                "private-detail", status_code=404, provider_code=404, dispatched=True
            )
        if error == "plan":
            raise TwelveDataResponseError(
                "private-detail", status_code=200, provider_code=403, dispatched=True
            )
        raise RuntimeError("private-detail")

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            scanned = runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        # Only one member: every failure that keeps the round going scans it to its end.
        assert scanned is True
        assert source.outcomes == [
            {"work_item_id": "fp1:123", "member_key": "AAPL", "reason": "source_error"}
        ]
        diagnostics = runtime.state.diagnostics("fp1:123:AAPL")
        assert [
            (row["category"], row["http_status"], row["provider_code"]) for row in diagnostics
        ] == [(category, http_status, provider_code)]
        health = runtime.state.health(runtime.provider)
        assert health["source_error_diagnostics"] == {category: 1}
        with runtime.state.connection() as db:
            dumped = json.dumps(
                [dict(row) for row in db.execute("SELECT * FROM full_work_diagnostic")]
            )
        assert "private-detail" not in dumped + json.dumps(health)
    finally:
        runtime.close()


def _members(*keys, gaps=()):
    return {
        "members": [
            {
                **_member(),
                "symbol": key,
                "provider_symbol": key,
                "member_key": key,
                "mapping_status": "gap" if key in gaps else "mapped",
            }
            for key in keys
        ]
    }


def _statuses(runtime):
    with runtime.state.connection() as db:
        return {
            row["member_key"]: (row["status"], row["reason"], row["attempts"])
            for row in db.execute("SELECT member_key,status,reason,attempts FROM full_work")
        }


@pytest.mark.parametrize(
    "reply",
    [
        {"status_code": 400, "provider_code": 400, "category": "no_data"},
        {"status_code": 404, "provider_code": 404},
    ],
    ids=["no_data", "not_found"],
)
def test_no_data_member_keeps_the_round_going_with_bounded_retries(tmp_path, reply):
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if member["member_key"] == "ACA":
            raise TwelveDataResponseError("private-detail", dispatched=True, **reply)
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL", "AACI", "ACA", "MSFT"]
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(
                plan, _members("AAPL", "AACI", "ACA", "MSFT", gaps={"AACI"}), runtime.feeds[0]
            )
        assert acquired == ["AAPL", "ACA", "MSFT"]
        assert [row["member_key"] for row in source.outcomes] == ["AACI", "ACA"]
        assert source.outcomes[1]["reason"] == "source_error"
        statuses = _statuses(runtime)
        assert statuses["ACA"] == ("pending", "no_data", 1)
        assert statuses["MSFT"][0] == "complete"
        assert statuses["AACI"][:2] == ("manual", "mapping_gap")
        with runtime.state.connection() as db:
            db.execute(
                "UPDATE full_work SET attempts=? WHERE member_key='ACA'",
                (FULL_WORK_NO_DATA_MAX_ATTEMPTS - 1,),
            )
        _skip_waits(runtime)
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(
                plan, _members("AAPL", "AACI", "ACA", "MSFT", gaps={"AACI"}), runtime.feeds[0]
            )
        assert _statuses(runtime)["ACA"] == (
            "manual",
            "no_data_exhausted",
            FULL_WORK_NO_DATA_MAX_ATTEMPTS,
        )
        # The last retry reports a final outcome after its own source error.
        assert [(row["member_key"], row["reason"]) for row in source.outcomes[-2:]] == [
            ("ACA", "source_error"),
            ("ACA", "no_data_exhausted"),
        ]
        assert sum(row["reason"] == "no_data_exhausted" for row in source.outcomes) == 1
        assert not runtime.state.has_blocking_manual("p")
        expected = reply.get("category", "not_found")
        assert {row["category"] for row in runtime.state.diagnostics("fp1:123:ACA")} == {expected}
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "reply",
    [
        {"status_code": 400, "provider_code": 400, "category": "no_data"},
        {"status_code": 404, "provider_code": 404},
    ],
    ids=["no_data", "not_found"],
)
def test_unbroken_no_data_run_is_bounded_and_ends_the_plan_as_systemic(tmp_path, reply):
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        raise TwelveDataResponseError("private-detail", dispatched=True, **reply)

    runtime._acquire = acquire
    bound = FULL_WORK_NO_DATA_BREAKER * FULL_WORK_MAX_ATTEMPTS
    keys = [f"M{index:03d}" for index in range(bound + 10)]
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    try:
        for number in range(1, FULL_WORK_MAX_ATTEMPTS + 1):
            with owned_unit_work(runtime.provider, tmp_path):
                assert not runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
            assert len(acquired) == FULL_WORK_NO_DATA_BREAKER * number
            runtime._settle_queued_round(owner)
            _skip_waits(runtime)
        assert owner.settled[:-1] == [("retry", None)] * (FULL_WORK_MAX_ATTEMPTS - 1)
        assert owner.settled[-1] == ("failed", "MANUAL_INTERVENTION_REQUIRED")
        # Each held round resumes with members not yet fetched in it, never the same run.
        assert acquired == keys[:bound]
    finally:
        runtime.close()


def test_data_between_no_data_members_resets_the_breaker(tmp_path):
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    keys = [f"M{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER * 3)]
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if keys.index(member["member_key"]) % FULL_WORK_NO_DATA_BREAKER:
            raise TwelveDataResponseError(
                "private-detail",
                status_code=400,
                provider_code=400,
                dispatched=True,
                category="no_data",
            )
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        assert acquired == keys
    finally:
        runtime.close()


def _no_data_error():
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    return TwelveDataResponseError(
        "private-detail",
        status_code=400,
        provider_code=400,
        dispatched=True,
        category="no_data",
    )


def _round(runtime, plan_id="p"):
    with runtime.state.connection() as db:
        row = db.execute(
            "SELECT round,not_before FROM full_no_data_round WHERE plan_id=?", (plan_id,)
        ).fetchone()
    return tuple(row) if row else None


def _skip_waits(runtime, plan_id="p"):
    """Settle the no-data retry round, then end every wait as if its time had passed."""
    runtime.state.advance_no_data_round(plan_id, now=time.time())
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET next_at=0")
        db.execute("UPDATE full_no_data_round SET not_before=0")


def test_persistent_no_data_run_in_retry_rounds_never_starves_the_tail(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    no_data = [f"N{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER + 10)]
    data = [f"D{index:03d}" for index in range(len(no_data))]
    # Interleaved so the first pass acquires data; retries then see only the no-data run.
    keys = [key for pair in zip(no_data, data, strict=True) for key in pair]
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if member["member_key"].startswith("N"):
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    try:
        for _ in range(FULL_WORK_NO_DATA_MAX_ATTEMPTS):
            with owned_unit_work(runtime.provider, tmp_path):
                assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
            runtime._settle_queued_round(owner)
            _skip_waits(runtime)
        assert acquired.count(no_data[-1]) == FULL_WORK_NO_DATA_MAX_ATTEMPTS
        assert len(acquired) == len(data) + len(no_data) * FULL_WORK_NO_DATA_MAX_ATTEMPTS
        statuses = _statuses(runtime)
        assert all(
            statuses[key] == ("manual", "no_data_exhausted", FULL_WORK_NO_DATA_MAX_ATTEMPTS)
            for key in no_data
        )
        assert not runtime.state.has_blocking_manual("p")
        # Only once nothing is left does the incomplete plan end for review.
        assert owner.settled[:-1] == [("retry", None)] * (FULL_WORK_NO_DATA_MAX_ATTEMPTS - 1)
        assert owner.settled[-1] == ("failed", "MANUAL_INTERVENTION_REQUIRED")
    finally:
        runtime.close()


def test_breaker_after_proven_data_still_counts_first_attempts_only(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    breaker = FULL_WORK_NO_DATA_BREAKER
    data = [f"A{index:03d}" for index in range(breaker)]
    fresh = [f"F{index:03d}" for index in range(breaker + 5)]
    keys = data + fresh
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if member["member_key"].startswith("F"):
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert not runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        assert acquired == data + fresh[:breaker]
        _skip_waits(runtime)
        acquired.clear()
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        # The held round resumes with the five left, too few to trip again.
        assert acquired == fresh[breaker:]
        _skip_waits(runtime)
        acquired.clear()
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        # The retry round's unbroken no-data run does not count.
        assert acquired == fresh
    finally:
        runtime.close()


def test_breaker_holds_every_pending_member_instead_of_advancing(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    keys = [f"M{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER + 10)]
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        raise _no_data_error()

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert not runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        assert acquired == keys[:FULL_WORK_NO_DATA_BREAKER]
        runtime._settle_queued_round(owner)
        assert owner.delays[-1] > FULL_WORK_NO_DATA_BREAKER_HOLD_SECONDS - 60
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        assert acquired == keys[:FULL_WORK_NO_DATA_BREAKER]
    finally:
        runtime.close()


def test_no_data_member_recovers_on_a_later_round(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    published = False

    def acquire(member, *args):
        if member["member_key"] == "AAPL" and not published:
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
        assert _statuses(runtime)["AAPL"] == ("pending", "no_data", 1)
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
        # Still backing off: the provider is not asked again this soon.
        assert _statuses(runtime)["AAPL"] == ("pending", "no_data", 1)
        published = True
        _skip_waits(runtime)
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
        assert _statuses(runtime)["AAPL"][0] == "complete"
    finally:
        runtime.close()


def test_no_data_retries_wait_longer_until_the_budget_is_spent(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    calls = 0

    def acquire(member, *args):
        nonlocal calls
        if member["member_key"] == "AAPL":
            calls += 1
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    assert FULL_WORK_NO_DATA_RETRY_DELAYS == (1800, 3600, 3600, 7200)
    # The first fetch, one round per delay, then the final round before the deadline.
    assert len(FULL_WORK_NO_DATA_RETRY_DELAYS) == FULL_WORK_NO_DATA_MAX_ATTEMPTS - 2
    assert FULL_WORK_NO_DATA_MAX_ATTEMPTS == FULL_WORK_MAX_ATTEMPTS + 1
    try:
        for attempt, delay in enumerate(FULL_WORK_NO_DATA_RETRY_DELAYS, start=1):
            with owned_unit_work(runtime.provider, tmp_path):
                runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
            assert _statuses(runtime)["AAPL"] == ("pending", "no_data", attempt)
            before = time.time()
            runtime._settle_queued_round(owner)
            after = time.time()
            # The wait starts at the round's settlement and the next attempt sleeps through it.
            not_before = _round(runtime)[1]
            assert before + delay <= not_before <= after + delay
            assert delay - 1 <= owner.delays[-1] <= delay + 1
            with owned_unit_work(runtime.provider, tmp_path):
                runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
            assert calls == attempt
            _skip_waits(runtime)
        with owned_unit_work(runtime.provider, tmp_path):
            runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
        # Past the general budget a no-data member still has its final round.
        assert _statuses(runtime)["AAPL"] == ("pending", "no_data", FULL_WORK_MAX_ATTEMPTS)
        runtime._settle_queued_round(owner)
        assert _round(runtime)[0] == FULL_WORK_NO_DATA_MAX_ATTEMPTS
        _skip_waits(runtime)
        with owned_unit_work(runtime.provider, tmp_path):
            runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
        assert _statuses(runtime)["AAPL"] == (
            "manual",
            "no_data_exhausted",
            FULL_WORK_NO_DATA_MAX_ATTEMPTS,
        )
        assert calls == FULL_WORK_NO_DATA_MAX_ATTEMPTS
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "deadline_in,expected_wait",
    [
        (7200, 7200 - FULL_WORK_NO_DATA_FINAL_ROUND_LEAD_SECONDS),
        (FULL_WORK_NO_DATA_FINAL_ROUND_LEAD_SECONDS - 60, 0),
        (-3600, 0),
        # A plan recorded without a deadline keeps the longest configured wait.
        (None, FULL_WORK_NO_DATA_RETRY_DELAYS[-1]),
    ],
    ids=["before_deadline", "inside_lead", "deadline_passed", "no_deadline"],
)
def test_final_no_data_round_is_anchored_to_the_plan_deadline(tmp_path, deadline_in, expected_wait):
    state = FullMarketState(tmp_path / "state.sqlite3")
    now = time.time()
    plan = _plan()
    if deadline_in is not None:
        plan["deadline_at"] = datetime.fromtimestamp(now + deadline_in, timezone.utc).isoformat()
    state.record_plan(plan, "twelve_data")
    with state.connection() as db:
        db.execute(
            "UPDATE full_work SET reason='no_data',attempts=?", (FULL_WORK_MAX_ATTEMPTS - 1,)
        )
        db.execute("INSERT INTO full_no_data_round VALUES('p',?,0)", (FULL_WORK_MAX_ATTEMPTS - 1,))
    # The round before the final one still waits from its own settlement.
    state.advance_no_data_round("p", now=now)
    assert state.no_data_retry_at("p") == now + FULL_WORK_NO_DATA_RETRY_DELAYS[-1]
    with state.connection() as db:
        db.execute("UPDATE full_work SET attempts=?", (FULL_WORK_MAX_ATTEMPTS,))
    state.advance_no_data_round("p", now=now)
    assert state.no_data_retry_at("p") == pytest.approx(now + expected_wait, abs=1)


def test_only_no_data_members_get_the_final_round(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3")
    state.record_plan(_plan(), "twelve_data")
    with state.connection() as db:
        db.execute("UPDATE full_work SET attempts=?", (FULL_WORK_MAX_ATTEMPTS,))
    state.finish("fp1:123:AAPL", status="pending", reason="no_data")
    state.finish("fp1:123:MSFT", status="pending", reason="source_error")
    with state.connection() as db:
        rows = dict(db.execute("SELECT member_key,status||':'||reason FROM full_work"))
    assert rows == {"AAPL": "pending:no_data", "MSFT": "manual:retry_exhausted"}
    with state.connection() as db:
        db.execute(
            "UPDATE full_work SET attempts=? WHERE member_key='AAPL'",
            (FULL_WORK_NO_DATA_MAX_ATTEMPTS,),
        )
    state.finish("fp1:123:AAPL", status="pending", reason="no_data")
    with state.connection() as db:
        assert db.execute("SELECT status,reason FROM full_work WHERE member_key='AAPL'").fetchone()[
            :
        ] == ("manual", "no_data_exhausted")


def test_only_no_data_uses_the_long_retry_schedule(tmp_path):
    runtime, _, _ = _runtime(tmp_path)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    now = time.time()
    runtime.state.finish("fp1:123:AAPL", status="pending", reason="source_error", now=now)
    try:
        with runtime.state.connection() as db:
            next_at = db.execute(
                "SELECT next_at FROM full_work WHERE member_key='AAPL'"
            ).fetchone()[0]
        assert next_at == now + 60
    finally:
        runtime.close()


def test_each_no_data_round_fetches_every_member_once_and_settles_once(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    no_data = ["N1", "N2", "N3"]
    keys = ["N1", "D1", "N2", "D2", "N3", "D3"]
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if member["member_key"].startswith("N"):
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    try:
        for number in range(1, FULL_WORK_NO_DATA_MAX_ATTEMPTS + 1):
            acquired.clear()
            with owned_unit_work(runtime.provider, tmp_path):
                assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
            assert acquired == (keys if number == 1 else no_data)
            runtime._settle_queued_round(owner)
            assert len(owner.settled) == number
            # Members fetched apart in a round wait together; nothing is due before the round.
            with owned_unit_work(runtime.provider, tmp_path):
                assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
            assert acquired == (keys if number == 1 else no_data)
            with runtime.state.connection() as db:
                db.execute("UPDATE full_no_data_round SET not_before=0")
        assert owner.settled[:-1] == [("retry", None)] * (FULL_WORK_NO_DATA_MAX_ATTEMPTS - 1)
        assert owner.settled[-1] == ("failed", "MANUAL_INTERVENTION_REQUIRED")
        # This plan carries no deadline, so its final round keeps the longest wait.
        assert owner.delays[:-1] == [
            pytest.approx(delay, abs=1)
            for delay in (*FULL_WORK_NO_DATA_RETRY_DELAYS, FULL_WORK_NO_DATA_RETRY_DELAYS[-1])
        ]
        statuses = _statuses(runtime)
        assert all(
            statuses[key] == ("manual", "no_data_exhausted", FULL_WORK_NO_DATA_MAX_ATTEMPTS)
            for key in no_data
        )
    finally:
        runtime.close()


def test_interrupted_no_data_round_resumes_without_refetching(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    keys = ["N1", "N2", "N3", "N4"]
    acquired = []
    quota_waiting = False

    def acquire(member, *args):
        if quota_waiting and member["member_key"] == "N3":
            raise QuotaBlockedError("account waiting", quota_wait=True)
        acquired.append(member["member_key"])
        raise _no_data_error()

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        runtime._settle_queued_round(owner)
        _skip_waits(runtime)
        assert _round(runtime)[0] == 2
        acquired.clear()
        quota_waiting = True
        with owned_unit_work(runtime.provider, tmp_path):
            assert not runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        assert acquired == ["N1", "N2"]
        # The wait refunds the attempt and keeps the member unfetched in this round.
        assert _statuses(runtime)["N3"] == ("pending", "no_data", 1)
        runtime._settle_queued_round(owner)
        assert _round(runtime) == (2, 0)
        assert owner.delays[-1] == 60
        # The round survives a restart of the local state.
        restarted = FullMarketState(tmp_path / "state.sqlite3")
        now = time.time()
        assert not restarted.eligible("fp1:123:N1", now=now + 61)
        assert restarted.eligible("fp1:123:N3", now=now + 61)
        quota_waiting = False
        acquired.clear()
        with runtime.state.connection() as db:
            db.execute("UPDATE full_work SET next_at=0")
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        assert acquired == ["N3", "N4"]
        before = time.time()
        runtime._settle_queued_round(owner)
        assert _round(runtime)[0] == 3
        assert _round(runtime)[1] >= before + FULL_WORK_NO_DATA_RETRY_DELAYS[1]
    finally:
        runtime.close()


@pytest.mark.parametrize("other", ["source_error", "normalizing"])
def test_earlier_member_retry_never_fetches_no_data_before_its_round(tmp_path, other):
    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    now = time.time()
    assert runtime.state.claim("fp1:123:AAPL", now=now)
    runtime.state.finish("fp1:123:AAPL", status="pending", reason="no_data", now=now)
    assert runtime.state.claim("fp1:123:MSFT", now=now)
    if other == "normalizing":
        # Normalization refunds the attempt, but the provider response is already held.
        runtime.state.prepare("fp1:123:MSFT", b"prepared delivery")
        runtime.state.finish("fp1:123:MSFT", status="prepared", reason="normalizing", now=now)
    else:
        runtime.state.finish("fp1:123:MSFT", status="pending", reason="source_error", now=now)
    owner = _SettleOwner()
    try:
        with runtime.state.connection() as db:
            assert db.execute("SELECT next_at FROM full_work WHERE member_key='AAPL'").fetchone()[
                0
            ] == pytest.approx(now)
        runtime._settle_queued_round(owner)
        assert owner.settled == [("retry", None)]
        assert owner.delays == [60]
        assert _round(runtime)[0] == 2
        assert runtime.state.eligible("fp1:123:MSFT", now=now + 61)
        assert not runtime.state.eligible("fp1:123:AAPL", now=now + 61)
        assert runtime.state.eligible(
            "fp1:123:AAPL", now=now + FULL_WORK_NO_DATA_RETRY_DELAYS[0] + 61
        )
    finally:
        runtime.close()


def test_plan_without_a_round_resumes_from_its_least_fetched_no_data_member(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3")
    plan = _plan()
    state.record_plan(plan, "twelve_data")
    # A plan in flight before retry rounds existed holds members at different attempts.
    with state.connection() as db:
        db.execute("UPDATE full_work SET reason='no_data',attempts=2 WHERE member_key='AAPL'")
        db.execute("UPDATE full_work SET reason='no_data',attempts=3 WHERE member_key='MSFT'")
    now = time.time()
    assert not state.eligible("fp1:123:AAPL", now=now)
    state.advance_no_data_round("p", now=now)
    later = now + FULL_WORK_NO_DATA_RETRY_DELAYS[1]
    assert state.no_data_retry_at("p") == later
    assert state.eligible("fp1:123:AAPL", now=later)
    assert not state.eligible("fp1:123:MSFT", now=later)
    # Settling again before the round has fetched anyone neither moves nor skips it.
    state.advance_no_data_round("p", now=later)
    assert state.no_data_retry_at("p") == later


@pytest.mark.parametrize(
    "other_status,expected_delay_above",
    [(None, FULL_WORK_NO_DATA_RETRY_DELAYS[0] - 60), ("prepared", None)],
)
def test_retry_round_waits_only_when_every_remaining_member_is_backing_off(
    tmp_path, other_status, expected_delay_above
):
    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    now = time.time()
    assert runtime.state.claim("fp1:123:AAPL", now=now)
    runtime.state.finish("fp1:123:AAPL", status="pending", reason="no_data", now=now)
    if other_status:
        runtime.state.prepare("fp1:123:MSFT", b"prepared delivery")
        runtime.state.finish("fp1:123:MSFT", status="prepared", reason="normalizing", now=now)
    else:
        runtime.state.finish("fp1:123:MSFT", status="complete")
    owner = _SettleOwner()
    try:
        runtime._settle_queued_round(owner)
        assert owner.settled == [("retry", None)]
        if expected_delay_above is None:
            assert owner.delays == [60]
        else:
            assert owner.delays[0] > expected_delay_above
    finally:
        runtime.close()


def test_no_data_starting_after_proven_data_ends_the_plan_with_bounded_calls(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    breaker = FULL_WORK_NO_DATA_BREAKER
    data = [f"A{index:03d}" for index in range(breaker + 5)]
    no_data = [f"F{index:03d}" for index in range(breaker * FULL_WORK_MAX_ATTEMPTS + 40)]
    keys = data + no_data
    acquired = []

    def acquire(member, *args):
        if member["member_key"].startswith("F"):
            acquired.append(member["member_key"])
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    try:
        for _ in range(FULL_WORK_MAX_ATTEMPTS):
            with owned_unit_work(runtime.provider, tmp_path):
                assert not runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
            runtime._settle_queued_round(owner)
            _skip_waits(runtime)
        assert owner.settled[:-1] == [("retry", None)] * (FULL_WORK_MAX_ATTEMPTS - 1)
        assert owner.settled[-1] == ("failed", "MANUAL_INTERVENTION_REQUIRED")
        # Every trip holds the first round, which resumes with members not yet fetched.
        assert acquired == no_data[: breaker * FULL_WORK_MAX_ATTEMPTS]
    finally:
        runtime.close()


def test_acquired_data_restarts_the_trip_count(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    # Each retry round trips on its last member.
    keys = [f"M{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER)]
    publishing = False

    def acquire(member, *args):
        if publishing:
            return []
        raise _no_data_error()

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    try:
        for _ in range(FULL_WORK_MAX_ATTEMPTS - 1):
            with owned_unit_work(runtime.provider, tmp_path):
                assert not runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
            _skip_waits(runtime)
        with runtime.state.connection() as db:
            assert db.execute("SELECT trips FROM full_no_data_trip").fetchone()[0] == (
                FULL_WORK_MAX_ATTEMPTS - 1
            )
        publishing = True
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
        with runtime.state.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM full_no_data_trip").fetchone()[0] == 0
        assert not runtime.state.has_blocking_manual("p")
    finally:
        runtime.close()


def test_exhausted_no_data_members_alone_never_end_the_plan(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3")
    plan = _plan()
    no_data = [f"N{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER * 2)]
    plan["parts"][0]["member_keys"] = [*no_data, "DONE"]
    state.record_plan(plan, "twelve_data")
    with state.connection() as db:
        db.execute(
            "UPDATE full_work SET status='manual',reason='no_data_exhausted' WHERE member_key LIKE 'N%'"
        )
    assert not state.has_blocking_manual("p")


def test_terminal_members_are_not_probed_again_in_a_tail_round(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: []
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AACI", "AAPL", "MSFT"]
    runtime.state.record_plan(plan, runtime.provider)
    runtime.state.finish("fp1:123:AACI", status="manual", reason="mapping_gap")
    runtime.state.finish("fp1:123:AAPL", status="complete")
    try:
        with owned_unit_work(runtime.provider, tmp_path) as owner:
            restored = []
            real_restore = owner.restore

            def restore(*args, **kwargs):
                restored.append(kwargs["member_key"])
                return real_restore(*args, **kwargs)

            owner.restore = restore
            assert runtime._deliver_plan(
                plan, _members("AACI", "AAPL", "MSFT", gaps={"AACI"}), runtime.feeds[0]
            )
        assert restored == ["MSFT"]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("error", "acquired_keys"),
    [
        # A rate limit is the account's: the round stops at once.
        ({"status_code": 429}, ["AAPL"]),
        # One symbol's provider error keeps its retries and the next symbol runs.
        ({"status_code": 503, "category": "server_error"}, ["AAPL", "MSFT"]),
        (
            {"status_code": 400, "provider_code": 400, "category": "invalid_symbol"},
            ["AAPL", "MSFT"],
        ),
        (
            {"status_code": 403, "provider_code": 403, "category": "plan_restricted"},
            ["AAPL", "MSFT"],
        ),
    ],
)
def test_only_an_account_wide_provider_error_stops_the_round_at_the_first_member(
    tmp_path, error, acquired_keys
):
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        raise TwelveDataResponseError("private-detail", dispatched=True, **error)

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            scanned = runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
        assert scanned is (len(acquired_keys) == 2)
        assert acquired == acquired_keys
        assert len(source.outcomes) == len(acquired_keys)
    finally:
        runtime.close()


class _SettleOwner:
    work = {"kind": "daily"}

    def __init__(self):
        self.journal = SimpleNamespace(manifest=lambda: {"plan_id": "p"})
        self.settled = []
        self.delays = []
        self.proofs = []

    def request(self, *_):
        return {}

    def prepared_keys(self):
        return []

    def settle(self, desired, **kwargs):
        self.settled.append((desired, kwargs.get("failure_code")))
        self.delays.append(kwargs.get("delay"))
        self.proofs.append(kwargs.get("proof"))


@pytest.mark.parametrize(
    "reason,pending,expected",
    [
        ("mapping_gap", True, ("retry", None)),
        ("no_data_exhausted", True, ("retry", None)),
        ("mapping_gap", False, ("failed", "MANUAL_INTERVENTION_REQUIRED")),
        ("retry_exhausted", True, ("failed", "MANUAL_INTERVENTION_REQUIRED")),
        (None, True, ("failed", "MANUAL_INTERVENTION_REQUIRED")),
        ("source_rejected", True, ("failed", "MANUAL_INTERVENTION_REQUIRED")),
        ("normalization_failed", True, ("failed", "MANUAL_INTERVENTION_REQUIRED")),
    ],
)
def test_settlement_only_lets_member_scoped_manuals_continue_the_plan(
    tmp_path, reason, pending, expected
):
    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    runtime.state.finish("fp1:123:AAPL", status="manual", reason=reason)
    if not pending:
        runtime.state.finish("fp1:123:MSFT", status="complete")
    owner = _SettleOwner()
    try:
        runtime._settle_queued_round(owner)
        assert owner.settled == [expected]
    finally:
        runtime.close()


def _gap(key: str, status: str = "unavailable", reason: str = "mapping_gap") -> dict:
    return {"member_key": key, "status": status, "reason": reason}


def test_a_plan_lacking_only_unavailable_members_settles_completed(tmp_path):
    """A member the provider has no symbol for is unavailable, so Source calls it complete."""
    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    summary = {
        **plan["summary"],
        "data": plan["summary"]["expected"] - 1,
        "missing": 0,
        "blocked": 0,
        "unavailable": 1,
        "mapping_gap": 1,
        "gaps": [_gap("AAPL")],
    }
    source.evaluate = lambda plan_id: {**plan, "status": "complete", "summary": summary}
    runtime.state.finish("fp1:123:AAPL", status="manual", reason="mapping_gap")
    runtime.state.finish("fp1:123:MSFT", status="complete")
    owner = _SettleOwner()
    try:
        runtime._settle_queued_round(owner)
        assert owner.settled == [("completed", None)]
        assert "mapping_gaps" not in owner.proofs[0]
        with runtime.state.connection() as db:
            assert db.execute("SELECT complete FROM full_plan").fetchone()[0] == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "summary",
    [
        # Another member is blocked for its own reason beside an unavailable one.
        {
            "missing": 0,
            "blocked": 1,
            "unavailable": 1,
            "mapping_gap": 1,
            "gaps": [_gap("AAPL"), _gap("MSFT", "blocked", "source_error")],
        },
        # Source still misses a member.
        {"missing": 1, "blocked": 0, "unavailable": 1, "mapping_gap": 1, "gaps": [_gap("AAPL")]},
    ],
)
def test_an_incomplete_plan_with_nothing_left_to_acquire_still_fails(tmp_path, summary):
    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {
        **plan,
        "status": "incomplete",
        "summary": {**plan["summary"], **summary},
    }
    runtime.state.finish("fp1:123:AAPL", status="manual", reason="mapping_gap")
    runtime.state.finish("fp1:123:MSFT", status="manual", reason="member_unavailable")
    owner = _SettleOwner()
    try:
        runtime._settle_queued_round(owner)
        assert owner.settled == [("failed", "MANUAL_INTERVENTION_REQUIRED")]
        assert "mapping_gaps" not in owner.proofs[0]
    finally:
        runtime.close()


def test_exhaustion_through_another_failure_after_no_data_still_blocks(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3")
    plan = _plan()
    state.record_plan(plan, "twelve_data")
    key = "fp1:123:AAPL"
    state.record_diagnostic(key, provider="twelve_data", category="no_data")
    with state.connection() as db:
        db.execute("UPDATE full_work SET attempts=? WHERE key=?", (FULL_WORK_MAX_ATTEMPTS, key))
    state.finish(key, status="pending", reason="source_rejected")
    assert state.has_blocking_manual("p")


def test_diagnostics_keep_history_and_summarize_latest_unfinished_class(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3", clock=lambda: 1.0)
    plan = _plan()
    state.record_plan(plan, "twelve_data")
    key = "fp1:123:AAPL"
    state.record_diagnostic(key, provider="twelve_data", category="server_error", http_status=503)
    state.record_diagnostic(
        key, provider="twelve_data", category="no_data", http_status=400, provider_code=400
    )
    assert [row["category"] for row in state.diagnostics(key)] == ["server_error", "no_data"]
    assert state.health("twelve_data")["source_error_diagnostics"] == {"no_data": 1}
    state.finish(key, status="complete")
    assert state.health("twelve_data")["source_error_diagnostics"] == {}
    assert len(state.diagnostics(key)) == 2


@pytest.mark.parametrize("category", ["", "Provider message", "exception:", "x" * 41, "a;drop"])
def test_diagnostic_category_vocabulary_is_bounded(tmp_path, category):
    from findb_fetcher.full_market_state import FullMarketStateError

    state = FullMarketState(tmp_path / "state.sqlite3")
    with pytest.raises(FullMarketStateError, match="category"):
        state.record_diagnostic("k", provider="twelve_data", category=category)


def test_out_of_range_codes_are_not_persisted(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3")
    state.record_diagnostic(
        "k", provider="twelve_data", category="http_error", http_status=1000, provider_code=-1
    )
    assert state.diagnostics("k")[0]["http_status"] is None
    assert state.diagnostics("k")[0]["provider_code"] is None


@pytest.mark.parametrize(
    ("failure", "reason", "category"),
    [
        ("http_429", "rate_limited", "rate_limited"),
        ("payload_429", "rate_limited", "rate_limited"),
        ("quota_dispatched", "source_error", "quota_blocked"),
    ],
)
def test_dispatched_rate_or_quota_failure_records_diagnostic(tmp_path, failure, reason, category):
    from findb_fetcher.full_market_state import QuotaBlockedError
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True

    def acquire(*args):
        if failure == "http_429":
            raise TwelveDataResponseError("limited", status_code=429, dispatched=True)
        if failure == "payload_429":
            raise TwelveDataResponseError(
                "limited", status_code=200, provider_code=429, dispatched=True
            )
        exc = QuotaBlockedError("quota", status_code=None)
        exc.dispatched = True
        raise exc

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert source.outcomes == [
            {"work_item_id": "fp1:123", "member_key": "AAPL", "reason": reason}
        ]
        assert [row["category"] for row in runtime.state.diagnostics("fp1:123:AAPL")] == [category]
    finally:
        runtime.close()


def test_predispatch_quota_wait_is_not_a_failure_diagnostic(tmp_path):
    from findb_fetcher.full_market_state import QuotaBlockedError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True

    def acquire(*args):
        raise QuotaBlockedError("quota wait", quota_wait=True)

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert source.outcomes == []
        assert runtime.state.diagnostics("fp1:123:AAPL") == []
    finally:
        runtime.close()


def test_existing_checkpoint_gains_diagnostic_table_without_rewriting_work(tmp_path):
    import sqlite3

    path = tmp_path / "state.sqlite3"
    state = FullMarketState(path)
    state.record_plan(_plan(), "twelve_data")
    state.prepare("fp1:123:AAPL", b"prepared delivery")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE full_work_diagnostic")
        before = db.execute("SELECT * FROM full_work ORDER BY key").fetchall()
    reopened = FullMarketState(path)
    reopened.record_diagnostic("fp1:123:AAPL", provider="twelve_data", category="server_error")
    assert reopened.health("twelve_data")["source_error_diagnostics"] == {"server_error": 1}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM full_work ORDER BY key").fetchall() == before
    assert reopened.prepared("fp1:123:AAPL") == b"prepared delivery"


@pytest.mark.parametrize("finished", [True, False])
def test_settlement_records_finished_runs_so_the_next_attempt_is_the_next_round(
    tmp_path, monkeypatch, finished
):
    from uuid import UUID

    from findb_fetcher import full_market_runtime as runtime_module
    from findb_fetcher.full_market_universe import canonical_bytes

    runtime, source, _ = _runtime(tmp_path)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    # AAPL waits for the next no-data round; MSFT was delivered late in this round.
    runtime.state.finish("fp1:123:AAPL", status="pending", reason="no_data", now=time.time())
    with runtime.state.connection() as db:
        db.execute("UPDATE full_work SET attempts=1 WHERE key='fp1:123:AAPL'")
    request = {
        "idempotency_key": "full:msft",
        "dataset_key": "us_equity_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
    }
    runtime.state.prepare("fp1:123:MSFT", canonical_bytes([request]))
    runtime.state.save_sequence_receipt("fp1:123:MSFT", "full:msft", str(UUID(int=7)))
    runtime.state.finish("fp1:123:MSFT", status="prepared", reason="normalizing", now=time.time())
    reads = []

    class Delivery:
        def get_run_status(self, run_id, *, expected):
            reads.append((run_id, expected.idempotency_key))
            return SimpleNamespace(
                status="completed" if finished else "processing",
                failed_records=0,
                total_records=1,
                success_records=1,
            )

    runtime.delivery = Delivery()
    monkeypatch.setattr(runtime_module, "NORMALIZING_SETTLE_WAIT_SECONDS", 0)
    owner = _SettleOwner()
    try:
        runtime._settle_queued_round(owner)
    finally:
        runtime.close()
    assert reads == [(UUID(int=7), "full:msft")]
    statuses = _statuses(runtime)
    assert owner.settled == [("retry", None)]
    if finished:
        # No near-immediate attempt just to read MSFT: the next one is the no-data round.
        assert statuses["MSFT"][0] == "complete"
        assert owner.delays[-1] > 25 * 60
    else:
        # A run still processing keeps its one-minute revisit.
        assert statuses["MSFT"][:2] == ("prepared", "normalizing")
        assert owner.delays[-1] <= 61


def _isolation_round(tmp_path, failures):
    """A Twelve Data round of four members; ``failures`` maps a member to its exception."""
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    acquired: list[str] = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        failure = failures.get(member["member_key"])
        if failure is not None:
            raise failure
        return []  # Served and proven without a trade: the member completes.

    runtime._acquire = acquire
    plan = _plan()
    keys = ["AAPL", "MSFT", "NVDA", "TSLA"]
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    with owned_unit_work(runtime.provider, tmp_path):
        scanned = runtime._deliver_plan(plan, _members(*keys), runtime.feeds[0])
    with runtime.state.connection() as db:
        rows = {
            row["member_key"]: (row["status"], row["reason"])
            for row in db.execute("SELECT member_key,status,reason FROM full_work")
        }
    return runtime, source, scanned, acquired, rows


def test_three_failures_in_a_row_stop_the_round_and_one_success_resets_the_count(tmp_path):
    from findb_fetcher.full_market_state import FULL_WORK_FAILURE_BREAKER

    assert FULL_WORK_FAILURE_BREAKER == 3
    boom = RuntimeError("private SDK detail")
    runtime, source, scanned, acquired, rows = _isolation_round(
        tmp_path, {"AAPL": boom, "MSFT": boom, "NVDA": boom}
    )
    try:
        # Three unclassified failures in a row may be the provider's: TSLA waits.
        assert scanned is False and acquired == ["AAPL", "MSFT", "NVDA"]
        assert [o["member_key"] for o in source.outcomes] == ["AAPL", "MSFT", "NVDA"]
        assert rows.get("TSLA", ("pending", None)) == ("pending", None)
    finally:
        runtime.close()

    runtime, source, scanned, acquired, rows = _isolation_round(
        tmp_path / "reset", {"AAPL": boom, "MSFT": boom, "TSLA": boom}
    )
    try:
        # NVDA succeeds between them, so no run of three forms and the round completes.
        assert scanned is True and acquired == ["AAPL", "MSFT", "NVDA", "TSLA"]
        assert rows["NVDA"][0] == "complete"
        assert {rows[key] for key in ("AAPL", "MSFT", "TSLA")} == {("pending", "source_error")}
    finally:
        runtime.close()


def test_one_intraday_symbol_sdk_failure_keeps_the_round_and_a_run_of_three_stops_it(
    tmp_path, monkeypatch
):
    """#747: one symbol's FinLab request failing is that member's, not an outage."""
    from findb_fetcher.provider_adapters.finlab import FinLabAdapter
    from findb_fetcher.provider_adapters.twelve_data import TwelveDataAdapter
    from findb_fetcher.providers.finlab_intraday import FinLabIntradaySymbolError

    classify = TwelveDataAdapter.classify

    def finlab_symbol(self, exc, phase):
        # Borrow FinLab's classification for its per-symbol intraday failures.
        if isinstance(exc, FinLabIntradaySymbolError):
            return FinLabAdapter.classify(self, exc, phase)
        return classify(self, exc, phase)

    monkeypatch.setattr(TwelveDataAdapter, "classify", finlab_symbol)

    def failed() -> FinLabIntradaySymbolError:
        return FinLabIntradaySymbolError("FinLab SDK intraday retrieval failed", "sdk_error")

    runtime, source, scanned, acquired, rows = _isolation_round(tmp_path, {"MSFT": failed()})
    try:
        # The other members are delivered in the same round; MSFT keeps its retries.
        assert scanned is True and acquired == ["AAPL", "MSFT", "NVDA", "TSLA"]
        assert rows["MSFT"] == ("pending", "source_error")
        assert [o["member_key"] for o in source.outcomes] == ["MSFT"]
    finally:
        runtime.close()

    runtime, source, scanned, acquired, rows = _isolation_round(
        tmp_path / "run", {"AAPL": failed(), "MSFT": failed(), "NVDA": failed()}
    )
    try:
        # Three in a row may be the provider's: the round stops before TSLA.
        assert scanned is False and acquired == ["AAPL", "MSFT", "NVDA"]
        assert rows.get("TSLA", ("pending", None)) == ("pending", None)
    finally:
        runtime.close()


def test_a_member_scoped_provider_failure_is_final_and_does_not_hold_the_plan(
    tmp_path, monkeypatch
):
    from findb_fetcher.provider_adapters.base import FailureKind, ProviderFailure
    from findb_fetcher.provider_adapters.twelve_data import TwelveDataAdapter

    class PlanRestrictedError(RuntimeError):
        pass

    classify = TwelveDataAdapter.classify

    def member_scoped(self, exc, phase):
        if isinstance(exc, PlanRestrictedError):
            return ProviderFailure(FailureKind.MEMBER, code="plan_restricted")
        return classify(self, exc, phase)

    monkeypatch.setattr(TwelveDataAdapter, "classify", member_scoped)
    runtime, source, scanned, acquired, rows = _isolation_round(
        tmp_path, {"AAPL": PlanRestrictedError(), "MSFT": PlanRestrictedError()}
    )
    try:
        # Outside the plan's coverage, say: no retry can change it, and nothing else waits.
        assert scanned is True and acquired == ["AAPL", "MSFT", "NVDA", "TSLA"]
        assert rows["AAPL"] == rows["MSFT"] == ("manual", "member_unavailable")
        assert [o["reason"] for o in source.outcomes] == ["source_error", "source_error"]
        assert not runtime.state.has_blocking_manual("p")
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("status", "continues"),
    [(409, True), (422, True), (401, False), (403, False), (503, False)],
)
def test_only_a_member_body_refusal_lets_the_round_continue(tmp_path, status, continues):
    from findb_fetcher.client import SourceAPIResponseError

    runtime, _, scanned, acquired, rows = _isolation_round(
        tmp_path, {"AAPL": SourceAPIResponseError(status)}
    )
    try:
        assert rows["AAPL"][1] == "source_rejected"
        # A refused body is that member's; credentials or a Source outage are everyone's.
        assert acquired == (["AAPL", "MSFT", "NVDA", "TSLA"] if continues else ["AAPL"])
        assert scanned is continues
    finally:
        runtime.close()


def test_only_a_member_published_providers_unpublished_member_does_not_hold_the_plan(tmp_path):
    runtime, _, _ = _runtime(tmp_path)
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL", "MSFT"]
    runtime.state.record_plan(plan, runtime.provider)
    try:
        runtime.state.finish("fp1:123:AAPL", status="manual", reason="member_not_published")
        assert not runtime.state.has_blocking_manual("p")
        # A cohort-published provider's unpublished date may be the provider's own state.
        runtime.state.finish("fp1:123:MSFT", status="manual", reason="target_not_ready")
        assert runtime.state.has_blocking_manual("p")
    finally:
        runtime.close()
