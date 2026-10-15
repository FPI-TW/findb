"""Verify immutable prepared requests and R2 bytes without any provider acquisition."""

import hashlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.raw_storage import RawStorageConfig

spec = importlib.util.spec_from_file_location(
    "native_evidence",
    Path(__file__).resolve().parents[2] / "infra/acceptance/export_native_fetcher_evidence.py",
)
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    state = FullMarketState(tmp_path / "state.sqlite3")
    state.bind_profile("pilot")
    plan = {
        "plan_id": "plan",
        "dataset_key": "us_equity_eod",
        "trade_date": "2026-10-15",
        "release_id": "universe",
        "execution_profile": "pilot",
        "parts": [
            {
                "part_id": "part",
                "work_item_id": "fp1:part",
                "member_keys": ["A"],
                "member_sha256": "x",
            }
        ],
    }
    state.record_plan(plan, "twelve_data")
    raw = b'{"native":1}'
    digest = hashlib.sha256(raw).hexdigest()
    ref = "r2://" + "a" * 32 + "/fixture-raw/twelve_data/us_equity_eod/symbol/" + digest + ".json"
    request = {
        "source": "twelve_data",
        "dataset_key": "us_equity_eod",
        "idempotency_key": "key",
        "delivery": {"work_item_id": "fp1:part"},
        "payload": {
            "batch": {"source_raw_ref": ref, "source_raw_sha256": digest},
            "data": [{"symbol": "A"}],
        },
    }
    state.prepare("fp1:part:A", canonical_bytes([request]))
    state.finish("fp1:part:A", status="complete", receipt="run")
    target = {k: v for k, v in plan.items() if k != "parts"}
    target.update(
        parts_sha256=hashlib.sha256(canonical_bytes(plan["parts"])).hexdigest(),
        run_id="run",
        idempotency_key="key",
        source_request_sha256=hashlib.sha256(canonical_bytes(request)).hexdigest(),
        source_raw_ref=ref,
        source_raw_sha256=digest,
        member_key="A",
        work_item_id="fp1:part",
    )
    config = RawStorageConfig(
        account_id="a" * 32, bucket="fixture-raw", access_key_id="test", secret_access_key="test"
    )
    stream = io.BytesIO(raw)
    calls = []

    def get(**kwargs):
        calls.append(kwargs)
        return {
            "Body": stream,
            "ContentLength": len(raw),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    monkeypatch.setattr(evidence.RawStorageConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        evidence,
        "R2RawPayloadStore",
        lambda config: SimpleNamespace(_client=SimpleNamespace(get_object=get)),
    )
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "pilot")
    monkeypatch.setenv("FETCHER_STATE_PATH", str(state.path))
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(state.path))
    return (
        state,
        {
            "environment": "staging",
            "execution_profile": "pilot",
            "provider": "twelve_data",
            "plans": [target],
        },
        stream,
        calls,
    )


def test_verified_native_checkpoint_and_exact_raw_bytes(native_case):
    state, spec, stream, calls = native_case
    before = state.prepared("fp1:part:A")
    result = evidence.export(spec)
    sample = result["plans"][0]["sample"]
    assert sample["raw_verified"] and sample["status"] == "complete"
    assert sample["member_key"] == "A" and sample["run_id"] == "run"
    assert stream.closed and len(calls) == 1
    assert state.prepared("fp1:part:A") == before
    assert result["governor"]["official_usage"] == []
    assert result["governor"]["scope"] == "shared_environment_account"


@pytest.mark.parametrize(
    "field,value",
    [
        ("parts_sha256", "wrong"),
        ("source_request_sha256", "wrong"),
        ("source_raw_ref", "r2://other/bucket/key"),
        ("member_key", "B"),
        ("work_item_id", "other"),
    ],
)
def test_cross_scope_or_changed_request_rejected_before_r2(native_case, field, value):
    _, spec, _, calls = native_case
    spec["plans"][0][field] = value
    with pytest.raises(ValueError):
        evidence.export(spec)
    assert not calls


def test_corrupt_r2_bytes_rejected_and_closed(native_case):
    _, spec, stream, _ = native_case
    stream.seek(0)
    stream.write(b"X")
    stream.seek(0)
    with pytest.raises(ValueError, match="integrity"):
        evidence.export(spec)
    assert stream.closed


def test_other_profile_rejected_before_read(native_case):
    _, spec, _, calls = native_case
    spec["execution_profile"] = "full_market"
    with pytest.raises(ValueError, match="identity"):
        evidence.export(spec)
    assert not calls


def test_governor_keeps_unknown_liability_and_official_usage(native_case):
    state, spec, _, _ = native_case
    with state.connection() as db:
        db.execute(
            "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,operation_kind,outcome) VALUES('unknown','account','twelve_data',1,'unknown',60,'acquisition','unknown')"
        )
        db.execute(
            "INSERT INTO acquisition_usage(operation_id,observed_at,bytes,limit_bytes,remaining_bytes,connections) VALUES('unknown',2,10,100,90,2)"
        )
    result = evidence.export(spec)["governor"]
    assert result["liabilities"] == [
        {"operation_kind": "acquisition", "status": "unknown", "count": 1}
    ]
    assert result["official_usage"][0]["remaining_bytes"] == 90
    assert result["recent_operations"][0]["outcome"] == "unknown"
    with state.connection() as db:
        assert db.execute("SELECT status FROM acquisition_intent").fetchone()[0] == "unknown"
