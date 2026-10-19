"""A reservation spans all native steps, not only the final denied step."""

import time
from datetime import date
from types import SimpleNamespace

import pytest

from findb_fetcher import acquisition as acq
from findb_fetcher import provider_work as queue
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError
from findb_fetcher.historical_runtime import _execute
from findb_fetcher.providers.finlab import FinLabSdkGateway
from findb_fetcher.scheduler_state import ScheduledJob
from findb_fetcher.twelve_data_scheduler import JobExecution
from test_full_market import _member, _plan, _runtime

DAY = date(2026, 10, 15)


@pytest.mark.parametrize(
    ("lane", "phase"),
    [
        (lane, phase)
        for lane in ("daily", "historical")
        for phase in ("before", "login", "previous_field", "quota_before", "quota_after")
    ]
    + [("maintenance", "quota_before"), ("maintenance", "quota_after")],
)
def test_suspension_retains_only_dispatched_reservation(tmp_path, monkeypatch, lane, phase):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "account.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    calls = []
    stop = [phase == "before"]

    class Source:
        def request(self, method, route, body):
            if route.endswith("/prepared"):
                return {"bodies": []}
            if route.endswith("/authorize") and stop[0]:
                raise FullMarketProtocolError(
                    "stopped", status_code=409, code="PROVIDER_WORK_SUSPENDED"
                )
            return {}

    owner = queue.ProviderWorkSession(
        Source(),
        "finlab",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "journal.sqlite3", "original"),
    )
    runtime, _, _ = _runtime(tmp_path, provider="finlab")
    runtime._acquisition_allowed = lambda: True

    def login(_):
        calls.append("login")
        stop[0] = phase == "login"

    def get(_):
        calls.append("get")
        return SimpleNamespace(index=(DAY,), columns=("2330",), loc={(DAY, "2330"): 42})

    sdk = SimpleNamespace(login=login, data=SimpleNamespace(get=get))

    def acquire(*args, **kwargs):
        if phase == "quota_before":
            raise QuotaBlockedError("predispatch quota", quota_wait=True)
        FinLabSdkGateway("offline", _sdk=sdk).fetch_dataset(
            "price:收盤價", target_date=DAY, symbols=["2330"]
        )
        if phase == "quota_after":
            raise QuotaBlockedError("next operation predispatch quota", quota_wait=True)
        if phase == "previous_field":
            stop[0] = True
            FinLabSdkGateway("offline", _sdk=sdk).fetch_dataset(
                "price:開盤價", target_date=DAY, symbols=["2330"]
            )
        pytest.fail("suspended acquisition must not complete")

    runtime._acquire = acquire
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, "finlab")
    token = queue._current.set(owner)
    try:
        with (
            queue.host_lock("finlab", root=tmp_path),
            acq.authorization_scope(
                lambda: acq.Authorization(
                    time.monotonic(), time.time(), time.time(), provider="finlab"
                ),
                state_path=path,
            ),
        ):
            if lane == "daily":
                assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
                with runtime.state.connection() as db:
                    attempts = db.execute("SELECT attempts FROM full_work").fetchone()[0]
            elif lane == "maintenance":
                owner.work.update(state="open", dataset_key=runtime.feeds[0]["dataset_key"])
                runtime.sync_universes = acquire
                runtime._settle_queued_round = lambda *args, **kwargs: None
                assert runtime._run_queued_maintenance(owner)[0]["status"] == "queued"
                with owner.journal.connection() as db:
                    attempts = db.execute("SELECT attempts FROM work_budget").fetchone()[0]
            else:
                # Use the real historical reservation wrapper and SDK accounting;
                # the executor seam mirrors the scheduler's public quota result.
                def execute(job, **kwargs):
                    try:
                        acquire()
                    except QuotaBlockedError as exc:
                        return JobExecution(
                            outcome="provider_rate_limited",
                            succeeded=False,
                            retryable=True,
                            predispatch_quota_wait=exc.quota_wait and not exc.dispatched,
                        )

                job = ScheduledJob(
                    job_key="original-job",
                    schedule_id="finlab",
                    scheduled_date=DAY,
                    universe_id="frozen",
                    universe_version=1,
                    symbol="2330",
                    canonical_symbol="2330",
                    exchange="TWSE",
                    status="pending",
                    attempt_count=0,
                    slot_id="daily",
                    provider="finlab",
                    dataset_key="tw_equity_eod",
                    work_item="original",
                    target_data_date=DAY,
                    checkpoint_before=None,
                    prepared_request=None,
                )
                with pytest.raises(
                    (queue.ProviderWorkAcquisitionStopped, HistoricalWorkDeferredError)
                ):
                    _execute(SimpleNamespace(execute=execute), SimpleNamespace(max_attempts=3), job)
                with owner.journal.connection() as db:
                    attempts = db.execute("SELECT attempts FROM work_budget").fetchone()[0]
        dispatched = phase in {"login", "previous_field", "quota_after"}
        assert attempts == int(dispatched)
        assert owner.dispatch_observations == int(dispatched)
        assert calls == (
            [] if not dispatched else ["login"] if phase == "login" else ["login", "get"]
        )
        with acq.FullMarketState(path).connection() as db:
            assert db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
            ).fetchone()[0] == int(dispatched)
            assert db.execute("SELECT COALESCE(SUM(requests),0) FROM full_quota").fetchone()[
                0
            ] == 2 * int(dispatched)
    finally:
        queue._current.reset(token)
        owner.stop()
        runtime.close()


def test_daily_repeated_login_stop_reaches_original_attempt_ceiling(tmp_path, monkeypatch):
    from findb_fetcher import full_market_state

    monkeypatch.setattr(full_market_state, "FULL_WORK_MAX_ATTEMPTS", 2)
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    logged_in = [False]
    calls = []

    def source(method, path, body):
        if path.endswith("/prepared"):
            return {"bodies": []}
        if path.endswith("/authorize") and logged_in[0]:
            raise FullMarketProtocolError(
                "stopped", status_code=409, code="PROVIDER_WORK_SUSPENDED"
            )
        return {}

    owner = queue.ProviderWorkSession(
        SimpleNamespace(request=source),
        "finlab",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "journal.sqlite3", "original"),
    )
    runtime, _, _ = _runtime(tmp_path, provider="finlab")
    runtime._acquisition_allowed = lambda: True

    def login(_):
        calls.append("login")
        logged_in[0] = True

    sdk = SimpleNamespace(
        login=login, data=SimpleNamespace(get=lambda _: pytest.fail("get after stop"))
    )
    runtime._acquire = lambda *args: FinLabSdkGateway("offline", _sdk=sdk).fetch_dataset(
        "price:收盤價", target_date=DAY, symbols=["2330"]
    )
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL"]
    runtime.state.record_plan(plan, "finlab")
    token = queue._current.set(owner)
    try:
        with (
            queue.host_lock("finlab", root=tmp_path),
            acq.authorization_scope(
                lambda: acq.Authorization(
                    time.monotonic(), time.time(), time.time(), provider="finlab"
                )
            ),
        ):
            for expected in (1, 2):
                logged_in[0] = False
                with runtime.state.connection() as db:
                    db.execute("UPDATE full_work SET next_at=0")
                assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
                with runtime.state.connection() as db:
                    row = db.execute("SELECT attempts,status,reason FROM full_work").fetchone()
                assert row[0] == expected
            assert tuple(row) == (2, "manual", "retry_exhausted")
            logged_in[0] = False
            runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
            assert calls == ["login", "login"]
    finally:
        queue._current.reset(token)
        owner.stop()
        runtime.close()
