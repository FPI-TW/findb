"""Installed allocation accounting is independent of Full-market readiness expiry."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from findb_fetcher import account_governor
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError


def allocation(tmp_path, monkeypatch):
    value = {
        "provider": "twelve_data",
        "environment": "local",
        "requests_per_day": 20,
        "requests_per_minute": 20,
        "requests_per_second": 1000000,
        "bytes_per_day": 2000,
        "max_response_bytes": 100,
        "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "account": {
            "account_id": "shared",
            "governor_identity": str(tmp_path / "account.sqlite3"),
            "consumers": ["bounded", "full_market", "historical"],
            "other_requests_per_day": 2,
            "other_bytes_per_day": 200,
        },
    }
    path = tmp_path / "twelve_data.account.json"
    path.write_text(json.dumps(value))
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "bounded")
    monkeypatch.setenv("FETCHER_ACCOUNT_READINESS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(path))
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", value["account"]["governor_identity"])
    return value


def legacy_capacity_violation(tmp_path):
    """Seed legacy diagnostic evidence; it remains inspectable/repairable independently."""
    state = FullMarketState(tmp_path / "account.sqlite3")
    window = datetime.now(timezone.utc).date().isoformat()
    state.usage_floor(account="shared", window=window, requests=1, byte_count=100)
    state.observe_capacity(
        account="shared", window=window, bound=100, observed=250, allocation_sha256=""
    )
    return state, window


def test_unset_consumer_is_bounded_and_retains_bounded_runtime(monkeypatch, tmp_path):
    monkeypatch.delenv("FETCHER_CONSUMER_PROFILE", raising=False)
    monkeypatch.delenv("FETCHER_ACCOUNT_STATE_PATH", raising=False)
    monkeypatch.delenv("SOURCE_API_URL", raising=False)
    assert account_governor.consumer() == "bounded"
    assert account_governor.consumer() in account_governor.BOUNDED_CONSUMERS
    # Bounded runtimes keep their own control/authorization and never call Source here.
    assert account_governor.authorize({}) == {"acquisition_allowed": True}


def test_full_market_authorization_never_sends_a_retired_profile(monkeypatch):
    from findb_fetcher import full_market_client

    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")
    paths = []

    class Source:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def request(self, method, path):
            paths.append(path)
            return {"acquisition_allowed": True}

    monkeypatch.setattr(full_market_client, "FullMarketSourceClient", Source)
    monkeypatch.setattr(
        "findb_fetcher.config.FetcherConfig.from_env", classmethod(lambda cls: None)
    )
    assert account_governor.authorize({}) == {"acquisition_allowed": True}
    assert paths == ["full-market/authorization?consumer=full_market&execution_profile=full_market"]


def test_all_source_clients_share_pacing_during_expired_drain(tmp_path, monkeypatch):
    import httpx

    from findb_fetcher.client import SourceAPIClient
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.contracts import ContractRegistry
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.scheduler_control import SchedulerControlClient

    value = allocation(tmp_path, monkeypatch)
    value["source_requests_per_minute"] = 120
    (tmp_path / "twelve_data.account.json").write_text(json.dumps(value))
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")
    clock = [datetime.now(timezone.utc).timestamp()]
    actual = []
    real = FullMarketState
    monkeypatch.setattr(
        account_governor, "FullMarketState", lambda path: real(path, clock=lambda: clock[0])
    )
    monkeypatch.setattr(
        account_governor.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def respond(request):
        actual.append(clock[0])
        return httpx.Response(200, json={})

    real(tmp_path / "account.sqlite3").observe_capacity(
        account="shared",
        window=datetime.now(timezone.utc).date().isoformat(),
        bound=100,
        observed=250,
        allocation_sha256="a" * 64,
    )

    config = FetcherConfig(
        "https://source.example",
        "same-source-key",
        contracts_dir=__import__("pathlib").Path(__file__).resolve().parents[2] / "contracts",
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        governance = FullMarketSourceClient(config, client=http)
        delivery = SourceAPIClient(config, ContractRegistry(config.contracts_dir), client=http)
        control = SchedulerControlClient(config, "full_market_twelve_data_v1", client=http)
        for _ in range(40):
            governance.request("GET", "delivery-plans")
            delivery._request_json(
                "GET", "/api/v1/source/runs", deadline=None, monotonic=lambda: clock[0]
            )
            control._request(b"{}")
    assert len(actual) == 120
    assert (
        actual[-1] == actual[0]
    )  # Source enforces its actual key rate limit; no allocation budget.
    state = real(tmp_path / "account.sqlite3")
    with state.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM full_quota WHERE account='shared'").fetchone()[0] == 0
        )


def test_capacity_violation_blocks_restart_and_next_day(tmp_path, monkeypatch):
    allocation(tmp_path, monkeypatch)
    _, window = legacy_capacity_violation(tmp_path)
    restarted = FullMarketState(
        tmp_path / "account.sqlite3", clock=lambda: datetime.now(timezone.utc).timestamp() + 86400
    )
    with pytest.raises(QuotaBlockedError, match="capacity violation"):
        restarted.reserve_acquisition(
            account="shared",
            interval=1,
            requests=1,
            byte_count=100,
            request_limit=100,
            byte_limit=10000,
            minute_limit=100,
        )
    with restarted.connection() as db:
        row = dict(db.execute("SELECT * FROM full_capacity_violation").fetchone())
    with pytest.raises(QuotaBlockedError, match="does not cover"):
        restarted.repair_capacity(
            account="shared",
            violation_id=row["violation_id"],
            corrected_bound=249,
            allocation_sha256="b" * 64,
            declaration_sha256="c" * 64,
        )
    with pytest.raises(QuotaBlockedError, match="changed"):
        restarted.repair_capacity(
            account="shared",
            violation_id="stale",
            corrected_bound=300,
            allocation_sha256="b" * 64,
            declaration_sha256="c" * 64,
        )
    restarted.repair_capacity(
        account="shared",
        violation_id=row["violation_id"],
        corrected_bound=300,
        allocation_sha256="b" * 64,
        declaration_sha256="c" * 64,
    )
    with restarted.connection() as db:
        assert (
            db.execute(
                "SELECT bytes FROM full_quota WHERE account='shared' AND window=?", (window,)
            ).fetchone()[0]
            == 250
        )
        assert db.execute("SELECT count(*) FROM full_capacity_repair").fetchone()[0] == 1
    assert (
        restarted.reserve_acquisition(
            account="shared",
            interval=1,
            requests=1,
            byte_count=300,
            request_limit=100,
            byte_limit=10000,
            minute_limit=100,
        )[0]
        == 0
    )


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing-enrollment",
        "bad-uuid",
        "missing-source",
        "noncanonical",
        "wrong-runtime",
        "not-installed",
        "digest",
        "revoked",
        "replaced",
        "network",
        "stale-ack",
        "changed-authority",
    ],
)
def test_trusted_capacity_repair_requires_current_source_authority_and_installed_correction(
    tmp_path,
    monkeypatch,
    failure,
):
    import hashlib
    from copy import deepcopy
    from urllib.parse import parse_qs
    from uuid import UUID

    import httpx

    from findb_fetcher import full_market_cli
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_universe import canonical_bytes, checksum
    from findb_fetcher.installation_receipt import installed_artifact_digest
    from test_full_market import _proof

    value = allocation(tmp_path, monkeypatch)
    state, window = legacy_capacity_violation(tmp_path)
    with state.connection() as db:
        violation_id = db.execute("SELECT violation_id FROM full_capacity_violation").fetchone()[0]
    value.pop("expires_at")
    value.update(max_response_bytes=300, bytes_per_day=4000)
    value["account"]["other_bytes_per_day"] = 1000
    value["account"].update(
        environment_exclusive=True,
        environment_allocations={},
        provider_totals={},
        usage_day=datetime.now(timezone.utc).date().isoformat(),
        used_requests=0,
        used_bytes=0,
    )
    value["requests_per_second"] = float(value["requests_per_second"])
    value["source_requests_per_minute"] = 120
    path = tmp_path / "twelve_data.account.json"
    path.write_bytes(canonical_bytes(value))
    config = tmp_path / "config.json"
    config.write_text('{"deployment_target":"local"}')
    artifact = installed_artifact_digest(Path(full_market_cli.__file__).parent)
    runtime_id = (
        "local:"
        + hashlib.sha256(
            artifact.encode() + config.read_bytes() + str(tmp_path / "account.sqlite3").encode()
        ).hexdigest()
    )
    proof = {
        **_proof(datetime.now(timezone.utc)),
        **value,
        "version": 1,
        "source_client_id": str(UUID(int=1)),
        "runtime_id": runtime_id,
        "artifact_sha256": artifact,
        "config_sha256": checksum(config.read_bytes()),
        "account_allocation_sha256": checksum(canonical_bytes(value)),
        "provider_call_seconds": 1.0,
        "source_check_seconds": 1.0,
        "completion_window_seconds": 14400,
    }
    proof["verified_at"] = proof["verified_at"].replace("+00:00", "Z")
    proof["expires_at"] = proof["expires_at"].replace("+00:00", "Z")
    metadata = {
        "enrollment_id": str(UUID(int=2)),
        "declaration_sha256": checksum(canonical_bytes(proof)),
        "runtime_id": proof["runtime_id"],
        "datasets": proof["datasets"],
    }
    authority = {
        "enrollment_id": metadata["enrollment_id"],
        "source_client_id": proof["source_client_id"],
        "declaration_sha256": metadata["declaration_sha256"],
        "declaration": deepcopy(proof),
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "installation_sha256": "d" * 64,
    }
    readiness, envelope = tmp_path / "corrected.json", tmp_path / "enrollment.json"
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "full_market")
    monkeypatch.setenv("FETCHER_BOOTSTRAP_CONFIG", str(config))
    monkeypatch.setenv("FETCHER_ACCOUNT_READINESS_FILE", str(readiness))
    monkeypatch.setenv("SOURCE_API_URL", "http://localhost:8080")
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "current-source-key")
    if failure == "missing-enrollment":
        metadata.pop("enrollment_id")
    elif failure == "bad-uuid":
        metadata["enrollment_id"] = "wrong"
    elif failure == "missing-source":
        proof.pop("source_client_id")
    elif failure == "noncanonical":
        proof["requests_per_second"] = int(proof["requests_per_second"])
    elif failure == "wrong-runtime":
        proof["runtime_id"] = metadata["runtime_id"] = "never-installed-runtime"
        metadata["declaration_sha256"] = checksum(canonical_bytes(proof))
    elif failure == "not-installed":
        other = tmp_path / "other-allocation.json"
        other.write_bytes(canonical_bytes(value))
        monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(other))
    elif failure == "digest":
        metadata["declaration_sha256"] = "a" * 64
    elif failure == "stale-ack":
        authority["reported_at"] = (datetime.now(timezone.utc) - timedelta(seconds=91)).isoformat()
    elif failure == "changed-authority":
        authority["declaration"]["account"]["account_id"] = "another-account"
    readiness.write_bytes(canonical_bytes(proof))
    envelope.write_bytes(canonical_bytes(metadata))
    requests = []

    def verify(request):
        requests.append(request)
        assert request.headers["X-API-Key"] == "current-source-key"
        assert request.url.path == "/api/v1/source/full-market/enrollment-verification"
        assert parse_qs(request.url.query.decode())["enrollment_id"] == [str(UUID(int=2))]
        if failure == "network":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(403 if failure in {"revoked", "replaced"} else 200, json=authority)

    http = httpx.Client(transport=httpx.MockTransport(verify))
    monkeypatch.setattr(
        "findb_fetcher.full_market_client.FullMarketSourceClient",
        lambda config: FullMarketSourceClient(config, client=http),
    )
    kwargs = dict(
        provider="twelve_data",
        environment="local",
        governor=tmp_path / "account.sqlite3",
        readiness=readiness,
        allocation=path,
        enrollment=envelope,
        violation_id=violation_id,
    )
    if failure is not None:
        with pytest.raises(QuotaBlockedError):
            account_governor.repair_capacity(**kwargs)
        with state.connection() as db:
            assert db.execute("SELECT count(*) FROM full_capacity_violation").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM full_capacity_repair").fetchone()[0] == 0
            assert (
                db.execute(
                    "SELECT bytes FROM full_quota WHERE account='shared' AND window=?",
                    (window,),
                ).fetchone()[0]
                == 250
            )
        if failure in {
            "missing-enrollment",
            "bad-uuid",
            "missing-source",
            "noncanonical",
            "wrong-runtime",
            "not-installed",
            "digest",
        }:
            assert requests == []
    else:
        account_governor.repair_capacity(**kwargs)
        assert len(requests) == 1
        with state.connection() as db:
            assert (
                db.execute(
                    "SELECT bytes FROM full_quota WHERE account='shared' AND window=?",
                    (window,),
                ).fetchone()[0]
                == 250
            )
            assert db.execute("SELECT count(*) FROM full_capacity_repair").fetchone()[0] == 1
    # Source prepared delivery permits remain independent of provider violations.
    account_governor.source_permit("prepared-drain-source")
    http.close()
