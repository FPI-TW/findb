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
from provider_work_support import work_authorization
from provider_worker_support import CA_BUNDLE, child_deadline, fake_finlab, install_fake_sdk
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
        "remote_fence",
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
            if (
                route.endswith("/authorize")
                and len(fence_checks) == final_check
                and fault == "remote_fence"
            ):
                raise FullMarketProtocolError(
                    "fence differs", status_code=409, code="PROVIDER_WORK_FENCE"
                )
            return work_authorization(provider, body) if route.endswith("/authorize") else {}

    def grant():
        if fault == "heartbeat_auth":
            owner.failed.set()
        elif fault == "generation_auth":
            owner.work["generation"] += 1
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
            return work_authorization("twelve_data", body) if path.endswith("/authorize") else {}

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
def test_slow_owner_rpc_precedes_fresh_dispatch_grant(tmp_path, monkeypatch, always_slow):
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
            return work_authorization("twelve_data", body) if path.endswith("/authorize") else {}

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

                dispatch()
        assert len(grants) == 1 and len(native) == 1
        with acq.FullMarketState(path).connection() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
                ).fetchone()[0]
                == 1
            )
    finally:
        queue._current.reset(token)
        owner.stop()


@pytest.mark.parametrize("boundary", ["http", "checkpoint"])
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "stop",
        "heartbeat",
        "generation",
        "token",
        "descriptor",
        "slow_grant",
        "slow_owner",
        "owner_expiry",
        "minute",
        "owner_missing",
        "owner_naive",
        "owner_identity",
        "owner_stale_check",
    ],
)
def test_fresh_grant_follows_owner_rpc_and_local_checks_fail_closed(
    tmp_path, monkeypatch, boundary, fault
):
    import os

    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    clock = [1792501200.0]
    monkeypatch.setattr(
        acq,
        "time",
        SimpleNamespace(
            time=lambda: clock[0],
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ),
    )
    events = []
    armed = [boundary == "http"]

    class Source:
        def request(self, method, route, body):
            if route.endswith("/authorize"):
                events.append("owner")
                if armed[0] and fault == "stop":
                    raise FullMarketProtocolError(
                        "stopped", status_code=409, code="PROVIDER_WORK_SUSPENDED"
                    )
                clock[0] += 5.01 if armed[0] and fault == "slow_owner" else 0.2
            response = {
                "work_id": "original",
                "provider": "twelve_data",
                **body,
                "checked_at": datetime_from(clock[0]),
                "expires_at": datetime_from(
                    clock[0] + (0.02 if fault == "owner_expiry" and armed[0] else 120)
                ),
            }
            if armed[0]:
                if fault == "owner_missing":
                    response.pop("expires_at")
                elif fault == "owner_naive":
                    response["expires_at"] = "2026-10-20T23:00:00"
                elif fault == "owner_identity":
                    response["token"] = "different"
                elif fault == "owner_stale_check":
                    response["checked_at"] = datetime_from(clock[0] - 6)
            return response

    def datetime_from(timestamp):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    def grant():
        events.append("grant")
        result = acq.Authorization(clock[0], clock[0], clock[0], provider="twelve_data")
        if armed[0]:
            if fault == "heartbeat":
                owner.failed.set()
            elif fault in {"generation", "token"}:
                owner.work[fault] = 99 if fault == "generation" else "different"
            elif fault == "descriptor":
                os.close(queue.inherited_fds()[0])
                # Replace the descriptor number, so merely checking existence
                # cannot mistake an unrelated inode for the locked host file.
                replacement = os.open(tmp_path / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
                assert replacement == queue.inherited_fds()[0]
            elif fault in {"slow_grant", "owner_expiry"}:
                clock[0] += 0.06 if fault == "slow_grant" else 0.03
            elif fault == "minute":
                clock[0] += 60 - clock[0] % 60
        return result

    journal = queue.WorkJournal(tmp_path / "work.sqlite3", "original")
    journal.reserve_attempt("original-operation", 8)
    owner = queue.ProviderWorkSession(
        Source(), "twelve_data", {"work_id": "original", "generation": 1, "token": "fence"}, journal
    )
    token = queue._current.set(owner)
    try:
        with (
            queue.host_lock("twelve_data", root=tmp_path),
            acq.authorization_scope(grant, state_path=path),
        ):

            def dispatch():
                if boundary == "http":
                    with httpx.Client(
                        transport=httpx.MockTransport(
                            lambda request: (
                                events.append("native"),
                                httpx.Response(200, request=request),
                            )[1]
                        )
                    ) as client:
                        with acq.http_acquisition(
                            "twelve_data", client, url="https://offline.test", credential="offline"
                        ) as (_, extensions):
                            client.get("https://offline.test", extensions=extensions)
                else:
                    with acq.Acquisition("twelve_data", credential="offline") as operation:
                        operation.start()
                        events.clear()
                        armed[0] = True
                        deadline = operation.dispatch_checkpoint()
                        assert clock[0] < deadline
                        events.append("native")

            if fault == "none":
                dispatch()
                assert events[-3:] == ["owner", "grant", "native"]
            elif fault == "stop":
                with pytest.raises(queue.ProviderWorkAcquisitionStopped):
                    dispatch()
            elif fault in {
                "heartbeat",
                "generation",
                "token",
                "descriptor",
                "owner_missing",
                "owner_naive",
                "owner_identity",
            }:
                with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
                    dispatch()
            else:
                with pytest.raises(acq.QuotaBlockedError, match="latency"):
                    dispatch()
                assert events.count("grant") == 3
        assert events.count("native") == (fault == "none")
        with acq.FullMarketState(path).connection() as db:
            starts = db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
            ).fetchone()[0]
            assert starts == (boundary == "checkpoint" or fault == "none")
            assert (
                db.execute("SELECT COALESCE(SUM(requests),0) FROM full_quota").fetchone()[0]
                == 2 * starts
            )
        with journal.connection() as db:
            assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == 1
    finally:
        queue._current.reset(token)
        owner.stop()


