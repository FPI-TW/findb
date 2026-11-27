"""Observe actual invocations, crash recovery and immutable authorization origins."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
import pytest

from findb_fetcher.acquisition import Acquisition, Authorization, authorization_scope
from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.platform_limits import account_identity
from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig
from provider_work_support import account_boundary_unit


def _grant() -> Authorization:
    now = time.time()
    return Authorization(time.monotonic(), now, now)


@pytest.fixture
def governor(tmp_path, monkeypatch):
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "shared-key")
    return path


@account_boundary_unit
def _worker(path: str, queue, count: int = 2) -> None:
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["TWELVE_DATA_API_KEY"] = "shared-key"
    with authorization_scope(_grant):
        for _ in range(count):
            with Acquisition("twelve_data") as operation:
                operation.start()
                queue.put(("start", time.time()))
                time.sleep(0.025)
                queue.put(("complete", time.time()))


@account_boundary_unit
def _crash(path: str, queue) -> None:
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["TWELVE_DATA_API_KEY"] = "shared-key"
    with authorization_scope(_grant), Acquisition("twelve_data") as operation:
        operation.prepare()
        queue.put(operation.operation_id)
        queue.close()
        queue.join_thread()
        os._exit(9)


@account_boundary_unit
def _headers_then_body(path: str, queue) -> None:
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["TWELVE_DATA_API_KEY"] = "shared-key"

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            queue.put("body")
            while True:
                time.sleep(1)
            yield b""

    with (
        authorization_scope(_grant),
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(429, headers={"Retry-After": "200"}, stream=Body())
            )
        ) as http,
    ):
        TwelveDataClient(TwelveDataConfig(api_key="shared-key"), client=http).fetch_daily("AAPL")


def test_processes_space_actual_starts_from_confirmed_completion(governor):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_worker, args=(str(governor), queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    events = [queue.get(timeout=10) for _ in range(8)]
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    events.sort(key=lambda event: event[1])
    assert [event[0] for event in events] == ["start", "complete"] * 4
    assert all(events[index + 1][1] - events[index][1] >= 60 / 1597 - 0.002 for index in (1, 3, 5))
    with FullMarketState(governor).connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE status='finalized'"
            ).fetchone()[0]
            == 4
        )
        assert (
            sum(
                row[0]
                for row in db.execute("SELECT requests FROM full_quota WHERE length(window)=10")
            )
            == 4
        )


def test_threads_serialize_same_account_and_other_account_remains_free(governor, monkeypatch):
    from threading import Event

    entered, release = Event(), Event()

    def slow():
        with authorization_scope(_grant), Acquisition("twelve_data") as operation:
            operation.start()
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(slow)
        assert entered.wait(5)
        began = time.monotonic()
        with authorization_scope(_grant), Acquisition("finlab") as operation:
            operation.start()
        assert time.monotonic() - began < 1
        release.set()
        future.result(timeout=5)


def test_delayed_durable_prepare_then_revocation_spends_no_request(governor, monkeypatch):
    original = Acquisition.prepare
    stopped = []

    def delayed(self):
        original(self)
        stopped.append(True)
        time.sleep(0.06)

    monkeypatch.setattr(Acquisition, "prepare", delayed)

    def denied():
        assert stopped
        raise QuotaBlockedError("revoked")

    with authorization_scope(denied), pytest.raises(QuotaBlockedError, match="revoked"):
        with Acquisition("twelve_data") as operation:
            operation.start()
            pytest.fail("provider must not start")
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0
        assert db.execute("SELECT status FROM acquisition_intent").fetchone()[0] == "cancelled"


def test_armed_process_death_records_one_unknown_and_full_cooldown(governor):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    worker = ctx.Process(target=_crash, args=(str(governor), queue))
    worker.start()
    identity = queue.get(timeout=10)
    worker.join(10)
    assert worker.exitcode == 9
    began = time.time()
    with Acquisition("twelve_data") as operation:
        operation.recover()
    with FullMarketState(governor).connection() as db:
        row = db.execute(
            "SELECT * FROM acquisition_intent WHERE operation_id=?", (identity,)
        ).fetchone()
        assert row["status"] == "unknown" and row["actual_start"] is None
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] >= began + 300
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM acquisition_intent WHERE status='unknown'").fetchone()[
                0
            ]
            == 1
        )


def test_kill_after_429_headers_keeps_early_cooldown_and_unknown_liability(governor):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    worker = ctx.Process(target=_headers_then_body, args=(str(governor), queue))
    worker.start()
    assert queue.get(timeout=10) == "body"
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] > time.time() + 190
    worker.kill()
    worker.join(10)
    now = time.time()
    with Acquisition("twelve_data"):
        pass
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] >= now + 300
        assert (
            db.execute("SELECT COUNT(*) FROM acquisition_intent WHERE status='unknown'").fetchone()[
                0
            ]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0


def _response(*, checked_at: float | None = None, stream=None):
    stamp = datetime.fromtimestamp(checked_at or time.time(), timezone.utc).isoformat()
    body = {
        "acquisition_allowed": True,
        "provider": "twelve_data",
        "checked_at": stamp,
        "max_age_seconds": 5,
    }
    return (
        httpx.Response(200, headers={"X-FinDB-Acquisition-Checked-At": stamp}, json=body)
        if stream is None
        else httpx.Response(
            200, headers={"X-FinDB-Acquisition-Checked-At": stamp}, stream=stream(stamp)
        )
    )


def test_body_decode_pause_does_not_refresh_receipt_origin():
    import json

    class SlowBody(httpx.SyncByteStream):
        def __init__(self, stamp):
            self.stamp = stamp

        def __iter__(self):
            time.sleep(0.06)
            yield json.dumps(
                {
                    "acquisition_allowed": True,
                    "provider": "twelve_data",
                    "checked_at": self.stamp,
                    "max_age_seconds": 5,
                }
            ).encode()

    with httpx.Client(transport=httpx.MockTransport(lambda _: _response(stream=SlowBody))) as http:
        grant = FullMarketSourceClient(
            FetcherConfig("https://source.example", "safe"), client=http
        ).authorization("GET", "test")
    assert not grant.valid()
    assert grant.deadline() <= grant.origin + 0.050


def test_realistic_source_latency_is_allowed_but_old_server_check_is_rejected():
    def response(_):
        checked = time.time()
        time.sleep(1)
        return _response(checked_at=checked)

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        assert (
            FullMarketSourceClient(FetcherConfig("https://source.example", "safe"), client=http)
            .authorization("GET", "test")
            .valid()
        )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: _response(checked_at=time.time() - 6))
    ) as http:
        with pytest.raises(FullMarketProtocolError, match="stale"):
            FullMarketSourceClient(
                FetcherConfig("https://source.example", "safe"), client=http
            ).authorization("GET", "test")


def test_lease_and_minute_bound_cap_grant_even_before_50ms():
    assert not Authorization(time.monotonic(), time.time(), time.time(), time.time() - 1).valid()
    grant = Authorization(100, 120.999, 120.999)
    assert grant.deadline() == 100.05
    assert Authorization(100, 179.999, 179.999).deadline() == pytest.approx(100.001)


def test_proven_whitespace_identity_merges_debt_once_without_current_byte_gate(
    governor, monkeypatch
):
    raw, normalized = " shared-key ", "shared-key"
    state = FullMarketState(governor)
    with state.connection() as db:
        for key, account, requests, byte_count in (
            (raw, "legacy-raw", 3, 600000000),
            (normalized, "legacy-normal", 4, 700000000),
        ):
            db.execute(
                "INSERT INTO full_account_identity VALUES('twelve_data',?,?)",
                (hashlib.sha256(key.encode()).hexdigest(), account),
            )
            db.execute(
                "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,?,?)",
                (account, "2026-10-01", requests, byte_count),
            )
        db.execute("INSERT INTO full_pacing VALUES('legacy-raw',1234)")
    monkeypatch.setenv("TWELVE_DATA_API_KEY", raw)
    assert account_identity("twelve_data", state) == "legacy-normal"
    assert account_identity("twelve_data", state) == "legacy-normal"
    with state.connection() as db:
        assert tuple(
            db.execute(
                "SELECT requests,bytes FROM full_quota WHERE account='legacy-normal'"
            ).fetchone()
        ) == (7, 1300000000)
        assert db.execute("SELECT COUNT(*) FROM account_alias_audit").fetchone()[0] == 1
        assert (
            db.execute("SELECT next_at FROM full_pacing WHERE account='legacy-normal'").fetchone()[
                0
            ]
            == 1234
        )
    with authorization_scope(_grant), Acquisition("twelve_data") as operation:
        # Historic incomparable scalar bytes never gate current acquisition.
        operation.start()


def test_response_hook_pause_preserves_header_origin_and_denies_actual_start(governor):
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: _response()),
        event_hooks={"response": [lambda _: time.sleep(0.06)]},
    ) as http:
        source = FullMarketSourceClient(
            FetcherConfig("https://source.example", "safe"), client=http
        )
        with authorization_scope(lambda: source.authorization("GET", "test")):
            with Acquisition("twelve_data") as operation:
                with pytest.raises(QuotaBlockedError, match="latency"):
                    operation.start()
                    calls.append("provider")
    assert calls == []
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0


def test_unsupported_provider_transport_fails_before_contact(governor):
    calls = []

    class Unsupported(httpx.BaseTransport):
        def handle_request(self, request):
            calls.append(request)
            return httpx.Response(200, json={})

    with httpx.Client(transport=Unsupported()) as http:
        with authorization_scope(_grant), pytest.raises(QuotaBlockedError, match="transport"):
            TwelveDataClient(TwelveDataConfig(api_key="shared-key"), client=http).fetch_daily(
                "AAPL"
            )
    assert calls == []
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0


def test_rollover_after_authorization_rechecks_and_charges_actual_day(governor, monkeypatch):
    current = [1767225599.999]
    monkeypatch.setattr(time, "time", lambda: current[0])
    calls = []

    def authorize():
        grant = _grant()
        calls.append(grant)
        if len(calls) == 1:
            current[0] += 1
        return grant

    with authorization_scope(authorize), Acquisition("twelve_data") as operation:
        operation.start()
        assert operation.started_at == current[0]
    assert len(calls) == 2
    with FullMarketState(governor).connection() as db:
        assert [row[0] for row in db.execute("SELECT window FROM full_quota ORDER BY window")] == [
            "2026-01-01",
            "2026-01-01T00:00",
        ]


def test_lock_wait_and_hold_reach_the_timing_line_and_long_ones_their_own(
    governor, monkeypatch, capsys
):
    import json
    from threading import Event

    from findb_fetcher import acquisition, rpc_timing

    rpc_timing.drain()
    monkeypatch.setattr(acquisition, "LONG_ACCOUNT_LOCK_SECONDS", 0.1)
    entered, release = Event(), Event()

    def holder():
        with authorization_scope(_grant), Acquisition("twelve_data") as operation:
            operation.start()
            entered.set()
            assert release.wait(5)
            time.sleep(0.15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(holder)
        assert entered.wait(5)
        release.set()
        # The same account: this one queues behind the holder for the lock.
        with authorization_scope(_grant), Acquisition("twelve_data") as operation:
            operation.start()
        future.result(timeout=5)

    totals = rpc_timing.drain()
    assert totals["account_lock wait"][0] == totals["account_lock hold"][0] == 2
    assert totals["account_lock wait"][1] >= 100
    lines = [
        json.loads(line)["account_lock"]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"account_lock"')
    ]
    # The holder held it past the threshold; the follower waited past it.
    assert sorted(entry["hold_seconds"] >= 0.1 for entry in lines) == [False, True]
    assert all(
        set(entry) == {"provider", "kind", "wait_seconds", "hold_seconds"} for entry in lines
    )
    assert {entry["provider"] for entry in lines} == {"twelve_data"}


SCOPE = "finlab_intraday"


@account_boundary_unit
def _crash_under(path: str, queue, scope) -> None:
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    with authorization_scope(_grant), Acquisition("finlab", lock_scope=scope) as operation:
        operation.prepare()
        queue.put(operation.operation_id)
        queue.close()
        queue.join_thread()
        os._exit(9)


def _crashed_intent(governor, scope) -> str:
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    worker = ctx.Process(target=_crash_under, args=(str(governor), queue, scope))
    worker.start()
    identity = queue.get(timeout=10)
    worker.join(10)
    assert worker.exitcode == 9
    return identity


def _intent(governor, operation_id):
    with FullMarketState(governor).connection() as db:
        return db.execute(
            "SELECT status,lock_scope FROM acquisition_intent WHERE operation_id=?",
            (operation_id,),
        ).fetchone()


def _recovery_floors(governor) -> int:
    with FullMarketState(governor).connection() as db:
        return db.execute(
            "SELECT COUNT(*) FROM full_account_cooldown WHERE reason='unknown_recovery'"
        ).fetchone()[0]


def test_a_scoped_acquisition_runs_beside_the_account_lock(governor):
    from threading import Event

    entered, release = Event(), Event()

    def whole_table():
        with authorization_scope(_grant), Acquisition("finlab") as operation:
            operation.start()
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(whole_table)
        assert entered.wait(5)
        began = time.monotonic()
        with authorization_scope(_grant), Acquisition("finlab", lock_scope=SCOPE) as operation:
            operation.start()
        # It never queued behind the account lock's holder.
        assert time.monotonic() - began < 1
        release.set()
        future.result(timeout=5)


def test_a_live_intent_of_another_scope_is_never_recovered(governor):
    from threading import Event

    armed, release = Event(), Event()
    identity: list[str] = []

    def partition():
        with authorization_scope(_grant), Acquisition("finlab", lock_scope=SCOPE) as operation:
            operation.prepare()
            identity.append(operation.operation_id)
            armed.set()
            assert release.wait(5)
            operation.start()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(partition)
        assert armed.wait(5)
        # The account lock's next request recovers on entry; the scoped intent is
        # still dispatching, so it stays armed and no recovery cooldown appears.
        with authorization_scope(_grant), Acquisition("finlab"):
            pass
        assert tuple(_intent(governor, identity[0])) == ("armed", SCOPE)
        assert _recovery_floors(governor) == 0
        release.set()
        future.result(timeout=5)
    assert _intent(governor, identity[0])["status"] == "finalized"


@pytest.mark.parametrize(
    ("crashed", "recovering"), [(SCOPE, None), (None, SCOPE), (SCOPE, SCOPE), (None, None)]
)
def test_an_intent_whose_lock_is_free_is_recovered_by_any_scope(governor, crashed, recovering):
    identity = _crashed_intent(governor, crashed)
    began = time.time()

    with authorization_scope(_grant), Acquisition("finlab", lock_scope=recovering):
        pass

    assert tuple(_intent(governor, identity)) == ("unknown", crashed)
    # The unknown dispatch still costs the whole account its cooldown.
    with FullMarketState(governor).connection() as db:
        assert db.execute("SELECT next_at FROM full_pacing").fetchone()[0] >= began + 300


def test_an_unknown_lock_scope_is_refused(governor):
    with pytest.raises(ValueError, match="scope"):
        Acquisition("finlab", lock_scope="anything")
