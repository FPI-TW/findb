"""Concrete provider boundaries preserve current account identity and liability."""

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from findb_fetcher.account_governor import main
from findb_fetcher.acquisition import Acquisition, Authorization, authorization_scope
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.providers.finlab import FinLabSdkError, FinLabSdkGateway
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataResponseError,
)


def _grant():
    now = time.time()
    return Authorization(time.monotonic(), now, now)


@pytest.fixture
def governor(tmp_path, monkeypatch):
    path = tmp_path / "governor.sqlite3"
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(path))
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "environment-A")
    monkeypatch.setenv("FINLAB_API_TOKEN", "environment-finlab")
    return FullMarketState(path)


def _pin(state, provider, key, account):
    with state.connection() as db:
        db.execute(
            "INSERT INTO full_account_identity VALUES(?,?,?)",
            (provider, hashlib.sha256(key.encode()).hexdigest(), account),
        )


def test_twelve_data_client_config_key_uses_known_normalized_and_raw_pins(governor):
    _pin(governor, "twelve_data", "actual-B", "known-B")
    _pin(governor, "twelve_data", " actual-B ", "legacy-raw-B")
    minute = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    with governor.connection() as db:
        for account, used in (("known-B", 1596), ("legacy-raw-B", 1)):
            db.execute(
                "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,?,0)",
                (account, minute, used),
            )
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request))) as http:
        with authorization_scope(_grant), pytest.raises(QuotaBlockedError, match="quota"):
            TwelveDataClient(TwelveDataConfig(api_key=" actual-B "), client=http).fetch_daily(
                "AAPL"
            )
    assert calls == []
    with governor.connection() as db:
        assert (
            db.execute(
                "SELECT requests FROM full_quota WHERE account='known-B' AND window=?", (minute,)
            ).fetchone()[0]
            == 1597
        )
        assert db.execute("SELECT COUNT(*) FROM acquisition_intent").fetchone()[0] == 0


def test_twelve_data_actual_config_key_cooldown_blocks_next_boundary(governor, monkeypatch):
    _pin(governor, "twelve_data", "actual-B", "known-B")
    calls = []

    def upstream(request):
        calls.append(request.url.params["apikey"])
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"status": "error"})

    class CooldownWaitError(Exception):
        pass

    def wait(_):
        raise CooldownWaitError

    with httpx.Client(transport=httpx.MockTransport(upstream)) as http:
        client = TwelveDataClient(TwelveDataConfig(api_key="actual-B"), client=http)
        with authorization_scope(_grant), pytest.raises(TwelveDataResponseError):
            client.fetch_daily("AAPL")
        monkeypatch.setattr("findb_fetcher.acquisition.time.sleep", wait)
        with authorization_scope(_grant), pytest.raises(CooldownWaitError):
            client.fetch_daily("AAPL")
    assert calls == ["actual-B"]
    with governor.connection() as db:
        assert (
            db.execute("SELECT next_at FROM full_pacing WHERE account='known-B'").fetchone()[0]
            > time.time() + 100
        )
        assert [row[0] for row in db.execute("SELECT account FROM acquisition_intent")] == [
            "known-B"
        ]


