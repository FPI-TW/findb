"""Actual native boundaries cannot dispatch after waiting invalidates work authority."""

import json
import time
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from findb_fetcher import acquisition as acq
from findb_fetcher import provider_work as queue
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.providers.finlab import FinLabSdkGateway
from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
from sdk_bootstrap_harness import install_launcher


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji"])
@pytest.mark.parametrize(
    "fault",
    [
        "heartbeat_wait",
        "heartbeat_prepare",
        "heartbeat_auth",
        "generation_auth",
        "heartbeat_fence",
        "generation_fence",
    ],
)
def test_actual_http_finlab_sdk_and_shioaji_go_recheck_work_after_all_waits(
    tmp_path, monkeypatch, provider, fault
):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    clock = [time.time()]
    revoked = [False]
    calls = []
    fence_checks = []

    class Source:
        def request(self, method, route, body):
            if route.endswith("/authorize"):
                fence_checks.append(route)
                final_check = 4 if provider == "shioaji" else 2
                if len(fence_checks) == final_check and fault == "heartbeat_fence":
                    owner.failed.set()
                if len(fence_checks) == final_check and fault == "generation_fence":
                    owner.work["generation"] += 1
            if route.endswith("/authorize") and revoked[0]:
                raise FullMarketProtocolError(
                    "fence differs", status_code=409, code="PROVIDER_WORK_FENCE"
                )
            return {}

    def grant():
        if fault == "heartbeat_auth":
            owner.failed.set()
        elif fault == "generation_auth":
            revoked[0] = True
        return acq.Authorization(clock[0], clock[0], clock[0], provider=provider)

    def sleep(seconds):
        clock[0] += seconds
        if fault in {"heartbeat_wait", "heartbeat_prepare"}:
            owner.failed.set()

    monkeypatch.setattr(
        acq, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0], sleep=sleep)
    )
    output = tmp_path / "sdk.jsonl"
    if provider == "shioaji":
        install_launcher(monkeypatch, output)
    prepare = acq.Acquisition.prepare
    preparations = []

    def paced(operation):
        preparations.append(operation.kind)
        # For Shioaji this is specifically after child READY, before GO.
        target = 2 if provider == "shioaji" and fault != "heartbeat_prepare" else 1
        if fault in {"heartbeat_wait", "heartbeat_prepare"} and len(preparations) == target:
            with operation.state.connection() as db:
                operation._floor(db, clock[0] + 0.2, reason="pacing")
        return prepare(operation)

    monkeypatch.setattr(acq.Acquisition, "prepare", paced)
    with queue.host_lock(provider, root=tmp_path):
        owner = queue.ProviderWorkSession(
            Source(),
            provider,
            {"work_id": "original", "generation": 1, "token": "fence"},
            queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
        )
        token = queue._current.set(owner)
        sdk = None
        try:
            with acq.authorization_scope(grant, state_path=path):
                with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
                    if provider == "twelve_data":

                        def native(request):
                            calls.append("http")
                            return httpx.Response(200, request=request)

                        with httpx.Client(transport=httpx.MockTransport(native)) as client:
                            with acq.http_acquisition(
                                provider, client, url="https://offline.test", credential="offline"
                            ) as (_, extensions):
                                client.get("https://offline.test", extensions=extensions)
                    elif provider == "finlab":
                        native = SimpleNamespace(
                            login=lambda *_: calls.append("login"),
                            data=SimpleNamespace(get=lambda *_: calls.append("get")),
                        )
                        FinLabSdkGateway("offline", _sdk=native).fetch_dataset(
                            "price:收盤價", target_date=date(2026, 10, 15), symbols=["2330"]
                        )
                    else:
                        sdk = PersistentIsolatedShioajiGateway("offline", "offline-secret")
                        sdk.fetch_kbars("2330", date(2026, 10, 15))
            assert calls == []
            if output.exists():
                events = [json.loads(row)["event"] for row in output.read_text().splitlines()]
                assert not set(events) & {"create", "login", "contracts", "kbars"}
            with acq.FullMarketState(path).connection() as db:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    db.execute("SELECT COALESCE(SUM(requests),0) FROM full_quota").fetchone()[0]
                    == 0
                )
        finally:
            if sdk is not None:
                sdk.close()
            queue._current.reset(token)
            owner.stop()


def test_old_governor_floor_reopens_conservatively_and_never_erases_longer_unknown_debt(tmp_path):
    import sqlite3

    from findb_fetcher.full_market_state import FullMarketState

    path = tmp_path / "old-governor.sqlite3"
    now = time.time()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE full_pacing(account TEXT PRIMARY KEY,next_at REAL NOT NULL)")
        db.execute("INSERT INTO full_pacing VALUES(?,?)", ("account", now + 300))
    first = FullMarketState(path)
    with first.connection() as db:
        assert tuple(
            db.execute("SELECT not_before,reason FROM full_account_cooldown").fetchone()
        ) == (now + 300, "legacy")
        first.record_floor(db, "account", now + 0.2, reason="pacing")
    reopened = FullMarketState(path)
    with reopened.connection() as db:
        assert tuple(
            db.execute("SELECT not_before,reason FROM full_account_cooldown").fetchone()
        ) == (now + 300, "legacy")
        reopened.record_floor(db, "account", now + 400, reason="unknown_recovery")
        reopened.record_floor(db, "account", now + 350, reason="rate_limited")
    with FullMarketState(path).connection() as db:
        assert tuple(
            db.execute("SELECT not_before,reason FROM full_account_cooldown").fetchone()
        ) == (now + 400, "unknown_recovery")
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] == now + 400


