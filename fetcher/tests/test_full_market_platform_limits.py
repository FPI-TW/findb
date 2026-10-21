"""Full acquisition has no readiness prerequisites; real provider limits survive."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from findb_fetcher import account_governor
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.platform_limits import SHIOAJI_DAILY_BYTES, account_identity, interval, limits
from findb_fetcher.providers.shioaji import SHIOAJI_SDK_VERSION, ShioajiSdkError
from findb_fetcher.providers.shioaji_session import ShioajiSession
from test_full_market import _runtime


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
@pytest.mark.parametrize(
    "evidence",
    [
        None,
        "invalid JSON",
        '{"expires_at":"2000-01-01T00:00:00Z","datasets":[],"requests_per_day":1}',
    ],
)
def test_native_full_acquisition_needs_no_evidence(
    tmp_path, monkeypatch, environment, provider, evidence
):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    monkeypatch.setenv("FULL_MARKET_ENABLED", "true")
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    path = tmp_path / "obsolete.json"
    monkeypatch.setenv("FETCHER_ACCOUNT_READINESS_FILE", str(path))
    if evidence is not None:
        path.write_text(evidence)
    probes = []
    monkeypatch.setattr(
        account_governor,
        "authorize",
        lambda value: probes.append(value) or {"acquisition_allowed": True},
    )
    runtime, _, _ = _runtime(tmp_path, provider=provider)
    runtime.readiness_file = path
    runtime._ensure_proof()
    assert runtime.proof["datasets"] == [feed["dataset_key"] for feed in runtime.installed_feeds]
    assert runtime.proof["requests_per_day"] is None
    before, after = runtime._reserve()
    assert (before, after) == (0, 1)
    assert probes == [{}, {}]
    permit = runtime.account_permit
    permit.state.observe_capacity(
        account=permit.account,
        window=permit.window,
        bound=1,
        observed=2,
        allocation_sha256="legacy",
    )
    assert runtime._acquisition_allowed()
    assert runtime._reserve() == (1, 2)
    with permit.state.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_capacity_violation").fetchone()[0] == 1
    runtime.close()


@pytest.mark.parametrize("provider", ["twelve_data", "shioaji"])
def test_durable_platform_pacing_and_quota_across_restart(tmp_path, provider):
    now = [datetime.now(timezone.utc).timestamp()]
    path = tmp_path / "quota.sqlite3"
    policy = limits(provider)
    state = FullMarketState(path, clock=lambda: now[0])
    args = dict(
        account="shared",
        interval=interval(policy),
        requests=1,
        byte_count=0,
        request_limit=None,
        byte_limit=policy["bytes_per_day"],
        minute_limit=policy["requests_per_minute"],
        enforce_capacity=False,
    )
    assert state.reserve_acquisition(**args)[0] == 0
    restarted = FullMarketState(path, clock=lambda: now[0])
    assert restarted.reserve_acquisition(**args)[0] == pytest.approx(
        60 / 1597 if provider == "twelve_data" else 0.2
    )
    if provider == "shioaji":
        now[0] += 1
        restarted.usage_floor(
            account="shared",
            window=datetime.fromtimestamp(now[0], timezone.utc).date().isoformat(),
            requests=0,
            byte_count=SHIOAJI_DAILY_BYTES,
        )
        with pytest.raises(QuotaBlockedError, match="exhausted"):
            restarted.reserve_acquisition(**args)
    else:
        now[0] += 1
        minute = datetime.fromtimestamp(now[0], timezone.utc).strftime("%Y-%m-%dT%H:%M")
        restarted.usage_floor(account="shared", window=minute, requests=1597, byte_count=0)
        with pytest.raises(QuotaBlockedError, match="exhausted"):
            restarted.reserve_acquisition(**args)


def test_shioaji_actual_api_usage_exhaustion_prevents_kbars():
    session = object.__new__(ShioajiSession)
    session.contracts = {"2330": object()}
    session.api = SimpleNamespace(
        usage=lambda: SimpleNamespace(bytes=SHIOAJI_DAILY_BYTES),
        kbars=lambda *args, **kwargs: pytest.fail("quota exhausted: no acquisition"),
    )
    with pytest.raises(ShioajiSdkError, match="quota exhausted"):
        session.fetch_kbars("2330", date(2026, 10, 1))


@pytest.mark.parametrize("provider", ["finlab", "taifex"])
def test_unpublished_numeric_dimensions_are_not_fabricated(provider):
    policy = limits(provider)
    assert policy["requests_per_day"] is None
    assert policy["requests_per_minute"] is None
    assert policy["requests_per_second"] is None
    assert policy["bytes_per_day"] is None


def test_optional_evidence_changes_cannot_reset_pinned_account_debt(tmp_path, monkeypatch):
    path = tmp_path / "old.account.json"
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(path))
    monkeypatch.setenv("SHIOAJI_API_KEY", "same-account")
    path.write_text(json.dumps({"provider": "shioaji", "account": {"account_id": "old-ledger"}}))
    database = tmp_path / "account.sqlite3"
    state = FullMarketState(database)
    assert account_identity("shioaji", state) == "old-ledger"
    day = datetime.now(timezone.utc).date().isoformat()
    state.usage_floor(account="old-ledger", window=day, requests=3, byte_count=SHIOAJI_DAILY_BYTES)
    path.unlink()
    restarted = FullMarketState(database)
    assert account_identity("shioaji", restarted) == "old-ledger"
    path.write_text(json.dumps({"provider": "shioaji", "account": {"account_id": "new-ledger"}}))
    assert account_identity("shioaji", restarted) == "old-ledger"
    with pytest.raises(QuotaBlockedError, match="exhausted"):
        restarted.reserve_acquisition(
            account="old-ledger",
            interval=0.2,
            requests=1,
            byte_count=0,
            request_limit=None,
            byte_limit=SHIOAJI_DAILY_BYTES,
            minute_limit=None,
            enforce_capacity=False,
        )


@pytest.mark.parametrize(
    "allocation",
    [
        None,
        "invalid JSON",
        "[]",
        '{"provider":"shioaji","account":{}}',
        '{"provider":"shioaji","account":{"account_id":null}}',
        '{"provider":"shioaji","account":{"account_id":"legacy-account"}}',
    ],
)
def test_first_pin_upgrade_keeps_legacy_rows_and_enforces_platform_limits(
    tmp_path, monkeypatch, allocation
):
    # These are the pre-identity governor tables, not a newly initialized state
    # with its mapping removed. Account IDs carried no provider association.
    database = tmp_path / "old-governor.sqlite3"
    now = [datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()]
    day = "2026-10-01"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE full_quota (account TEXT, window TEXT, requests INTEGER NOT NULL, bytes INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0, PRIMARY KEY(account,window));
            CREATE TABLE full_pacing (account TEXT PRIMARY KEY, next_at REAL NOT NULL);
        """)
        db.execute(
            "INSERT INTO full_quota VALUES(?,?,?,?,?)",
            ("legacy-account", day, 7, SHIOAJI_DAILY_BYTES, now[0] + 60),
        )
        db.execute("INSERT INTO full_pacing VALUES(?,?)", ("legacy-account", now[0] + 60))
        original_quota = db.execute("SELECT * FROM full_quota").fetchall()
        original_pacing = db.execute("SELECT * FROM full_pacing").fetchall()
    path = tmp_path / "old.account.json"
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(path))
    monkeypatch.setenv("SHIOAJI_API_KEY", "same-credential")
    if allocation is not None:
        path.write_text(allocation)
    state = FullMarketState(database, clock=lambda: now[0])
    identity = account_identity("shioaji", state)
    args = dict(
        account=identity,
        interval=0.2,
        requests=1,
        byte_count=0,
        request_limit=None,
        byte_limit=SHIOAJI_DAILY_BYTES,
        minute_limit=None,
        enforce_capacity=False,
    )
    if allocation and "legacy-account" in allocation:
        assert identity == "legacy-account"
        assert state.reserve_acquisition(**args)[0] == 60
        now[0] += 60
    else:
        # User-approved discontinuity: unknowable legacy association stays audit
        # data, while the new fingerprint ledger starts on actual platform limits.
        assert identity.startswith("shioaji:")
        assert state.reserve_acquisition(**args)[0] == 0
        assert state.reserve_acquisition(**args)[0] == pytest.approx(0.2)
        now[0] += 0.2
        assert state.reserve_acquisition(**args)[0] == 0
        state.usage_floor(account=identity, window=day, requests=0, byte_count=SHIOAJI_DAILY_BYTES)
        now[0] += 0.2
    with pytest.raises(QuotaBlockedError, match="exhausted"):
        state.reserve_acquisition(**args)
    with state.connection() as db:
        assert [
            tuple(row)
            for row in db.execute("SELECT * FROM full_quota WHERE account='legacy-account'")
        ] == original_quota
        assert [
            tuple(row)
            for row in db.execute("SELECT * FROM full_pacing WHERE account='legacy-account'")
        ] == original_pacing
    path.write_text('{"provider":"shioaji","account":{"account_id":"different-account"}}')
    restarted = FullMarketState(database, clock=lambda: now[0])
    assert account_identity("shioaji", restarted) == identity
    with pytest.raises(QuotaBlockedError, match="exhausted"):
        restarted.reserve_acquisition(**args)


