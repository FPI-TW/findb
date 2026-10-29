"""Protected actual SDK bootstrap/cleanup using the production child loop."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from findb_fetcher.acquisition import Acquisition, Authorization, authorization_scope
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.platform_limits import account_identity
from findb_fetcher.providers.shioaji import (
    IsolatedShioajiGateway,
    ShioajiSdkError,
    ShioajiSdkGateway,
)
from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
from provider_work_support import account_boundary_unit
from sdk_bootstrap_harness import SDK_CHILD, install_launcher


def grant():
    now = time.time()
    return Authorization(time.monotonic(), now, now, provider="shioaji")


def deny():
    raise QuotaBlockedError("cancelled current Source scope")


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    path = tmp_path / "governor.sqlite3"
    output = tmp_path / "sdk.jsonl"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    monkeypatch.setenv("SHIOAJI_API_KEY", "environment-A")
    install_launcher(monkeypatch, output)
    return path, output


@pytest.mark.parametrize(
    "gateway_type", [PersistentIsolatedShioajiGateway, IsolatedShioajiGateway, ShioajiSdkGateway]
)
def test_denied_explicit_no_provider_environment_starts_no_sdk(sdk, monkeypatch, gateway_type):
    path, output = sdk
    monkeypatch.delenv("SHIOAJI_API_KEY")
    with authorization_scope(deny), pytest.raises(ShioajiSdkError):
        gateway = gateway_type("actual-B", "private-key")
        try:
            gateway.fetch_kbars("2330", date(2026, 1, 1))
        finally:
            if isinstance(gateway, PersistentIsolatedShioajiGateway):
                gateway.close()
    assert not any(
        row["event"] in {"create", "login", "contracts", "kbars", "logout"}
        for row in events(output)
    )
    with FullMarketState(path).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT SUM(login_units) FROM acquisition_intent WHERE actual_start IS NOT NULL"
            ).fetchone()[0]
            is None
        )


def test_explicit_b_ignores_a_allocation_cooldown_and_caches_one_login(sdk, monkeypatch):
    path, output = sdk
    state = FullMarketState(path)
    account_a = account_identity("shioaji", state)
    with state.connection() as db:
        db.execute("INSERT INTO full_pacing VALUES(?,?)", (account_a, time.time() + 300))
    allocation = path.parent / "allocation.json"
    allocation.write_text(json.dumps({"provider": "shioaji", "account": {"account_id": account_a}}))
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(allocation))
    gateway = PersistentIsolatedShioajiGateway(" actual-B ", "private-key")
    with authorization_scope(grant):
        assert len(gateway.catalogue()) == 2
        assert len(gateway.catalogue()) == 2
        gateway.fetch_kbars("2330", date(2026, 1, 1))
    with authorization_scope(deny):
        gateway.close()
    rows = events(output)
    assert [r["event"] for r in rows].count("login") == 1
    assert [r["event"] for r in rows].count("contracts") == 2
    assert [r["event"] for r in rows].count("logout") == 1
    assert (
        next(r for r in rows if r["event"] == "login")["credential_sha256"]
        == hashlib.sha256(b"actual-B").hexdigest()
    )
    assert gateway._account != account_a
    with state.connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM full_quota WHERE account=?", (account_a,)).fetchone()[
                0
            ]
            == 0
        )
        assert (
            db.execute(
                "SELECT SUM(login_units) FROM acquisition_intent WHERE actual_start IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        assert tuple(
            db.execute(
                "SELECT request_units,login_units,status FROM acquisition_intent WHERE operation_kind='cleanup'"
            ).fetchone()
        ) == (0, 0, "finalized")


@pytest.mark.parametrize(
    "mode", ["unavailable", "bytes-full", "connections-zero", "connections-invalid"]
)
def test_invalid_postlogin_usage_blocks_contracts_and_data_and_cleans_up(sdk, monkeypatch, mode):
    path, output = sdk
    install_launcher(monkeypatch, output, mode)
    gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
    with authorization_scope(grant), pytest.raises(ShioajiSdkError):
        gateway.catalogue()
    gateway.close()
    rows = events(output)
    assert [r["event"] for r in rows].count("login") == 1
    assert not any(r["event"] in {"contracts", "kbars"} for r in rows)
    assert any(r["event"] == "logout" for r in rows)
    with FullMarketState(path).connection() as db:
        assert (
            db.execute(
                "SELECT status FROM acquisition_intent WHERE operation_kind='sdk_login'"
            ).fetchone()[0]
            == "finalized"
        )
        assert (
            db.execute(
                "SELECT usage_before_state FROM acquisition_intent WHERE operation_kind='sdk_login'"
            ).fetchone()[0]
            == "not_authenticated"
        )


def test_known_full_connections_reuses_session_but_blocks_new_login(sdk, monkeypatch):
    path, output = sdk
    install_launcher(monkeypatch, output, "connections-full")
    first = PersistentIsolatedShioajiGateway("actual-B", "private-key")
    with authorization_scope(grant):
        first.catalogue()
        first.fetch_kbars("2330", date(2026, 1, 1))
        second = PersistentIsolatedShioajiGateway("actual-B", "private-key")
        with pytest.raises(ShioajiSdkError):
            second.catalogue()
        second.close()
    first.close()
    assert [r["event"] for r in events(output)].count("login") == 1


@pytest.mark.parametrize("mode,outcome", [("normal", "success"), ("kbars-fail", "error")])
def test_actual_kbars_outcome_is_durable_without_refunding_request(sdk, monkeypatch, mode, outcome):
    path, output = sdk
    install_launcher(monkeypatch, output, mode)
    gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
    try:
        with authorization_scope(grant):
            if mode == "kbars-fail":
                with pytest.raises(ShioajiSdkError) as raised:
                    gateway.fetch_kbars("2330", date(2026, 1, 1))
                assert "private-provider-marker" not in str(raised.value)
            else:
                gateway.fetch_kbars("2330", date(2026, 1, 1))
        # Observe the child's first durable finalization before closing the parent.
        with FullMarketState(path).connection() as db:
            rows = db.execute(
                "SELECT status,outcome,request_units,login_units,actual_start "
                "FROM acquisition_intent WHERE operation_kind='kbars'"
            ).fetchall()
            assert len(rows) == 1
            assert tuple(rows[0])[:4] == ("finalized", outcome, 1, 0)
            assert rows[0]["actual_start"] is not None
    finally:
        with authorization_scope(deny):
            gateway.close()
    assert [row["event"] for row in events(output)].count("kbars") == 1
    assert [row["event"] for row in events(output)].count("logout") == 1
    with FullMarketState(path).connection() as db:
        assert tuple(
            db.execute(
                "SELECT status,outcome,request_units,login_units FROM acquisition_intent "
                "WHERE operation_kind='cleanup'"
            ).fetchone()
        ) == ("finalized", "success", 0, 0)
        assert (
            db.execute(
                "SELECT outcome FROM acquisition_intent WHERE operation_kind='kbars'"
            ).fetchone()[0]
            == outcome
        )


def test_second_real_child_cannot_construct_while_b_account_locked(sdk):
    path, output = sdk

    @account_boundary_unit
    def run(path):
        gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
        with authorization_scope(grant):
            gateway.catalogue()
        gateway.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Acquisition("shioaji", credential="actual-B"):
            future = pool.submit(run, path)
            deadline = time.monotonic() + 5
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert output.exists()
            assert not any(r["event"] in {"create", "login", "contracts"} for r in events(output))
        future.result(timeout=10)


@account_boundary_unit
def _bootstrap_parent(path, output, mode):
    import subprocess
    import sys

    from findb_fetcher.providers import shioaji_session as ss

    original = subprocess.Popen

    def launch(args, **kwargs):
        return original([sys.executable, "-c", SDK_CHILD, args[-1], output, mode], **kwargs)

    ss.subprocess.Popen = launch
    os.environ["FETCHER_ACCOUNT_STATE_PATH"] = path
    os.environ["SHIOAJI_API_KEY"] = "environment-A"
    os.environ["SOURCE_CLIENT_KEY"] = "must-not-enter-sdk"
    gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
    with authorization_scope(grant):
        gateway.catalogue()
    gateway.close()


@pytest.mark.parametrize(
    "kind,event", [("sdk_create", "create"), ("sdk_login", "login"), ("sdk_contracts", "contracts")]
)
def test_parent_kill_during_actual_bootstrap_keeps_child_fd_and_journal(sdk, kind, event):
    path, output = sdk
    mode = {
        "sdk_create": "create-slow",
        "sdk_login": "login-slow",
        "sdk_contracts": "contracts-slow",
    }[kind]
    ctx = multiprocessing.get_context("spawn")
    parent = ctx.Process(target=_bootstrap_parent, args=(str(path), str(output), mode))
    parent.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = [r for r in events(output) if r["event"] == event]
        if found:
            break
        time.sleep(0.01)
    else:
        pytest.fail("bootstrap did not start")
    began = found[0]["at"]
    parent.kill()
    parent.join(10)
    with Acquisition("shioaji", credential="actual-B"):
        assert time.time() - began >= 0.35
    with FullMarketState(path).connection() as db:
        assert (
            db.execute(
                "SELECT status FROM acquisition_intent WHERE operation_kind=?", (kind,)
            ).fetchone()[0]
            == "finalized"
        )
    assert events(output)[0]["environment_present"] == []


def test_real_login_timeout_finalizes_then_logs_out_before_unlock(sdk, monkeypatch):
    path, output = sdk
    install_launcher(monkeypatch, output, "login-slow")
    gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
    with authorization_scope(grant):
        gateway._run("sdk_create")
        gateway._constructed = True
    gateway.timeout_seconds = 0.15
    with authorization_scope(grant), pytest.raises(ShioajiSdkError):
        gateway.catalogue()
    assert not gateway.process.is_alive()
    assert any(r["event"] == "logout" for r in events(output))
    with FullMarketState(path).connection() as db:
        assert (
            db.execute(
                "SELECT status FROM acquisition_intent WHERE operation_kind='sdk_login'"
            ).fetchone()[0]
            == "finalized"
        )
        assert (
            db.execute(
                "SELECT status FROM acquisition_intent WHERE operation_kind='cleanup'"
            ).fetchone()[0]
            == "finalized"
        )


def seed_logins(state, account, count, stamp, status="finalized"):
    with state.connection() as db:
        db.executemany(
            "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,actual_start,completed_at,operation_kind,request_units,login_units) VALUES(?,?,'shioaji',?,?,300,?,?,'sdk_login',1,1)",
            [
                (
                    f"login-{i}",
                    account,
                    stamp,
                    status,
                    stamp if status == "finalized" else None,
                    stamp,
                )
                for i in range(count)
            ],
        )


def test_login_999_1000_failure_and_exact_rolling_expiry(sdk, monkeypatch):
    path, _ = sdk
    now = 200000.0
    monkeypatch.setattr(time, "time", lambda: now)
    operation = Acquisition("shioaji", credential="actual-B", kind="sdk_login")
    seed_logins(operation.state, operation.account, 999, now - 100)
    with authorization_scope(grant), operation:
        operation.start()
        # Simulated concrete SDK login failure remains a known call.
    now += 0.201
    with Acquisition("shioaji", credential="actual-B", kind="sdk_login") as blocked:
        with pytest.raises(QuotaBlockedError, match="rolling"):
            blocked.prepare()
    now = 286400.0
    with (
        authorization_scope(grant),
        Acquisition("shioaji", credential="actual-B", kind="sdk_login") as allowed,
    ):
        allowed.start()
    with operation.state.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE actual_start IS NOT NULL AND login_units=1"
            ).fetchone()[0]
            == 1001
        )


def test_unknown_login_restart_and_alias_retains_rolling_liability(sdk):
    path, _ = sdk
    state = FullMarketState(path)
    raw = " actual-B "
    with state.connection() as db:
        for credential, account in [(raw, "legacy_raw"), (raw.strip(), "normalized")]:
            db.execute(
                "INSERT INTO full_account_identity VALUES('shioaji',?,?)",
                (hashlib.sha256(credential.encode()).hexdigest(), account),
            )
    seed_logins(state, "normalized", 999, time.time() - 1)
    with state.connection() as db:
        db.execute(
            "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,operation_kind,login_units) VALUES('crashed','legacy_raw','shioaji',?,'armed',300,'sdk_login',1)",
            (time.time() - 1,),
        )
    with Acquisition("shioaji", credential=raw, kind="sdk_login") as operation:
        with state.connection() as db:
            assert (
                db.execute(
                    "SELECT status,account FROM acquisition_intent WHERE operation_id='crashed'"
                ).fetchone()[0]
                == "unknown"
            )
            with pytest.raises(QuotaBlockedError, match="rolling"):
                operation._check_login(db, time.time())
    assert account_identity("shioaji", state, credential=raw) == "normalized"
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE operation_id='crashed'"
            ).fetchone()[0]
            == 1
        )


def test_actual_failed_login_is_charged_once_and_logout_is_zero_unit(sdk, monkeypatch):
    path, output = sdk
    install_launcher(monkeypatch, output, "login-fail")
    gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
    with authorization_scope(grant), pytest.raises(ShioajiSdkError):
        gateway.catalogue()
    rows = events(output)
    assert [r["event"] for r in rows].count("login") == 1
    assert any(r["event"] == "logout" for r in rows)
    with FullMarketState(path).connection() as db:
        assert tuple(
            db.execute(
                "SELECT status,login_units,outcome FROM acquisition_intent WHERE operation_kind='sdk_login'"
            ).fetchone()
        ) == ("finalized", 1, "error")
        assert tuple(
            db.execute(
                "SELECT request_units,login_units FROM acquisition_intent WHERE operation_kind='cleanup'"
            ).fetchone()
        ) == (0, 0)


def test_source_cancellation_after_create_prevents_login_and_known_login_debt(sdk):
    path, output = sdk
    calls = []

    def current():
        calls.append(True)
        return grant() if len(calls) == 1 else deny()

    gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
    with authorization_scope(current), pytest.raises(ShioajiSdkError):
        gateway.catalogue()
    assert any(r["event"] == "create" for r in events(output))
    assert not any(r["event"] in {"login", "contracts", "kbars", "logout"} for r in events(output))
    with FullMarketState(path).connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE login_units=1 AND actual_start IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "gateway_type", [PersistentIsolatedShioajiGateway, IsolatedShioajiGateway, ShioajiSdkGateway]
)
def test_explicit_no_provider_environment_success_binds_raw_normalized_pins(
    sdk, monkeypatch, gateway_type
):
    path, output = sdk
    monkeypatch.delenv("SHIOAJI_API_KEY")
    state = FullMarketState(path)
    with state.connection() as db:
        for key, account in [(" actual-B ", "known_raw"), ("actual-B", "normalized")]:
            db.execute(
                "INSERT INTO full_account_identity VALUES('shioaji',?,?)",
                (hashlib.sha256(key.encode()).hexdigest(), account),
            )
    gateway = gateway_type(" actual-B ", " private-key ")
    with authorization_scope(grant):
        snapshot = gateway.fetch_kbars("2330", date(2026, 1, 1))
    if isinstance(gateway, PersistentIsolatedShioajiGateway):
        gateway.close()
    assert snapshot.usage_bytes_before == 10
    assert [r["event"] for r in events(output)].count("login") == 1
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT account FROM acquisition_intent WHERE operation_kind='sdk_login'"
            ).fetchone()[0]
            == "normalized"
        )
        assert db.execute("SELECT COUNT(*) FROM account_alias_audit").fetchone()[0] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"dataset_keys": []},
        {"dataset_keys": ["tw_equity_minute"]},
        {"provider": "finlab"},
        {"scheduler_key": "other"},
    ],
)
def test_mismatched_current_scheduler_scope_prevents_actual_sdk_and_debt(sdk, change):
    from datetime import datetime, timezone

    import httpx

    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.full_market_client import FullMarketSourceClient

    path, output = sdk
    expected = {
        "scheduler_key": "shioaji_tw_pilot_v1",
        "provider": "shioaji",
        "dataset_keys": ["tw_equity_minute", "tw_etf_minute"],
    }

    def response(_):
        stamp = datetime.now(timezone.utc).isoformat()
        return httpx.Response(
            200,
            headers={"X-FinDB-Acquisition-Checked-At": stamp},
            json={
                **expected,
                **change,
                "acquisition_allowed": True,
                "max_age_seconds": 5,
                "checked_at": stamp,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        source = FullMarketSourceClient(
            FetcherConfig("https://source.test", "fixture-key"), client=http
        )
        gateway = PersistentIsolatedShioajiGateway("actual-B", "private-key")
        with (
            authorization_scope(
                lambda: source.authorization(
                    "GET", "scheduler-controls/shioaji_tw_pilot_v1/authorization", expected=expected
                )
            ),
            pytest.raises(ShioajiSdkError),
        ):
            gateway.catalogue()
        gateway.close()
    assert not any(
        row["event"] in {"create", "login", "contracts", "kbars"} for row in events(output)
    )
    with FullMarketState(path).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_quota").fetchone()[0] == 0