@pytest.mark.parametrize("fault", ["slow_owner", "stop_after_connect", "expiry_after_connect"])
def test_real_factset_child_connect_and_select_use_separate_fresh_grants(
    tmp_path, monkeypatch, fault
):
    from findb_fetcher.providers import factset

    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    events, connected = tmp_path / "events", tmp_path / "connected"
    # The fake clock drives only the parent; the exec'd worker sees its verdict.
    elapsed = [0.0]
    epoch = 1792501200.0

    def advance(seconds):
        elapsed[0] += seconds

    clock = SimpleNamespace(
        time=lambda: epoch + elapsed[0], monotonic=lambda: 1000 + elapsed[0], sleep=advance
    )
    monkeypatch.setattr(acq, "time", clock)
    checkpoint = acq.Acquisition.dispatch_checkpoint
    monkeypatch.setattr(
        acq.Acquisition,
        "dispatch_checkpoint",
        lambda self: child_deadline(checkpoint(self), clock.monotonic()),
    )
    grants = []

    class Source:
        def request(self, method, route, body):
            if connected.exists() and fault == "stop_after_connect":
                raise FullMarketProtocolError(
                    "stopped", status_code=409, code="PROVIDER_WORK_SUSPENDED"
                )
            advance(0.2)
            result = work_authorization("factset", body)
            if connected.exists() and fault == "expiry_after_connect":
                from datetime import datetime, timezone

                result["expires_at"] = datetime.fromtimestamp(
                    clock.time() + 0.02, timezone.utc
                ).isoformat()
            return result

    install_fake_sdk(
        monkeypatch,
        tmp_path / "sdk",
        {
            "pymysql/__init__.py": f"""
                from pymysql import cursors


                def record(event):
                    with open({str(events)!r}, "a") as stream:
                        stream.write(event + "\\n")


                class Cursor:
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        pass

                    def execute(self, sql, params=()):
                        if sql.startswith("SELECT"):
                            record("select")

                    def fetchmany(self, count):
                        return []


                class Connection:
                    def cursor(self):
                        return Cursor()

                    def rollback(self):
                        record("rollback")

                    def close(self):
                        record("close")


                def connect(**kwargs):
                    record("connect")
                    open({str(connected)!r}, "w").close()
                    return Connection()
            """,
            "pymysql/cursors.py": "SSDictCursor = object\n",
        },
    )
    ca = CA_BUNDLE
    owner = queue.ProviderWorkSession(
        Source(),
        "factset",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
    )
    token = queue._current.set(owner)

    def grant():
        grants.append(connected.exists())
        result = acq.Authorization(
            clock.monotonic(), clock.time(), clock.time(), provider="factset"
        )
        if connected.exists() and fault == "expiry_after_connect":
            advance(0.03)
        return result

    try:
        with (
            queue.host_lock("factset", root=tmp_path),
            acq.authorization_scope(grant, state_path=path),
        ):
            gateway = factset.FactSetGateway(
                factset.FactSetConfig("offline.invalid", "reader", "secret", ca)
            )
            if fault == "slow_owner":
                assert json.loads(gateway.prices(date(2026, 10, 15), ("J3TRKQ-L",)))["rows"] == []
            else:
                error = (
                    queue.ProviderWorkAcquisitionStopped
                    if fault == "stop_after_connect"
                    else acq.QuotaBlockedError
                )
                with pytest.raises(error):
                    gateway.prices(date(2026, 10, 15), ("J3TRKQ-L",))
        assert events.read_text().splitlines() == (
            ["connect", "select", "rollback", "close"] if fault == "slow_owner" else ["connect"]
        )
        assert grants == (
            [False, True]
            if fault == "slow_owner"
            else [False]
            if fault == "stop_after_connect"
            else [False, True, True, True]
        )
        with acq.FullMarketState(path).connection() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
                ).fetchone()[0]
                == 1
            )
            assert [row[0] for row in db.execute("SELECT requests FROM full_quota")] == [1, 1]
    finally:
        queue._current.reset(token)
        owner.stop()