def test_concurrent_first_pin_keeps_one_identity_without_relabeling_ledgers(tmp_path):
    state = FullMarketState(tmp_path / "governor.sqlite3")
    state.usage_floor(account="legacy-account", window="2026-10-01", requests=7, byte_count=100)
    with ThreadPoolExecutor(max_workers=4) as workers:
        identities = list(
            workers.map(
                lambda initial: state.account_identity("shioaji", "same-digest", initial),
                ["legacy-account", "shioaji:fingerprint", "third-account", "fourth-account"],
            )
        )
    assert len(set(identities)) == 1
    with state.connection() as db:
        assert (
            db.execute("SELECT COUNT(DISTINCT account) FROM full_account_identity").fetchone()[0]
            == 1
        )
        assert tuple(db.execute("SELECT account,requests,bytes FROM full_quota").fetchone()) == (
            "legacy-account",
            7,
            100,
        )


def test_oversized_transport_is_validation_error_without_capacity_lock(tmp_path, monkeypatch):
    from findb_fetcher.full_market_state import FullMarketStateError

    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")
    monkeypatch.setenv("FULL_MARKET_ENABLED", "true")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    monkeypatch.setattr(account_governor, "authorize", lambda _: {"acquisition_allowed": True})
    permit = account_governor.reserve("twelve_data")
    with pytest.raises(FullMarketStateError) as rejected:
        permit.observe(permit.bound + 1)
    assert not isinstance(rejected.value, QuotaBlockedError)
    with permit.state.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM full_capacity_violation").fetchone()[0] == 0
    assert account_governor.reserve("twelve_data") is not None