def test_legacy_writer_extended_floor_after_upgrade_is_retained_on_reopen(tmp_path):
    from findb_fetcher.full_market_state import FullMarketState

    path = tmp_path / "governor.sqlite3"
    state = FullMarketState(path)
    with state.connection() as db:
        state.record_floor(db, "account", 100, reason="pacing")
        # An old binary only knows this existing table, not reason metadata.
        db.execute("UPDATE full_pacing SET next_at=400 WHERE account=?", ("account",))
    with FullMarketState(path).connection() as db:
        assert tuple(
            db.execute("SELECT not_before,reason FROM full_account_cooldown").fetchone()
        ) == (400, "legacy")


def test_cooldown_publish_lost_ack_keeps_unknown_debt_journal_and_host_quarantined(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "governor.sqlite3"))
    saved = []
    account_quarantines = len(acq._quarantined)
    host_quarantines = len(queue._quarantine)

    class Source:
        def request(self, method, path, body):
            if path.endswith("/heartbeat") and body.get("acquisition_not_before"):
                saved.append(body["acquisition_not_before"])
                raise FullMarketProtocolError("lost cooldown ACK", status_code=503, retryable=True)
            return {}

    journal = queue.WorkJournal(tmp_path / "work.sqlite3", "original")
    journal.reserve_attempt("original-native", 3)
    owner = queue.ProviderWorkSession(
        Source(), "twelve_data", {"work_id": "original", "generation": 2, "token": "fence"}, journal
    )
    token = queue._current.set(owner)
    try:
        with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
            with queue.host_lock("twelve_data", root=tmp_path):

                def grant():
                    return acq.Authorization(
                        time.monotonic(), time.time(), time.time(), provider="twelve_data"
                    )

                with acq.authorization_scope(grant):
                    with acq.Acquisition("twelve_data", account="account") as operation:
                        with operation.state.connection() as db:
                            db.execute(
                                "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,request_units,login_units,operation_kind) VALUES('old','account','twelve_data',?,'armed',300,1,0,'acquisition')",
                                (time.time() - 1,),
                            )
                        operation.recover()
                        operation.start()
        assert saved and journal.cooldown_until() > time.time() + 290
        with journal.connection() as db:
            assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == 1
        with acq.FullMarketState(tmp_path / "governor.sqlite3").connection() as db:
            assert (
                db.execute(
                    "SELECT status FROM acquisition_intent WHERE operation_id='old'"
                ).fetchone()[0]
                == "unknown"
            )
            assert db.execute("SELECT COALESCE(SUM(requests),0) FROM full_quota").fetchone()[0] == 0
        with pytest.raises(queue.ProviderWorkWaitingError):
            with queue.host_lock("twelve_data", root=tmp_path):
                pytest.fail("unacknowledged fence publication cannot release the host owner")
    finally:
        queue._current.reset(token)
        owner.stop()
        # This test creates no native child: its unknown intent is a synthetic
        # ledger row. After asserting quarantine, dispose only its own handles
        # so the same pytest thread can safely exercise later SDK boundaries.
        while len(acq._quarantined) > account_quarantines:
            acq._quarantined.pop().__exit__(None, None, None)
        while len(queue._quarantine) > host_quarantines:
            import os

            os.close(queue._quarantine.pop())


@pytest.mark.parametrize("always_slow", [False, True])
def test_slow_fresh_work_check_reauthorizes_with_original_dispatch_limit(
    tmp_path, monkeypatch, always_slow
):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    checks = []
    grants = []
    native = []

    class Source:
        def request(self, method, path, body):
            if path.endswith("/authorize"):
                checks.append(path)
                if len(checks) == 2 or (always_slow and len(checks) > 1):
                    time.sleep(0.06)
            return {}

    owner = queue.ProviderWorkSession(
        Source(),
        "twelve_data",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
    )
    token = queue._current.set(owner)

    def grant():
        grants.append(time.time())
        return acq.Authorization(time.monotonic(), time.time(), time.time(), provider="twelve_data")

    try:
        with (
            queue.host_lock("twelve_data", root=tmp_path),
            acq.authorization_scope(grant, state_path=path),
        ):
            with httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: (native.append(request), httpx.Response(200, request=request))[
                        1
                    ]
                )
            ) as client:

                def dispatch():
                    with acq.http_acquisition(
                        "twelve_data", client, url="https://offline.test", credential="offline"
                    ) as (_, extensions):
                        assert (
                            client.get("https://offline.test", extensions=extensions).status_code
                            == 200
                        )

                if always_slow:
                    with pytest.raises(acq.QuotaBlockedError) as failure:
                        dispatch()
                    assert not failure.value.quota_wait
                else:
                    dispatch()
        assert len(grants) == (3 if always_slow else 2) and len(native) == (0 if always_slow else 1)
        with acq.FullMarketState(path).connection() as db:
            assert db.execute(
                "SELECT count(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
            ).fetchone()[0] == (0 if always_slow else 1)
    finally:
        queue._current.reset(token)
        owner.stop()
