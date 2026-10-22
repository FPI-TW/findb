"""Real SDK child checkpoints and immutable negative table observations."""

import hashlib
import json
import os
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from findb_fetcher import acquisition as acq
from findb_fetcher import provider_work as queue
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.providers import finlab
from provider_work_support import work_authorization
from provider_worker_support import child_deadline, fake_finlab, install_fake_sdk

DAY = date(2026, 10, 15)


class Frame:
    def __init__(self, day=DAY, value=42):
        self.index = (day,)
        self.columns = ("2330",)
        self.loc = {(day, "2330"): value}
        self.attrs = {"secret": "must-not-be-saved"}


@pytest.mark.parametrize("catalogue", [False, True])
@pytest.mark.parametrize(
    "fault", [None, "stop", "heartbeat", "generation", "slow_fence", "slow_grant"]
)
def test_real_worker_dispatches_get_after_one_fresh_grant(tmp_path, monkeypatch, catalogue, fault):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    logged_in, events = tmp_path / "logged-in", tmp_path / "events"
    grants = []
    # Pinned 1.5.7 login is local: one READY covers login and get. The fake
    # clock drives the parent; the worker only sees its verdict.
    elapsed = [0.0]
    epoch = time.time()

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

    class Source:
        def request(self, method, route, body):
            if route.endswith("/authorize"):
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
            return work_authorization("finlab", body) if route.endswith("/authorize") else {}

    record = f"open({str(events)!r}, 'a').write('{{event}}:' + str(os.getpid()) + '\\n')\n"
    install_fake_sdk(
        monkeypatch,
        tmp_path / "sdk",
        fake_finlab(
            record.replace("{event}", "get")
            + f"import datetime\nfrom provider_worker_support import Frame\nreturn Frame({DAY!r})",
            login=f"open({str(logged_in)!r}, 'w').close()",
            check_version=record.replace("{event}", "pypi"),
        ),
    )
    native = SimpleNamespace(login=lambda token: None, data=SimpleNamespace(get=lambda name: None))
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
        grants.append(logged_in.exists())
        authorization = acq.Authorization(
            clock.monotonic(), clock.time(), clock.time(), provider="finlab"
        )
        if fault == "slow_grant":
            advance(0.06)
        return authorization

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
            if fault in {None, "slow_fence"}:
                assert fetch().symbols == ("2330",)
            else:
                error = (
                    queue.ProviderWorkAcquisitionStopped
                    if fault == "stop"
                    else finlab.FinLabOperationalError
                    if fault == "slow_grant"
                    else queue.ProviderWorkRecoveryBlockedError
                )
                with pytest.raises(error):
                    fetch()
        observed = events.read_text().splitlines() if events.exists() else []
        assert all(int(row.split(":")[1]) != os.getpid() for row in observed)
        if fault in {None, "slow_fence"}:
            assert logged_in.exists()
            assert [row.split(":")[0] for row in observed] == ["get"]
            assert grants == [False]
        else:
            assert not logged_in.exists() and observed == []
            assert grants == ([False] * 3 if fault == "slow_grant" else [])
    finally:
        queue._current.reset(token)
        owner.stop()


def test_supervisor_expired_ipc_grant_never_dispatches_or_retries(tmp_path, monkeypatch):
    from findb_fetcher.provider_process import ProviderProcessError, supervised_call

    called = tmp_path / "native"
    install_fake_sdk(
        monkeypatch, tmp_path / "sdk", fake_finlab(f"open({str(called)!r}, 'w').close()")
    )
    grants = []

    def expired():
        grants.append(True)
        return time.monotonic() - 1

    with pytest.raises(ProviderProcessError):
        supervised_call(
            "finlab.get",
            {"token": "offline", "dataset": "x", "symbols": ("2330",), "account": False},
            checkpoint=expired,
            timeout=5,
        )
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
            SimpleNamespace(
                request=lambda method, route, body: (
                    work_authorization("finlab", body) if route.endswith("/authorize") else {}
                )
            ),
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