def test_maintenance_does_not_duplicate_runtime_provider_permit(monkeypatch):
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "maintenance")
    monkeypatch.setattr(
        account_governor, "reserve", lambda _: pytest.fail("runtime already reserved")
    )
    assert account_governor.provider_permit("finlab") is None


@pytest.mark.parametrize(
    "provider,name",
    [
        ("twelve_data", "TWELVE_DATA_API_KEY"),
        ("finlab", "FINLAB_API_TOKEN"),
    ],
)
@pytest.mark.parametrize("initial_credential", ["dummy-key", "  dummy-key  "])
def test_effective_credential_whitespace_keeps_pinned_usage_and_cooldown(
    tmp_path, monkeypatch, provider, name, initial_credential
):
    from findb_fetcher.providers.finlab import FinLabSdkGateway
    from findb_fetcher.providers.twelve_data import TwelveDataConfig

    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv(name, initial_credential)

    def effective():
        return (
            TwelveDataConfig.from_env().api_key
            if provider == "twelve_data"
            else FinLabSdkGateway.from_env().api_token
        )

    assert effective() == "dummy-key"
    now = [datetime(2026, 10, 1, 12, tzinfo=timezone.utc).timestamp()]
    database = tmp_path / "account.sqlite3"
    state = FullMarketState(database, clock=lambda: now[0])
    identity = account_identity(provider, state)
    state.usage_floor(account=identity, window="2026-10-01", requests=7, byte_count=100)
    state.rate_limited(account=identity, window="2026-10-01", until=now[0] + 60)
    for credential in [" dummy-key ", "dummy-key", "\tdummy-key\n"]:
        monkeypatch.setenv(name, credential)
        assert effective() == "dummy-key"
        restarted = FullMarketState(database, clock=lambda: now[0])
        assert account_identity(provider, restarted) == identity
        wait, _, _, _ = restarted.reserve_acquisition(
            account=identity,
            interval=0,
            requests=1,
            byte_count=0,
            request_limit=None,
            byte_limit=None,
            minute_limit=None,
            enforce_capacity=False,
        )
        assert wait == 60
        with restarted.connection() as db:
            assert tuple(
                db.execute(
                    "SELECT requests,bytes,blocked_until FROM full_quota WHERE account=? AND window=?",
                    (identity, "2026-10-01"),
                ).fetchone()
            ) == (7, 100, now[0] + 60)
            assert (
                db.execute("SELECT COUNT(DISTINCT account) FROM full_account_identity").fetchone()[
                    0
                ]
                == 1
            )
    if provider == "twelve_data":
        now[0] += 60
        state.usage_floor(account=identity, window="2026-10-01T12:01", requests=1597, byte_count=0)
        with pytest.raises(QuotaBlockedError, match="exhausted"):
            restarted.reserve_acquisition(
                account=identity,
                interval=0,
                requests=1,
                byte_count=0,
                request_limit=None,
                byte_limit=None,
                minute_limit=1597,
                enforce_capacity=False,
            )