def test_parallel_twelve_data_clients_same_config_key_share_boundary_lock(governor):
    _pin(governor, "twelve_data", "actual-B", "known-B")
    events = []

    def upstream(request):
        assert request.url.params["apikey"] == "actual-B"
        events.append(("start", time.monotonic()))
        time.sleep(0.04)
        events.append(("complete", time.monotonic()))
        return httpx.Response(200, json={"status": "ok", "values": []})

    def fetch(key):
        with (
            authorization_scope(_grant),
            httpx.Client(transport=httpx.MockTransport(upstream)) as http,
        ):
            TwelveDataClient(TwelveDataConfig(api_key=key), client=http).fetch_daily("AAPL")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(fetch, ["actual-B", " actual-B "]))
    assert [kind for kind, _ in events] == ["start", "complete", "start", "complete"]
    assert events[2][1] - events[1][1] >= 60 / 1597 - 0.002
    with governor.connection() as db:
        assert [row[0] for row in db.execute("SELECT account FROM acquisition_intent")] == [
            "known-B",
            "known-B",
        ]
        assert (
            db.execute(
                "SELECT SUM(requests) FROM full_quota WHERE account='known-B' AND length(window)=10"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    "elapsed,blocked", [(7200, True), (86399.999, True), (86400, False), (86400.001, False)]
)
def test_rollback_inspection_matches_rolling_unknown_login_across_midnight(
    governor, monkeypatch, capsys, elapsed, blocked
):
    unknown = datetime(2026, 10, 12, 23, tzinfo=timezone.utc).timestamp()
    now = unknown + elapsed
    monkeypatch.setattr("time.time", lambda: now)
    with governor.connection() as db:
        db.execute(
            "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,operation_kind,request_units,login_units,completed_at) VALUES('unknown','sdk-account','shioaji',?,'unknown',300,'sdk_login',1,1,?)",
            (unknown, unknown),
        )
        db.executemany(
            "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,operation_kind,request_units,login_units,actual_start,completed_at) VALUES(?,'sdk-account','shioaji',?,'finalized',300,'sdk_login',1,1,?,?)",
            [(f"known-{i}", unknown + 3600, unknown + 3600, unknown + 3600) for i in range(999)],
        )
    monkeypatch.setattr(
        "sys.argv",
        [
            "governor",
            "inspect-acquisition",
            "--governor",
            str(governor.path),
            "--provider",
            "shioaji",
        ],
    )
    main()
    inspection = json.loads(capsys.readouterr().out)
    assert inspection["legacy_rollback_safe"] is (not blocked)
    assert inspection["current_unknown_or_armed"] == int(blocked)
    with (
        authorization_scope(_grant),
        Acquisition("shioaji", account="sdk-account", kind="sdk_login") as operation,
    ):
        if blocked:
            with pytest.raises(QuotaBlockedError, match="rolling 24h"):
                operation.start()
        else:
            operation.start()


def _finlab_sdk(http, logins):
    # Pinned 1.5.7 LoginPanel.login/get_token select/read this process-global
    # environment token; the fetch adapter sends get_token() as api_token.
    def login(token):
        logins.append(token)
        __import__("os").environ["FINLAB_API_TOKEN"] = token[:64]
        __import__("os").environ["finlab_id_token"] = token[:64]

    def get(dataset):
        import os

        http.get(
            "https://finlab.invalid/data", params={"api_token": os.environ["FINLAB_API_TOKEN"][:64]}
        )
        return SimpleNamespace(
            index=(date(2026, 10, 12),), columns=("2330",), loc={(date(2026, 10, 12), "2330"): 100}
        )

    return SimpleNamespace(login=login, data=SimpleNamespace(get=get))


def test_finlab_cross_token_gateway_denied_thread_cannot_change_dispatch_token(governor):
    _pin(governor, "finlab", "token-A", "known-A")
    _pin(governor, "finlab", "token-B", "known-B")
    waiting, release, b_attempted = threading.Event(), threading.Event(), threading.Event()
    calls, logins = [], []

    def allow_a():
        waiting.set()
        assert release.wait(5)
        return _grant()

    def deny_b():
        raise QuotaBlockedError("denied B")

    def fetch(gateway, policy, attempted=None):
        if attempted:
            attempted.set()
        with authorization_scope(policy):
            return gateway.fetch_dataset(
                "price:開盤價", target_date=date(2026, 10, 12), symbols=("2330",)
            )

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: calls.append(request.url.params["api_token"]) or httpx.Response(200)
        )
    ) as http:
        sdk = _finlab_sdk(http, logins)
        a, b = FinLabSdkGateway("token-A", _sdk=sdk), FinLabSdkGateway("token-B", _sdk=sdk)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(fetch, a, allow_a)
            assert waiting.wait(5)
            second = pool.submit(fetch, b, deny_b, b_attempted)
            assert b_attempted.wait(5)
            try:
                with pytest.raises(TimeoutError):
                    second.result(timeout=0.1)
                assert logins == ["token-A"]
            finally:
                release.set()
            assert first.result(timeout=5).symbols == ("2330",)
            with pytest.raises(FinLabSdkError):
                second.result(timeout=5)
        # Denied B must release the SDK lock; a reused A selects its own token.
        fetch(a, _grant)
    assert calls == ["token-A", "token-A"]
    with governor.connection() as db:
        assert [
            tuple(row)
            for row in db.execute("SELECT account,status FROM acquisition_intent ORDER BY armed_at")
        ] == [("known-A", "finalized"), ("known-B", "cancelled"), ("known-A", "finalized")]


def test_finlab_recursive_gateway_fails_without_deadlock_or_token_change(governor):
    calls, logins = [], []
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: calls.append(request.url.params["api_token"]) or httpx.Response(200)
        )
    ) as http:
        sdk = _finlab_sdk(http, logins)
        a, b = FinLabSdkGateway("token-A", _sdk=sdk), FinLabSdkGateway("token-B", _sdk=sdk)

        def authorize():
            with pytest.raises(FinLabSdkError):
                b.fetch_dataset("price:開盤價", target_date=date(2026, 10, 12), symbols=("2330",))
            return _grant()

        with authorization_scope(authorize):
            a.fetch_dataset("price:開盤價", target_date=date(2026, 10, 12), symbols=("2330",))
    assert calls == ["token-A"] and logins == ["token-A"]


def test_finlab_sdk_token_selection_preserves_installed_environment_identity(
    governor, monkeypatch, tmp_path
):
    allocation = tmp_path / "allocation.json"
    allocation.write_text(
        json.dumps({"provider": "finlab", "account": {"account_id": "installed-A"}})
    )
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(allocation))
    monkeypatch.setenv("finlab_id_token", "original-id")
    calls, logins = [], []

    def deny():
        raise QuotaBlockedError("denied")

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: calls.append(request.url.params["api_token"]) or httpx.Response(200)
        )
    ) as http:
        sdk = _finlab_sdk(http, logins)
        b = FinLabSdkGateway("different-B", _sdk=sdk)
        with authorization_scope(deny), pytest.raises(FinLabSdkError):
            b.fetch_dataset("price:開盤價", target_date=date(2026, 10, 12), symbols=("2330",))
        assert os.environ["FINLAB_API_TOKEN"] == "environment-finlab"
        assert os.environ["finlab_id_token"] == "original-id"
        a = FinLabSdkGateway("environment-finlab", _sdk=sdk)
        with authorization_scope(_grant):
            a.fetch_dataset("price:開盤價", target_date=date(2026, 10, 12), symbols=("2330",))
    assert calls == ["environment-finlab"]
    assert os.environ["FINLAB_API_TOKEN"] == "environment-finlab"
    assert os.environ["finlab_id_token"] == "original-id"
    with governor.connection() as db:
        assert [
            tuple(row)
            for row in db.execute("SELECT account,status FROM acquisition_intent ORDER BY armed_at")
        ] == [
            ("finlab:" + hashlib.sha256(b"different-B").hexdigest(), "cancelled"),
            ("installed-A", "finalized"),
        ]
