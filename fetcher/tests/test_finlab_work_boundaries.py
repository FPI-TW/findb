"""Real SDK child checkpoints and immutable negative table observations."""

import hashlib
import json
import multiprocessing
import os
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from findb_fetcher import acquisition as acq
from findb_fetcher import provider_work as queue
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.providers import finlab

DAY = date(2026, 10, 15)


class Frame:
    def __init__(self, day=DAY, value=42):
        self.index = (day,)
        self.columns = ("2330",)
        self.loc = {(day, "2330"): value}
        self.attrs = {"secret": "must-not-be-saved"}


@pytest.mark.parametrize("catalogue", [False, True])
@pytest.mark.parametrize("fault", ["stop", "heartbeat", "generation", "slow_login", "slow_fence"])
def test_real_child_reauthorizes_between_login_and_get(tmp_path, monkeypatch, catalogue, fault):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    logged_in, events = tmp_path / "logged-in", tmp_path / "events"
    grants = []
    before = {p.pid for p in multiprocessing.active_children()}
    # Share the dispatch clock with the real forked child. Advance it at the
    # blocking boundaries, so scheduler load cannot randomly exhaust a 50ms
    # grant between IPC messages in an otherwise successful-path test.
    from findb_fetcher import provider_process

    elapsed = multiprocessing.get_context("fork").Value("d", 0)
    epoch = time.time()

    def advance(seconds):
        with elapsed.get_lock():
            elapsed.value += seconds
        time.sleep(min(seconds, 0.001))

    clock = SimpleNamespace(
        time=lambda: epoch + elapsed.value,
        monotonic=lambda: 1000 + elapsed.value,
        sleep=advance,
    )
    monkeypatch.setattr(acq, "time", clock)
    monkeypatch.setattr(provider_process, "time", clock)

    class Source:
        def request(self, method, route, body):
            if route.endswith("/authorize") and logged_in.exists():
                if fault == "stop":
                    raise FullMarketProtocolError(
                        "stopped", status_code=409, code="PROVIDER_WORK_SUSPENDED"
                    )
                if fault == "heartbeat":
                    owner.failed.set()
                if fault == "generation":
                    owner.work["generation"] += 1
                if fault == "slow_fence":
                    advance(0.06)
            return {}

    def login(_):
        with events.open("a") as stream:
            stream.write(f"login:{os.getpid()}\n")
        logged_in.touch()
        advance(0.08)

    def get(_):
        with events.open("a") as stream:
            stream.write(f"get:{os.getpid()}\n")
        return Frame()

    native = SimpleNamespace(login=login, data=SimpleNamespace(get=get))
    original_import = finlab.importlib.import_module
    monkeypatch.setattr(
        finlab.importlib,
        "import_module",
        lambda name: native if name == "finlab" else original_import(name),
    )
    journal = queue.WorkJournal(tmp_path / "work.sqlite3", "original")
    journal.reserve_attempt("dataset", 3)
    owner = queue.ProviderWorkSession(
        Source(), "finlab", {"work_id": "original", "generation": 1, "token": "fence"}, journal
    )
    token = queue._current.set(owner)

    def grant():
        grants.append((os.getpid(), logged_in.exists()))
        return acq.Authorization(clock.monotonic(), clock.time(), clock.time(), provider="finlab")

    def fetch():
        gateway = finlab.FinLabSdkGateway("offline")
        if catalogue:
            return gateway.fetch_catalogue(symbols=["2330"])
        return gateway.fetch_dataset("price:收盤價", target_date=DAY, symbols=["2330"])

    try:
        with (
            queue.host_lock("finlab", root=tmp_path),
            acq.authorization_scope(grant, state_path=path),
        ):
            if fault == "slow_login":
                assert fetch().symbols == ("2330",)
            else:
                error = (
                    queue.ProviderWorkAcquisitionStopped
                    if fault == "stop"
                    else finlab.FinLabOperationalError
                    if fault == "slow_fence"
                    else queue.ProviderWorkRecoveryBlockedError
                )
                with pytest.raises(error):
                    fetch()
        observed = events.read_text().splitlines()
        assert len(observed) == (2 if fault == "slow_login" else 1)
        assert all(int(row.split(":")[1]) != os.getpid() for row in observed)
        assert len({row.split(":")[1] for row in observed}) == 1
        assert all(pid == os.getpid() for pid, _ in grants)
        assert 1 <= sum(not after_login for _, after_login in grants) <= 3
        after_login_grants = sum(after_login for _, after_login in grants)
        if fault == "slow_fence":
            assert after_login_grants == 3
        elif fault == "slow_login":
            assert 1 <= after_login_grants <= 3
        else:
            assert after_login_grants == 1
        assert {p.pid for p in multiprocessing.active_children()} == before
        with acq.FullMarketState(path).connection() as db:
            rows = db.execute("SELECT actual_start,status FROM acquisition_intent").fetchall()
            assert len(rows) == 1 and rows[0][0] is not None and rows[0][1] == "finalized"
            assert [row[0] for row in db.execute("SELECT requests FROM full_quota")] == [1, 1]
        with journal.connection() as db:
            assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == 1
    finally:
        queue._current.reset(token)
        owner.stop()