def test_shioaji_slow_owner_rpc_does_not_consume_ready_go_grant(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    output = tmp_path / "sdk.jsonl"
    install_launcher(monkeypatch, output)
    events = []

    class Source:
        def request(self, method, route, body):
            events.append("owner")
            time.sleep(0.06)
            return work_authorization("shioaji", body)

    owner = queue.ProviderWorkSession(
        Source(),
        "shioaji",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
    )
    token = queue._current.set(owner)

    def grant():
        assert events[-1] == "owner"
        events.append("grant")
        now = time.time()
        return acq.Authorization(time.monotonic(), now, now, provider="shioaji")

    sdk = None
    try:
        with (
            queue.host_lock("shioaji", root=tmp_path),
            acq.authorization_scope(grant, state_path=path),
        ):
            sdk = PersistentIsolatedShioajiGateway("offline", "offline-secret")
            assert sdk.fetch_kbars("2330", date(2026, 10, 15))
            sdk.close()
            sdk = None
        native_events = [json.loads(row)["event"] for row in output.read_text().splitlines()]
        assert all(
            native_events.count(name) == count
            for name, count in (("create", 1), ("login", 1), ("contracts", 2), ("kbars", 1))
        )
        with acq.FullMarketState(path).connection() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM acquisition_intent WHERE actual_start IS NOT NULL AND operation_kind!='cleanup'"
                ).fetchone()[0]
                == 5
            )
    finally:
        if sdk is not None:
            sdk.close()
        queue._current.reset(token)
        owner.stop()


def test_owner_expiry_monotonic_cap_survives_backward_wall_clock(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    wall, mono = [1792501200.0], [1000.0]
    monkeypatch.setattr(
        acq, "time", SimpleNamespace(time=lambda: wall[0], monotonic=lambda: mono[0])
    )

    def authorize(method, route, body):
        response = work_authorization("twelve_data", body)
        response["expires_at"] = datetime.fromtimestamp(wall[0] + 0.02, timezone.utc).isoformat()
        return response

    owner = queue.ProviderWorkSession(
        SimpleNamespace(request=authorize),
        "twelve_data",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
    )
    token = queue._current.set(owner)
    try:
        with queue.host_lock("twelve_data", root=tmp_path):
            queue.require_acquisition("twelve_data")
            assert queue.require_acquisition("twelve_data", local_only=True)
            wall[0] -= 0.01  # Within allowed server skew; UTC expiry alone still passes.
            mono[0] += 0.03
            assert wall[0] < owner._authorization_expires
            assert not queue.require_acquisition("twelve_data", local_only=True)
    finally:
        queue._current.reset(token)
        owner.stop()


@pytest.mark.parametrize("bound", ["owner_age", "owner_expiry", "server_ahead"])
@pytest.mark.parametrize("started", [False, True])
def test_deferred_child_cannot_outlive_owner_deadline(tmp_path, monkeypatch, bound, started):
    from datetime import datetime, timezone

    from findb_fetcher import provider_process

    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    # The fake clock drives only the parent; the exec'd worker sees its verdict.
    elapsed = [0.0]
    epoch = 1792501200.0

    def advance(seconds):
        elapsed[0] += seconds

    clock = SimpleNamespace(
        time=lambda: epoch + elapsed[0], monotonic=lambda: 1000 + elapsed[0], sleep=advance
    )
    monkeypatch.setattr(acq, "time", clock)
    near_expiry = [False]

    def authorize(method, route, body):
        if near_expiry[0] and bound == "owner_age":
            advance(4.99)
        response = work_authorization("factset", body)
        if near_expiry[0] and bound in {"owner_expiry", "server_ahead"}:
            ahead = 0.2 if bound == "server_ahead" else 0
            response["checked_at"] = datetime.fromtimestamp(
                clock.time() + ahead, timezone.utc
            ).isoformat()
            response["expires_at"] = datetime.fromtimestamp(
                clock.time() + ahead + 0.01, timezone.utc
            ).isoformat()
        return response

    owner = queue.ProviderWorkSession(
        SimpleNamespace(request=authorize),
        "factset",
        {"work_id": "original", "generation": 1, "token": "fence"},
        queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
    )
    token = queue._current.set(owner)
    called = tmp_path / "native"
    install_fake_sdk(
        monkeypatch, tmp_path / "sdk", fake_finlab(f"open({str(called)!r}, 'w').close()")
    )

    def grant():
        return acq.Authorization(clock.monotonic(), clock.time(), clock.time(), provider="factset")

    try:
        with (
            queue.host_lock("factset", root=tmp_path),
            acq.authorization_scope(grant, state_path=path),
        ):
            with acq.Acquisition("factset", account="offline") as operation:
                if started:
                    operation.start()
                near_expiry[0] = True

                def checkpoint():
                    deadline = operation.dispatch_checkpoint()
                    assert deadline <= clock.monotonic() + 0.011
                    advance(0.02)  # IPC scheduling delay before child receives GO.
                    return child_deadline(deadline, clock.monotonic())

                with pytest.raises(provider_process.ProviderProcessError):
                    provider_process.supervised_call(
                        "finlab.get",
                        {"token": "offline", "dataset": "x"},
                        checkpoint=checkpoint,
                        timeout=5,
                    )
        assert not called.exists()
    finally:
        queue._current.reset(token)
        owner.stop()
