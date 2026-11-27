"""A reservation spans all native steps, not only the final denied step."""

import time
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher import acquisition as acq
from findb_fetcher import provider_work as queue
from findb_fetcher.execution_metadata import historical_feed
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError, HistoricalWorkItem
from findb_fetcher.member_historical import MemberHistoricalRunner
from findb_fetcher.providers.finlab import FinLabSdkGateway
from provider_work_support import work_authorization
from test_full_market import _member, _plan, _runtime

DAY = date(2026, 10, 15)


@pytest.mark.parametrize(
    ("lane", "phase"),
    [
        (lane, phase)
        for lane in ("daily", "historical")
        for phase in ("before", "previous_field", "quota_before", "quota_after")
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
            return work_authorization("finlab", body) if route.endswith("/authorize") else {}

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
                # The member runner's real reservation, around an adapter whose member
                # acquisition is the same real SDK accounting as the daily lane.
                class Adapter:
                    member_batch_limit = 0

                    def pending_shared_fetch(self, *args):
                        return False

                    def acquire(self, member, context):
                        return acquire()

                    def member_outcome(self, exc):
                        return None

                    def close(self):
                        pass

                member = {
                    "member_key": "2330",
                    "symbol": "2330",
                    "provider_symbol": "2330",
                    "mapping_status": "mapped",
                }
                runner = MemberHistoricalRunner(
                    "finlab",
                    delivery=SimpleNamespace(),
                    raw_store=SimpleNamespace(),
                    http=SimpleNamespace(),
                    adapter=Adapter(),
                    limits={"max_response_bytes": 1_000_000},
                )
                item = HistoricalWorkItem(
                    item_id=UUID(int=1),
                    request_id=UUID(int=2),
                    request_key="original",
                    provider="finlab",
                    dataset_key="tw_equity_eod",
                    market="TW",
                    trade_date=DAY,
                    lease_token=UUID(int=3),
                )
                with pytest.raises(
                    (queue.ProviderWorkAcquisitionStopped, HistoricalWorkDeferredError)
                ):
                    runner._acquire(
                        owner,
                        item,
                        historical_feed("finlab", "tw_equity_eod"),
                        member,
                        [member],
                        "2330",
                        "historical-member:2330",
                    )
                with owner.journal.connection() as db:
                    attempts = db.execute("SELECT attempts FROM work_budget").fetchone()[0]
        dispatched = phase in {"previous_field", "quota_after"}
        assert attempts == int(dispatched)
        assert owner.dispatch_observations == int(dispatched)
        assert calls == (["login", "get"] if dispatched else [])
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


def test_daily_repeated_dispatched_stop_reaches_original_attempt_ceiling(tmp_path, monkeypatch):
    from findb_fetcher import full_market_state

    monkeypatch.setattr(full_market_state, "FULL_WORK_MAX_ATTEMPTS", 2)
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    dispatched = [False]
    calls = []

    def source(method, path, body):
        if path.endswith("/prepared"):
            return {"bodies": []}
        if path.endswith("/authorize") and dispatched[0]:
            raise FullMarketProtocolError(
                "stopped", status_code=409, code="PROVIDER_WORK_SUSPENDED"
            )
        return work_authorization("finlab", body) if path.endswith("/authorize") else {}

    owner = queue.ProviderWorkSession(
        SimpleNamespace(request=source),
        "finlab",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "journal.sqlite3", "original"),
    )
    runtime, _, _ = _runtime(tmp_path, provider="finlab")
    runtime._acquisition_allowed = lambda: True

    def get(_):
        calls.append("get")
        dispatched[0] = True
        return SimpleNamespace(index=(DAY,), columns=("2330",), loc={(DAY, "2330"): 42})

    sdk = SimpleNamespace(login=lambda _: None, data=SimpleNamespace(get=get))

    rounds = [0]

    def acquire(*args):
        # Each round dispatches a fresh dataset (durable responses would replay a
        # repeated one); the stop then rejects the next dataset's grant.
        rounds[0] += 1
        for field in ("first", "second"):
            FinLabSdkGateway("offline", _sdk=sdk).fetch_dataset(
                f"round-{rounds[0]}-{field}", target_date=DAY, symbols=["2330"]
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
                )
            ),
        ):
            for expected in (1, 2):
                dispatched[0] = False
                with runtime.state.connection() as db:
                    db.execute("UPDATE full_work SET next_at=0")
                assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
                with runtime.state.connection() as db:
                    row = db.execute("SELECT attempts,status,reason FROM full_work").fetchone()
                assert row[0] == expected
            assert tuple(row) == (2, "manual", "retry_exhausted")
            dispatched[0] = False
            runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
            assert calls == ["get", "get"]
    finally:
        queue._current.reset(token)
        owner.stop()
        runtime.close()