def test_shioaji_full_session_effective_key_and_account_are_whitespace_normalized(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("importlib.metadata.version", lambda name: SHIOAJI_SDK_VERSION)
    login = []
    api = SimpleNamespace(
        login=lambda **kwargs: login.append(kwargs["api_key"]),
        logout=lambda: None,
        Contracts=SimpleNamespace(Stocks=SimpleNamespace(TSE=[], OTC=[])),
    )
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(tmp_path / "missing.json"))
    state = FullMarketState(tmp_path / "account.sqlite3")
    monkeypatch.setenv("SHIOAJI_API_KEY", "key")
    first = account_identity("shioaji", state)
    monkeypatch.setenv("SHIOAJI_API_KEY", " key ")
    session = ShioajiSession(
        " key ", "secret", simulation=False, sdk=SimpleNamespace(Shioaji=lambda **kwargs: api)
    )
    assert login == ["key"]
    assert account_identity("shioaji", state) == first
    session.close()


@pytest.mark.parametrize(
    "provider,name",
    [
        ("twelve_data", "TWELVE_DATA_API_KEY"),
        ("finlab", "FINLAB_API_TOKEN"),
    ],
)
def test_existing_raw_whitespace_pin_migrates_without_resetting_known_debt(
    tmp_path, monkeypatch, provider, name
):
    import hashlib

    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(tmp_path / "missing.json"))
    credential = " dummy-key "
    monkeypatch.setenv(name, credential)
    database = tmp_path / "account.sqlite3"
    now = datetime.now(timezone.utc).timestamp()
    state = FullMarketState(database, clock=lambda: now)
    raw_digest = hashlib.sha256(credential.encode()).hexdigest()
    state.account_identity(provider, raw_digest, "known-padded-ledger")
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    state.usage_floor(account="known-padded-ledger", window=day, requests=7, byte_count=100)
    state.rate_limited(account="known-padded-ledger", window=day, until=now + 60)
    assert account_identity(provider, state) == "known-padded-ledger"
    monkeypatch.setenv(name, "dummy-key")
    restarted = FullMarketState(database, clock=lambda: now)
    assert account_identity(provider, restarted) == "known-padded-ledger"
    assert (
        restarted.reserve_acquisition(
            account="known-padded-ledger",
            interval=0,
            requests=1,
            byte_count=0,
            request_limit=None,
            byte_limit=None,
            minute_limit=None,
            enforce_capacity=False,
        )[0]
        == 60
    )
    with restarted.connection() as db:
        assert tuple(
            db.execute(
                "SELECT requests,bytes FROM full_quota WHERE account=? AND window=?",
                ("known-padded-ledger", day),
            ).fetchone()
        ) == (7, 100)
