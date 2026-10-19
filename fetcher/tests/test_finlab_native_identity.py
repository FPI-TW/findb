"""Offline FinLab 1.5.7 token equivalence, debt, locks and environment snapshots."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from findb_fetcher.acquisition import Acquisition, Authorization, authorization_scope
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.platform_limits import account_identity
from findb_fetcher.providers.finlab import FinLabSdkError, FinLabSdkGateway

TARGET = date(2026, 10, 12)
TOKEN = "a" * 64


def _frame():
    return SimpleNamespace(index=(TARGET,), columns=("2330",), loc={(TARGET, "2330"): 100})


def _grant():
    now = time.time()
    return Authorization(time.monotonic(), now, now, provider="finlab")


@pytest.fixture
def native():
    # An optional audited 1.5.7 __init__.py runs the same tests with the actual
    # wheel's pure-Python login/get_token. Ordinary CI uses its offline semantics.
    source = os.getenv("FINDB_TEST_FINLAB_NATIVE_SOURCE")
    if source:
        spec = importlib.util.spec_from_file_location("offline_native_finlab", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.__version__ == "1.5.7"
        return module

    def login(token):
        os.environ["FINLAB_API_TOKEN"] = token[:64]
        os.environ["finlab_id_token"] = token[:64]

    return SimpleNamespace(login=login, get_token=lambda: os.environ["FINLAB_API_TOKEN"][:64])


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    monkeypatch.setenv("FINLAB_API_TOKEN", TOKEN)
    monkeypatch.setenv("finlab_id_token", "installed-id")
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(tmp_path / "missing.json"))
    return FullMarketState(path)


def _invoke(gateway, grant=_grant):
    with authorization_scope(grant):
        return gateway.fetch_dataset("price:收盤價", target_date=TARGET, symbols=("2330",))


@pytest.mark.parametrize(
    "raw", [TOKEN, " " + TOKEN + " ", TOKEN + "suffix", " " + TOKEN + "suffix "]
)
def test_equivalent_native_token_respects_known_pin_allocation_and_300s_cooldown(
    state, monkeypatch, native, raw
):
    allocation = state.path.with_suffix(".account.json")
    allocation.write_text(json.dumps({"provider": "finlab", "account": {"account_id": "known-A"}}))
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(allocation))
    assert account_identity("finlab", state, credential=raw) == "known-A"
    until = time.time() + 300
    state.rate_limited(account="known-A", window=TARGET.isoformat(), until=until)
    calls = []
    sdk = SimpleNamespace(
        login=native.login,
        data=SimpleNamespace(get=lambda _: calls.append(native.get_token()) or _frame()),
    )
    gateway = FinLabSdkGateway(raw, _sdk=sdk)
    assert gateway.api_token == TOKEN

    def blocked_wait(_):
        raise RuntimeError("offline cooldown wait intercepted")

    monkeypatch.setattr("findb_fetcher.acquisition.time.sleep", blocked_wait)
    with pytest.raises(FinLabSdkError):
        _invoke(gateway)
    assert calls == []
    with state.connection() as db:
        assert (
            db.execute("SELECT next_at FROM full_pacing WHERE account='known-A'").fetchone()[0]
            >= until
        )
        assert db.execute("SELECT COUNT(*) FROM acquisition_intent").fetchone()[0] == 0
    # Finish the offline proof without sleeping 300 seconds.
    with state.connection() as db:
        db.execute("UPDATE full_pacing SET next_at=0")
        db.execute("UPDATE full_quota SET blocked_until=0")
    _invoke(gateway)
    assert calls == [TOKEN]
    assert os.environ["FINLAB_API_TOKEN"] == TOKEN
    assert os.environ["finlab_id_token"] == "installed-id"
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT account FROM acquisition_intent WHERE actual_start IS NOT NULL"
            ).fetchone()[0]
            == "known-A"
        )


def test_proven_raw_old_normalized_native_and_installed_pins_conserve_debt_max(state, monkeypatch):
    raw = " " + TOKEN + "explicit-suffix "
    installed = " " + TOKEN + "installed-suffix "
    monkeypatch.setenv("FINLAB_API_TOKEN", installed)
    keys = [
        (TOKEN, "native", 3, 40, 120),
        (raw, "raw%_", 4, 50, 300),
        (raw.strip(), "old-normalized", 5, 60, 200),
        (installed, "installed-raw", 6, 70, 150),
        (installed.strip(), "installed-normalized", 7, 80, 250),
        ("b" * 64, "unrelated", 99, 999, 999),
    ]
    now = time.time()
    with state.connection() as db:
        for key, account, requests, bytes_, delay in keys:
            db.execute(
                "INSERT INTO full_account_identity VALUES('finlab',?,?)",
                (hashlib.sha256(key.encode()).hexdigest(), account),
            )
            for name in (account, account + ":allocation:pilot"):
                db.execute(
                    "INSERT INTO full_quota VALUES(?,?,?,?,?)",
                    (name, TARGET.isoformat(), requests, bytes_, now + delay),
                )
            db.execute("INSERT INTO full_pacing VALUES(?,?)", (account, now + delay))
    assert account_identity("finlab", state, credential=raw) == "native"
    assert account_identity("finlab", state, credential=raw) == "native"
    with state.connection() as db:
        for account in ("native", "native:allocation:pilot"):
            assert tuple(
                db.execute(
                    "SELECT requests,bytes,blocked_until FROM full_quota WHERE account=?",
                    (account,),
                ).fetchone()
            ) == (25, 300, now + 300)
        assert (
            db.execute("SELECT next_at FROM full_pacing WHERE account='native'").fetchone()[0]
            == now + 300
        )
        assert db.execute("SELECT COUNT(*) FROM account_alias_audit").fetchone()[0] == 4
        assert (
            db.execute(
                "SELECT account FROM full_account_identity WHERE credential_sha256=?",
                (hashlib.sha256(("b" * 64).encode()).hexdigest(),),
            ).fetchone()[0]
            == "unrelated"
        )
        assert tuple(
            db.execute(
                "SELECT requests,bytes FROM full_quota WHERE account='unrelated:allocation:pilot'"
            ).fetchone()
        ) == (99, 999)


def test_different_actual_token_cannot_take_installed_allocation(state, monkeypatch):
    allocation = state.path.with_suffix(".account.json")
    allocation.write_text(
        json.dumps({"provider": "finlab", "account": {"account_id": "installed-A"}})
    )
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(allocation))
    assert account_identity("finlab", state) == "installed-A"
    assert account_identity("finlab", state, credential="b" * 64 + "suffix") != "installed-A"


def _process_acquisition(path, token, active, overlap, release, entered):
    with Acquisition("finlab", credential=token, state_path=Path(path)):
        with active.get_lock():
            if active.value:
                overlap.value = 1
            active.value += 1
        entered.set()
        assert release.wait(10)
        with active.get_lock():
            active.value -= 1


def test_native_equivalent_keys_share_cross_process_lock(state):
    ctx = multiprocessing.get_context("spawn")
    active, overlap = ctx.Value("i", 0), ctx.Value("i", 0)
    release, first, second = ctx.Event(), ctx.Event(), ctx.Event()
    p1 = ctx.Process(
        target=_process_acquisition, args=(str(state.path), TOKEN, active, overlap, release, first)
    )
    p2 = ctx.Process(
        target=_process_acquisition,
        args=(str(state.path), " " + TOKEN + "suffix ", active, overlap, release, second),
    )
    p1.start()
    assert first.wait(10)
    p2.start()
    assert not second.wait(0.3)
    release.set()
    for process in (p1, p2):
        process.join(10)
        assert process.exitcode == 0
    assert second.is_set()
    assert overlap.value == 0


@pytest.mark.parametrize("phase", ["login", "authorization", "data", "finalization"])
@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("explicit", [TOKEN + "suffix", "b" * 64])
def test_factory_and_identity_snapshot_each_sdk_phase_and_failure(
    state, monkeypatch, native, phase, fail, explicit
):
    from findb_fetcher import acquisition

    entered, release, constructing = threading.Event(), threading.Event(), threading.Event()
    snapshots = []
    original_finalize = Acquisition.finalize
    quarantine_size = len(acquisition._quarantined)
    installed_account = account_identity("finlab", state)
    actual_account = account_identity("finlab", state, credential=explicit)

    def checkpoint(current):
        if current != phase:
            return
        snapshots.append(FinLabSdkGateway.from_env().api_token)
        assert account_identity("finlab", state) == installed_account
        assert account_identity("finlab", state, credential=explicit) == actual_account
        entered.set()
        assert release.wait(5)
        if fail:
            raise RuntimeError("offline phase failure")

    def login(token):
        native.login(token)
        checkpoint("login")

    def grant():
        checkpoint("authorization")
        return _grant()

    def get(_):
        assert native.get_token() == explicit[:64]
        checkpoint("data")
        return _frame()

    def finalize(operation):
        original_finalize(operation)
        checkpoint("finalization")

    monkeypatch.setattr(Acquisition, "finalize", finalize)
    sdk = SimpleNamespace(login=login, data=SimpleNamespace(get=get))

    def construct():
        constructing.set()
        gateway = FinLabSdkGateway.from_env()
        assert account_identity("finlab", state) == installed_account
        return gateway

    def dispatch_in_worker():
        try:
            return _invoke(FinLabSdkGateway(explicit, _sdk=sdk), grant)
        finally:
            # Only the dispatch thread can release its thread RLock. The real
            # finalization succeeded before this test injected the failure.
            while len(acquisition._quarantined) > quarantine_size:
                acquisition._quarantined.pop().__exit__(None, None, None)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            dispatch = pool.submit(dispatch_in_worker)
            assert entered.wait(5)
            factory = pool.submit(construct)
            assert constructing.wait(5)
            assert not factory.done()
            release.set()
            if fail:
                with pytest.raises(FinLabSdkError):
                    dispatch.result(5)
            else:
                dispatch.result(5)
            gateway = factory.result(5)
        # Successful login and get each obtain a fresh authorization while the
        # same credential scope remains pinned; a denied first grant stops it.
        assert snapshots == [TOKEN] * (2 if phase == "authorization" and not fail else 1)
        assert gateway.api_token == TOKEN
        assert os.environ["FINLAB_API_TOKEN"] == TOKEN
        assert os.environ["finlab_id_token"] == "installed-id"
    finally:
        release.set()


def test_nested_identity_write_fails_closed_then_merges_debt_after_release(state):
    from findb_fetcher.full_market_state import QuotaBlockedError

    raw = " " + TOKEN + "new-suffix "
    with state.connection() as db:
        for key, alias, debt in [(raw, "raw", 4), (raw.strip(), "old-normalized", 5)]:
            db.execute(
                "INSERT INTO full_account_identity VALUES('finlab',?,?)",
                (hashlib.sha256(key.encode()).hexdigest(), alias),
            )
            db.execute(
                "INSERT INTO full_quota VALUES(?,?,?,0,0)", (alias, TARGET.isoformat(), debt)
            )
    with Acquisition("finlab", credential=TOKEN, state_path=state.path) as operation:
        assert account_identity("finlab", state) == operation.account
        with pytest.raises(QuotaBlockedError, match="cannot be updated"):
            account_identity("finlab", state, credential=raw)
        with state.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM account_alias_audit").fetchone()[0] == 0
            assert db.execute("SELECT SUM(requests) FROM full_quota").fetchone()[0] == 9
    assert account_identity("finlab", state, credential=raw) == operation.account
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT requests FROM full_quota WHERE account=?", (operation.account,)
            ).fetchone()[0]
            == 9
        )


def _waiting_merge(path, raw, attempting, done):
    state = FullMarketState(Path(path))
    attempting.set()
    account_identity("finlab", state, credential=raw)
    done.set()


def test_pinned_callback_read_avoids_identity_mutex_waiting_on_active_account(state):
    raw = TOKEN + "suffix"
    with state.connection() as db:
        db.execute(
            "INSERT INTO full_account_identity VALUES('finlab',?,'legacy')",
            (hashlib.sha256(raw.encode()).hexdigest(),),
        )
        db.execute("INSERT INTO full_quota VALUES('legacy',?,7,0,0)", (TARGET.isoformat(),))
    ctx = multiprocessing.get_context("spawn")
    attempting, done = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_waiting_merge, args=(str(state.path), raw, attempting, done))
    with Acquisition("finlab", credential=TOKEN, state_path=state.path) as operation:
        process.start()
        assert attempting.wait(10)
        assert not done.wait(0.3)
        # The child owns/waits at identity -> account; this read must not wait
        # at account -> identity. A new alias still fails closed, without writes.
        assert account_identity("finlab", state) == operation.account
    process.join(10)
    assert process.exitcode == 0
    assert done.is_set()
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT requests FROM full_quota WHERE account=?", (operation.account,)
            ).fetchone()[0]
            == 7
        )


def test_snapshot_under_external_account_lock_cannot_invert_sdk_lock_order(
    state, monkeypatch, native
):
    from findb_fetcher import finlab_credentials
    from findb_fetcher.full_market_state import QuotaBlockedError

    sdk_held = threading.Event()
    original_identity = account_identity

    def identity(provider, current_state, **kwargs):
        if threading.current_thread() is not threading.main_thread():
            # Gateway holds SDK scope before Acquisition identity construction.
            sdk_held.set()
        return original_identity(provider, current_state, **kwargs)

    monkeypatch.setattr("findb_fetcher.acquisition.account_identity", identity)
    sdk = SimpleNamespace(login=native.login, data=SimpleNamespace(get=lambda _: _frame()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Acquisition("finlab", credential=TOKEN, state_path=state.path):
            with pytest.raises(FinLabSdkError):
                _invoke(FinLabSdkGateway(TOKEN, _sdk=sdk))
            dispatch = pool.submit(_invoke, FinLabSdkGateway(TOKEN, _sdk=sdk))
            assert sdk_held.wait(5)
            with pytest.raises(QuotaBlockedError, match="snapshot is busy"):
                FinLabSdkGateway.from_env()
            assert finlab_credentials._SDK_LOCK.locked()
        dispatch.result(5)
    assert FinLabSdkGateway.from_env().api_token == TOKEN