def test_supervisor_expired_ipc_grant_never_dispatches_or_retries(tmp_path):
    from findb_fetcher.provider_process import ProviderProcessError, supervised_call

    called = tmp_path / "native"
    grants = []

    def expired():
        grants.append(True)
        return time.monotonic() - 1

    with pytest.raises(ProviderProcessError):
        supervised_call(lambda: called.touch(), checkpoint=expired, timeout=1)
    assert grants == [True] and not called.exists()


def test_negative_snapshot_survives_restart_and_valid_cache_replays_without_sdk(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    frame = [Frame(DAY - timedelta(days=1))]
    calls = []
    sdk = SimpleNamespace(
        login=lambda _: calls.append("login"),
        data=SimpleNamespace(get=lambda _: (calls.append("get"), frame[0])[1]),
    )
    journal_path = tmp_path / "work.sqlite3"
    prepared = b'{"original":"prepared-bytes"}'
    journal = queue.WorkJournal(journal_path, "original")
    journal.prepare("previous", prepared)
    journal.reserve_attempt("dataset", 3)

    def fetch(journal):
        owner = queue.ProviderWorkSession(
            SimpleNamespace(request=lambda *args: {}),
            "finlab",
            {"work_id": "original", "generation": 1, "token": "fence"},
            journal,
        )
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
                return finlab.FinLabSdkGateway("offline", _sdk=sdk).fetch_dataset(
                    "price:收盤價", target_date=DAY, symbols=["2330"]
                )
        finally:
            queue._current.reset(token)
            owner.stop()

    with pytest.raises(finlab.FinLabTargetNotReadyError):
        fetch(journal)
    with journal.connection() as db:
        rejected = db.execute("SELECT body,sha256,origin FROM work_response_observation").fetchall()
        assert db.execute("SELECT COUNT(*) FROM work_response").fetchone()[0] == 0
    assert len(rejected) == 1 and rejected[0][2] == "native"
    raw, digest, _ = rejected[0]
    assert hashlib.sha256(raw).hexdigest() == digest
    snapshot = json.loads(raw)["data"]
    assert snapshot == {"dates": ["2026-10-14"], "symbols": ["2330"], "values": [[42]]}
    assert b"must-not-be-saved" not in raw
    frame[0] = Frame()
    reopened = queue.WorkJournal(journal_path, "original")
    assert fetch(reopened).dates == (DAY.isoformat(),)
    assert fetch(queue.WorkJournal(journal_path, "original")).values == ((42,),)
    assert calls == ["login", "get", "login", "get"]
    with reopened.connection() as db:
        assert (
            db.execute("SELECT body,sha256,origin FROM work_response_observation").fetchall()
            == rejected
        )
        assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == 1
        assert db.execute("SELECT body FROM work_delivery").fetchone()[0] == prepared


def test_negative_snapshot_never_stringifies_arbitrary_objects():
    class Secret:
        def __str__(self):
            pytest.fail("SDK object must not be stringified")

    with pytest.raises(finlab.FinLabSdkError, match="unsupported scalar"):
        finlab._snapshot_scalar(Secret())


def test_predispatch_quota_wait_retains_typed_zero_dispatch_result(tmp_path, monkeypatch):
    from findb_fetcher.full_market_state import QuotaBlockedError

    calls = []

    def exhausted(operation):
        raise QuotaBlockedError("durable account quota is exhausted", quota_wait=True)

    monkeypatch.setattr(acq.Acquisition, "prepare", exhausted)
    sdk = SimpleNamespace(
        login=lambda _: calls.append("login"),
        data=SimpleNamespace(get=lambda _: calls.append("get")),
    )
    with pytest.raises(QuotaBlockedError) as failure:
        finlab.FinLabSdkGateway("offline", _sdk=sdk).fetch_dataset(
            "price:收盤價", target_date=DAY, symbols=["2330"]
        )
    assert failure.value.quota_wait and not failure.value.dispatched
    assert calls == []
