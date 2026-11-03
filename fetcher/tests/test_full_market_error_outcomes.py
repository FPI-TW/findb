"""Source error reports survive retries without another provider acquisition."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.full_market_state import (
    FULL_WORK_MAX_ATTEMPTS,
    FULL_WORK_NO_DATA_BREAKER,
    FULL_WORK_NO_DATA_RETRY_SECONDS,
    FullMarketState,
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


@pytest.mark.parametrize(
    ("error", "category", "http_status", "provider_code"),
    [
        (
            "no_data",
            "no_data",
            400,
            400,
        ),
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
        assert scanned is (error == "no_data")
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


def test_no_data_member_keeps_the_round_going_with_bounded_retries(tmp_path):
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if member["member_key"] == "ACA":
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
            db.execute("UPDATE full_work SET next_at=0,attempts=? WHERE member_key='ACA'", (7,))
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(
                plan, _members("AAPL", "AACI", "ACA", "MSFT", gaps={"AACI"}), runtime.feeds[0]
            )
        assert _statuses(runtime)["ACA"] == ("manual", "no_data_exhausted", FULL_WORK_MAX_ATTEMPTS)
        assert not runtime.state.has_blocking_manual("p")
    finally:
        runtime.close()


def test_unbroken_no_data_run_is_bounded_and_ends_the_plan_as_systemic(tmp_path):
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    acquired = []

    def acquire(member, *args):
        acquired.append(member["member_key"])
        raise TwelveDataResponseError(
            "private-detail",
            status_code=400,
            provider_code=400,
            dispatched=True,
            category="no_data",
        )

    runtime._acquire = acquire
    keys = [f"M{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER + 10)]
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
            with runtime.state.connection() as db:
                db.execute("UPDATE full_work SET next_at=0")
        assert owner.settled[:-1] == [("retry", None)] * (FULL_WORK_MAX_ATTEMPTS - 1)
        assert owner.settled[-1] == ("failed", "MANUAL_INTERVENTION_REQUIRED")
        assert set(acquired) == set(keys[:FULL_WORK_NO_DATA_BREAKER])
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


def test_every_no_data_member_is_retried_and_only_exhaustion_ends_the_plan(tmp_path):
    # Production 11/02 shape: data first, then more no-data members than one round covers.
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    silent = [f"M{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER * 2 + 5)]
    keys = ["AAPL", *silent, "LATE"]
    acquired = []
    published = {"AAPL"}

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if member["member_key"] not in published:
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = keys
    runtime.state.record_plan(plan, runtime.provider)
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    owner = _SettleOwner()
    release = _members(*keys)

    def run_round():
        with owned_unit_work(runtime.provider, tmp_path):
            runtime._deliver_plan(plan, release, runtime.feeds[0])
        runtime._settle_queued_round(owner)
        # Production rounds took about two minutes, longer than the old 60-second backoff.
        with runtime.state.connection() as db:
            db.execute("UPDATE full_work SET next_at=next_at-120 WHERE reason='no_data'")
        return _statuses(runtime)

    try:
        # Rounds stopped by the breaker move on instead of repeating the same members.
        run_round()
        run_round()
        statuses = run_round()
        assert acquired == keys
        assert all(statuses[key] == ("pending", "no_data", 1) for key in silent)
        assert owner.delays[:2] == [60, 60]
        assert (
            FULL_WORK_NO_DATA_RETRY_SECONDS - 300
            < owner.delays[-1]
            <= (FULL_WORK_NO_DATA_RETRY_SECONDS)
        )

        # Nothing is called again while the whole retry set is in backoff.
        run_round()
        assert acquired == keys

        published.add("LATE")
        rounds = 0
        while owner.settled[-1][0] == "retry":
            with runtime.state.connection() as db:
                db.execute("UPDATE full_work SET next_at=0 WHERE reason='no_data'")
            run_round()
            rounds += 1
            assert rounds < 100
        statuses = _statuses(runtime)
        assert statuses["LATE"][0] == "complete"
        assert all(
            statuses[key] == ("manual", "no_data_exhausted", FULL_WORK_MAX_ATTEMPTS)
            for key in silent
        )
        assert owner.settled[-1] == ("failed", "MANUAL_INTERVENTION_REQUIRED")
    finally:
        runtime.close()


def test_no_data_retries_stay_short_until_the_plan_has_a_result(tmp_path):
    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True

    def acquire(member, *args):
        if member["member_key"] == "ACA":
            raise _no_data_error()
        return []

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["ACA", "MSFT", "ZZZ"]
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            runtime._deliver_plan(plan, _members("ACA", "MSFT", "ZZZ"), runtime.feeds[0])

        def wait():
            with runtime.state.connection() as db:
                row = db.execute("SELECT next_at FROM full_work WHERE member_key='ACA'")
                return row.fetchone()[0] - time.time()

        assert 0 < wait() <= 60
        with runtime.state.connection() as db:
            db.execute("UPDATE full_work SET next_at=0 WHERE member_key='ACA'")
        with owned_unit_work(runtime.provider, tmp_path):
            runtime._deliver_plan(plan, _members("ACA", "MSFT", "ZZZ"), runtime.feeds[0])
        assert FULL_WORK_NO_DATA_RETRY_SECONDS - 60 < wait() <= FULL_WORK_NO_DATA_RETRY_SECONDS
    finally:
        runtime.close()


def test_no_data_wait_only_applies_when_nothing_else_remains(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3", clock=lambda: 1000.0)
    plan = _plan()
    state.record_plan(plan, "twelve_data")
    state.finish("fp1:123:AAPL", status="pending", reason="no_data", now=1000, retry_after=3600)
    assert state.no_data_wait_seconds("p", now=1000) == 0
    state.finish("fp1:123:MSFT", status="complete")
    assert state.no_data_wait_seconds("p", now=1000) == 3600
    assert state.no_data_wait_seconds("p", now=5000) == 0
    state.finish("fp1:123:AAPL", status="manual", reason="no_data_exhausted")
    assert state.no_data_wait_seconds("p", now=1000) == 0


def test_no_data_exhaustion_only_ends_a_plan_that_obtained_nothing(tmp_path):
    state = FullMarketState(tmp_path / "state.sqlite3")
    plan = _plan()
    keys = [f"M{index:03d}" for index in range(FULL_WORK_NO_DATA_BREAKER)]
    plan["parts"][0]["member_keys"] = [*keys, "AAPL"]
    state.record_plan(plan, "twelve_data")
    for key in keys:
        state.finish(f"fp1:123:{key}", status="manual", reason="no_data_exhausted")
    assert state.has_blocking_manual("p")
    state.finish("fp1:123:AAPL", status="complete")
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
    "error",
    [
        {"status_code": 503, "category": "server_error"},
        {"status_code": 429},
        {"status_code": 400, "provider_code": 400, "category": "invalid_symbol"},
        {"status_code": 403, "provider_code": 403, "category": "plan_restricted"},
    ],
)
def test_other_provider_errors_still_stop_the_round_at_the_first_member(tmp_path, error):
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
            assert not runtime._deliver_plan(plan, _members("AAPL", "MSFT"), runtime.feeds[0])
        assert acquired == ["AAPL"]
        assert len(source.outcomes) == 1
    finally:
        runtime.close()


class _SettleOwner:
    work = {"kind": "daily"}

    def __init__(self):
        self.journal = SimpleNamespace(manifest=lambda: {"plan_id": "p"})
        self.settled = []
        self.delays = []

    def request(self, *_):
        return {}

    def prepared_keys(self):
        return []

    def settle(self, desired, **kwargs):
        self.settled.append((desired, kwargs.get("failure_code")))
        self.delays.append(kwargs.get("delay"))


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
