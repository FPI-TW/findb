"""Tests for the staging active-feed evidence probes and coordinator."""

from __future__ import annotations

import ast
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import random
import shlex
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import ModuleType

import pytest

from tests.script_env import script_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sample_record(schema, *, provider="finlab", run_id="run"):
    record = {
        "instrument_id": "0199aabb-ccdd-7000-8000-000000000001",
        "trade_date": "2026-10-02",
        "source": provider,
        "source_fetched_at": "2026-10-02T10:00:00+00:00",
        "run_id": run_id,
        "open": "100.10000000",
        "high": "102",
        "low": "99",
        "close": "101",
        "volume": 10,
    }
    record.update(
        {
            "market_eod": {"total_ticks": None, "turnover": "1001.0000"},
            "market_minute": {
                "bar_start_time": "2026-10-02T01:30:00Z",
                "bar_end_time": "2026-10-02T01:31:00Z",
                "signal_time": "2026-10-02T01:31:00Z",
                "market_timezone": "Asia/Taipei",
                "turnover": "1001",
                "trade_count": None,
                "price_adjustment": "none",
            },
            "futures_eod": {
                "contract_id": "contract",
                "product_code": "TX",
                "contract_code": "TX:202610",
                "contract_month": "202610",
                "session": "regular",
                "settlement_price": "101",
                "open_interest": 20,
            },
        }[schema]
    )
    return record


def test_minute_lineage_requires_the_serve_boundary() -> None:
    module = _load(
        "export_staging_feed_evidence",
        REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py",
    )

    lineage = module._lineage_contract("market_minute")

    assert lineage["serve"] == "required"
    assert lineage["serve_reason"] is None
    assert lineage["operational_read_paths"] == [
        "serve",
        "admin_market_freshness",
        "dashboard",
    ]


def test_serve_probe_fails_closed_without_static_cache_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load(
        "export_staging_feed_evidence_missing_key",
        REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py",
    )
    monkeypatch.delenv("FINDB_STATIC_CACHE_SERVE_API_KEY", raising=False)

    probe = module._serve_probe(
        "market_eod",
        {
            "sample_instrument_id": "0199aabb-ccdd-7000-8000-000000000001",
            "sample_trade_date": "2026-09-18",
        },
    )

    assert probe == {
        "success": False,
        "http_status": None,
        "reason": "serve_credential_missing",
    }


def test_serve_samples_are_selected_from_pilot_canonical_scope_with_run_lineage() -> None:
    module = _load(
        "export_staging_feed_evidence_samples",
        REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py",
    )

    sql = str(module.SAMPLES)

    assert "sample_run_id" in sql
    assert "AAPL" in sql and "MSFT" in sql
    assert "2330" in sql
    assert "market_data_eod" in sql
    assert "i.market = 'US' AND i.asset_class = 'equity'" in sql
    assert "i.market = 'TW' AND i.asset_class = 'equity'" in sql


def test_calibration_identity_is_bounded_and_unique() -> None:
    module = _load(
        "calibrate_staging_provider_alerts",
        REPO_ROOT / "infra" / "acceptance" / "calibrate_staging_provider_alerts.py",
    )
    request = {
        "request_key": "r" * 100,
        "idempotency_key": "i" * 100,
        "fetched_at": "2026-01-01T00:00:00Z",
    }

    module._identity(request, "missing-close")

    assert len(request["request_key"]) <= 100
    assert len(request["idempotency_key"]) <= 100
    assert "staging-cal-missing-close" in request["request_key"]
    assert request["fetched_at"] != "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("schema", ["market_eod", "market_minute", "futures_eod"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "other_provider"),
        ("source_fetched_at", "2026-10-02T10:00:01Z"),
        ("close", "101.00000001"),
        ("volume", 11),
    ],
)
def test_serve_probe_rejects_same_key_different_provenance_or_content(
    monkeypatch, schema, field, value
):
    exporter = _load(
        "negative_serve", REPO_ROOT / "backend/scripts/export_staging_feed_evidence.py"
    )
    record = _sample_record(schema)
    sample = {
        "sample_instrument_id": record["instrument_id"],
        "sample_trade_date": record["trade_date"],
        "sample_run_id": "run",
        "sample_record": record,
        "sample_contract_code": "TX:202610",
        "sample_session": "regular",
    }
    returned = {**record, field: value}

    class Response(BytesIO):
        status = 200

    monkeypatch.setenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "fixture-key")
    monkeypatch.setattr(
        exporter,
        "urlopen",
        lambda *args, **kwargs: Response(
            json.dumps({"success": True, "data": [returned]}).encode()
        ),
    )
    probe = exporter._serve_probe(schema, sample)
    assert probe["success"] is False and probe["run_id"] is None
    assert probe["returned_fingerprint"] is None


def test_minute_probe_matches_exact_bar_and_normalizes_decimals_and_aware_times(monkeypatch):
    exporter = _load("exact_minute", REPO_ROOT / "backend/scripts/export_staging_feed_evidence.py")
    record = _sample_record("market_minute")
    record["open"] = Decimal("100.10000000")
    sample = {
        "sample_instrument_id": record["instrument_id"],
        "sample_trade_date": record["trade_date"],
        "sample_run_id": "run",
        "sample_record": record,
    }
    returned = {**record, "open": 100.1, "source_fetched_at": "2026-10-02T18:00:00+08:00"}
    other_bar = {**returned, "bar_start_time": "2026-10-02T01:31:00Z"}

    class Response(BytesIO):
        status = 200

    def urlopen(request, timeout):
        assert "page_size=1000" in request.full_url
        return Response(json.dumps({"success": True, "data": [other_bar, returned]}).encode())

    monkeypatch.setenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "fixture-key")
    monkeypatch.setattr(exporter, "urlopen", urlopen)
    proof = exporter._serve_probe("market_minute", sample)
    assert proof["success"] is True and proof["run_id"] == "run"
    assert proof["returned_projection"]["bar_start_time"] == "2026-10-02T01:30:00.000000+00:00"
    monkeypatch.setattr(
        exporter,
        "urlopen",
        lambda *args, **kwargs: Response(
            json.dumps({"success": True, "data": [other_bar]}).encode()
        ),
    )
    assert exporter._serve_probe("market_minute", sample)["success"] is False


@pytest.fixture
def local_ssm(monkeypatch, tmp_path):
    """Execute the entire generated shell command, replacing only Docker and AWS."""
    docker = tmp_path / "docker"
    docker.write_text(
        "#!" + sys.executable + "\n"
        "import os, sys\n"
        "arguments = sys.argv[1:]\n"
        "assert arguments[:2] == ['exec', '-i']\n"
        "arguments = arguments[2:]\n"
        "environment = dict(os.environ)\n"
        "while arguments[0] == '-e':\n"
        "    key, value = arguments[1].split('=', 1)\n"
        "    environment[key] = value\n"
        "    arguments = arguments[2:]\n"
        "assert arguments[1:] == ['python', '-']\n"
        "os.execve(sys.executable, [sys.executable, '-'], environment)\n"
    )
    docker.chmod(0o700)
    collector = _load(
        "transport_acceptance", REPO_ROOT / "infra/acceptance/collect_staging_feed_evidence.py"
    )
    monkeypatch.setattr(collector, "TRANSPORT_PREFIX", str(tmp_path / "findb-staging-evidence-"))
    state = {"commands": [], "responses": {}, "mutate": lambda _config, payload: payload}

    def aws_json(arguments, *, timeout=60):
        assert 1 <= timeout <= 60
        if arguments[1] == "send-command":
            parameters = json.loads(arguments[arguments.index("--parameters") + 1])
            assert parameters["executionTimeout"] == ["130"]
            command = parameters["commands"][0]
            encoded = shlex.split(command.split("\n", 1)[1])[2]
            source = base64.b64decode(encoded).decode()
            config = ast.literal_eval(source.split("\n", 1)[0].removeprefix("config = "))
            state["commands"].append(config)
            if config["operation"] == "cleanup" and state.get("cleanup_failure"):
                raise RuntimeError("cleanup unavailable")
            result = subprocess.run(
                ["sh"],
                check=False,
                capture_output=True,
                text=True,
                input=command,
                env={**script_env(), "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
                timeout=timeout,
            )
            command_id = str(len(state["commands"]))
            payload = json.loads(result.stdout) if result.returncode == 0 else None
            if payload is not None:
                if config["operation"] == "export":
                    state["envelope"] = deepcopy(payload)
                payload = state["mutate"](config, payload)
            stdout = json.dumps(payload) if payload is not None else result.stdout
            assert len(stdout) < 24_000
            state["responses"][command_id] = {
                "Status": "Success" if result.returncode == 0 else "Failed",
                "StandardOutputContent": stdout[:24_000],
            }
            return {"Command": {"CommandId": command_id}}
        assert arguments[1] == "get-command-invocation"
        return state["responses"][arguments[arguments.index("--command-id") + 1]]

    monkeypatch.setattr(collector, "_aws_json", aws_json)
    return collector, state


def _transport_probe(collector, tmp_path, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    script = tmp_path / "probe.py"
    script.write_text("import sys\nsys.stdout.buffer.write(" + repr(raw) + ")\n")
    result = collector._ssm_probe(
        region="test", instance_id="test", script=script, container="findb-ingest"
    )
    return result, raw


def _large_transport_payload():
    # Seeded noise exercises the fallback even when gzip cannot fit stdout inline.
    rng = random.Random(17)
    return {"noise": base64.b64encode(rng.randbytes(70_000)).decode()}


@pytest.mark.parametrize("large", [False, True])
def test_ssm_transport_roundtrip_inline_and_multiple_chunks(local_ssm, tmp_path, large):
    collector, state = local_ssm
    payload = _large_transport_payload() if large else {"small": "測試"}
    result, raw = _transport_probe(collector, tmp_path, payload)
    assert result["payload"] == payload
    assert state["envelope"]["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    count = state["envelope"]["chunks"]
    assert count > 1 if large else count == 1
    assert len(state["commands"]) == count + 2 if large else len(state["commands"]) == 2
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "order",
        "duplicate",
        "tamper",
        "over_raw",
        "over_compressed",
        "over_chunks",
        "nonce",
        "decode",
        "bomb",
        "invalid_json",
        "checksum",
        "raw_checksum",
        "missing_inline",
    ],
)
def test_ssm_transport_rejects_corruption_and_cleans(local_ssm, tmp_path, mutation):
    collector, state = local_ssm

    def mutate(config, payload):
        if config["operation"] == "read" and config["index"] == 1:
            if mutation == "missing":
                payload["data"] = ""
            elif mutation == "order":
                payload["index"] += 1
            elif mutation == "duplicate":
                payload["index"] = 0
            elif mutation == "tamper":
                payload["data"] = "!" + payload["data"][1:]
        elif config["operation"] == "export":
            if mutation.startswith("over_"):
                field, maximum = {
                    "over_raw": ("raw_bytes", collector.TRANSPORT_MAX_RAW_BYTES),
                    "over_compressed": (
                        "compressed_bytes",
                        collector.TRANSPORT_MAX_COMPRESSED_BYTES,
                    ),
                    "over_chunks": ("chunks", collector.TRANSPORT_MAX_CHUNKS),
                }[mutation]
                payload[field] = maximum + 1
            elif mutation == "nonce":
                payload["nonce"] = "../../invalid"
            elif mutation == "checksum":
                payload["sha256"] = "0" * 64
            elif mutation == "raw_checksum":
                payload["raw_sha256"] = "0" * 64
            elif mutation == "missing_inline":
                payload.update(chunks=1, compressed_bytes=1, raw_bytes=1)
                payload.pop("data", None)
            elif mutation in {"decode", "bomb", "invalid_json"}:
                raw = b"x" * 100_000 if mutation == "bomb" else b"not json"
                compressed = b"invalid gzip" if mutation == "decode" else gzip.compress(raw)
                payload.update(
                    compressed_bytes=len(compressed),
                    raw_bytes=1 if mutation == "bomb" else len(raw),
                    chunks=1,
                    data=base64.b64encode(compressed).decode(),
                    sha256=hashlib.sha256(compressed).hexdigest(),
                    raw_sha256=hashlib.sha256(raw).hexdigest(),
                )
        return payload

    state["mutate"] = mutate
    with pytest.raises(RuntimeError, match="invalid SSM evidence transport"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))
    if mutation.startswith("over_") or mutation == "nonce":
        assert len(state["commands"]) == 2


def test_ssm_transport_cleanup_failure_preserves_original_error(local_ssm, tmp_path):
    collector, state = local_ssm
    state["cleanup_failure"] = True
    state["mutate"] = lambda config, payload: (
        {**payload, "nonce": "invalid"} if config["operation"] == "export" else payload
    )
    with pytest.raises(RuntimeError, match="transport identity"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"


def test_ssm_transport_rejects_invalid_path_and_non_json_export(local_ssm, tmp_path):
    collector, state = local_ssm
    with pytest.raises(ValueError, match="invalid evidence transport target"):
        collector._transport_command("findb-ingest", "../../bad", "read")
    script = tmp_path / "bad.py"
    script.write_text("print('not json')")
    with pytest.raises(RuntimeError, match="finished with status Failed"):
        collector._ssm_probe(
            region="test", instance_id="test", script=script, container="findb-ingest"
        )
    assert state["commands"][-1]["operation"] == "cleanup"


@pytest.mark.parametrize(
    "budget", ["TRANSPORT_MAX_RAW_BYTES", "TRANSPORT_MAX_COMPRESSED_BYTES", "TRANSPORT_MAX_CHUNKS"]
)
def test_ssm_wrapper_enforces_output_budgets(local_ssm, monkeypatch, tmp_path, budget):
    collector, state = local_ssm
    monkeypatch.setattr(collector, budget, 1000 if budget != "TRANSPORT_MAX_CHUNKS" else 1)
    with pytest.raises(RuntimeError, match="finished with status Failed"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))


def test_ssm_transport_total_deadline_and_cleanup(local_ssm, monkeypatch, tmp_path):
    collector, state = local_ssm
    ticks = iter([0, 1, 1, 1, 181, 181, 181, 181, 181, 181])
    monkeypatch.setattr(collector.time, "monotonic", lambda: next(ticks, 181))
    with pytest.raises(RuntimeError, match="collection timeout"):
        _transport_probe(collector, tmp_path, _large_transport_payload())
    assert state["commands"][-1]["operation"] == "cleanup"
    assert not list(tmp_path.glob("findb-staging-evidence-*.gz"))


def test_ssm_wrapper_prunes_only_expired_owned_regular_artifacts(local_ssm, tmp_path):
    collector, _state = local_ssm
    import time

    paths = [
        Path("/tmp/findb-staging-evidence-" + f"{number:032x}" + ".gz")
        for number in (701, 702, 703)
    ]
    unrelated = tmp_path / "unrelated.gz"
    unrelated.write_bytes(b"untouched")
    try:
        paths[0].write_bytes(b"expired")
        paths[1].write_bytes(b"active")
        paths[2].symlink_to(unrelated)
        past = time.time() - 86401
        os.utime(paths[0], (past, past))
        os.utime(paths[2], (past, past), follow_symlinks=False)
        _transport_probe(collector, tmp_path, {"ok": True})
        assert not paths[0].exists()
        assert paths[1].read_bytes() == b"active"
        assert paths[2].is_symlink()
        assert unrelated.read_bytes() == b"untouched"
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def test_probe_command_preserves_environment_as_literal_data(local_ssm, tmp_path):
    collector, state = local_ssm
    script = tmp_path / "environment.py"
    script.write_text(
        "import json, os\nprint(json.dumps({'provider': os.environ['FINDB_EVIDENCE_PROVIDER'], 'value': os.environ['LITERAL']}))"
    )
    value = "quote ' $(do-not-execute); newline\n資料"
    nonce = "a" * 32
    _command_id, envelope = collector._ssm_json(
        region="test",
        instance_id="test",
        deadline=collector.time.monotonic() + 60,
        command=collector._probe_command(
            script, "findb-full-market-finlab", "finlab", {"LITERAL": value}, nonce=nonce
        ),
    )
    assert collector._decode_transport(envelope, [envelope["data"]], nonce) == {
        "provider": "finlab",
        "value": value,
    }
    assert state["commands"][0]["nonce"] == nonce


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "taifex", "factset"])
def test_ssm_transport_accepts_only_generated_full_market_containers(provider) -> None:
    collector = _load(
        "full_market_transport", REPO_ROOT / "infra/acceptance/collect_staging_feed_evidence.py"
    )
    # Same container naming as collect_native_feed_evidence.py.
    container = f"findb-full-market-{provider.replace('_', '-')}"
    assert container in collector.full_market_containers()
    assert container in collector._transport_command(container, "0" * 32, "read")
    for retired in (
        f"findb-pilot-{provider.replace('_', '-')}",
        f"findb-historical-{provider.replace('_', '-')}",
        "findb-fetcher-finlab-scheduler",
        "findb-full-market-shioaji",
    ):
        with pytest.raises(ValueError, match="invalid evidence transport target"):
            collector._transport_command(retired, "0" * 32, "read")


def test_transport_allowlist_rejects_tampered_generated_inventory(tmp_path) -> None:
    collector = _load(
        "tampered_inventory", REPO_ROOT / "infra/acceptance/collect_staging_feed_evidence.py"
    )
    inventory = json.loads(collector.PROVIDER_INVENTORY.read_text())
    inventory["consumers"].append(
        {"image_key": "finlab", "name": "findb-pilot-finlab", "provider": "finlab", "role": "pilot"}
    )
    path = tmp_path / "provider_inventory.v1.json"
    path.write_text(json.dumps(inventory))
    with pytest.raises(ValueError, match="integrity"):
        collector.full_market_containers(path)
    assert not hasattr(collector, "catalog_feeds")
    assert not (REPO_ROOT / "infra/acceptance/export_fetcher_feed_evidence.py").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "source",
        "fetched_at",
        "close",
        "fingerprint",
        "sample_run",
        "current_job",
        "current_receipt",
        "current_delivery",
        "historical_receipt",
    ],
)
def test_collector_rechecks_provenance_and_current_queue_generation(mutation) -> None:
    collector = _load(
        "provenance_collector", REPO_ROOT / "infra/acceptance/collect_staging_feed_evidence.py"
    )
    backend_probe = _load(
        "provenance_backend", REPO_ROOT / "backend/scripts/export_staging_feed_evidence.py"
    )
    record = _sample_record("futures_eod", provider="taifex", run_id="run")
    projection = backend_probe._canonical_projection("futures_eod", record)
    fingerprint = backend_probe._projection_fingerprint(projection)
    feed = {
        "schema_id": "futures_eod",
        "canonical_sample": {
            "sample_instrument_id": record["instrument_id"],
            "sample_trade_date": record["trade_date"],
            "sample_run_id": "run",
            "sample_record": record,
        },
        "serve_probe": {
            "success": True,
            "http_status": 200,
            "projection_version": 1,
            "trade_date": record["trade_date"],
            "instrument_id": record["instrument_id"],
            "run_id": "run",
            "canonical_projection": projection,
            "returned_projection": projection,
            "canonical_fingerprint": fingerprint,
            "returned_fingerprint": fingerprint,
        },
    }
    historical = {
        "outbox_id": "old",
        "job_id": "job",
        "run_id": "run",
        "delivery_id": "old-generation",
        "status": "published",
        "event_type": "normalize_run",
    }
    run = {
        "run_id": "run",
        "job_id": "job",
        "job_status": "completed",
        "job_delivery_id": "delivery",
        "outbox_status": "published",
        "outbox_receipts": [
            historical,
            {**historical, "outbox_id": "new", "delivery_id": "delivery"},
        ],
    }
    if mutation in {"source", "fetched_at", "close"}:
        field = {"source": "source", "fetched_at": "source_fetched_at", "close": "close"}[mutation]
        feed["serve_probe"]["returned_projection"] = {**projection, field: "tampered"}
    elif mutation == "fingerprint":
        feed["serve_probe"]["returned_fingerprint"] = "a" * 64
    elif mutation == "sample_run":
        feed["canonical_sample"]["sample_record"]["run_id"] = "other"
    elif mutation == "current_job":
        run["job_id"] = None
    elif mutation == "current_receipt":
        run["outbox_receipts"][-1]["status"] = "failed"
    elif mutation == "current_delivery":
        run["job_delivery_id"] = "unknown"
    elif mutation == "historical_receipt":
        historical["job_id"] = "unrelated-job"
    proof = collector._valid_serve_proof(feed, "taifex", record["trade_date"], {"run"})
    queue = collector._valid_queue_receipts(run)
    assert (proof and queue) is (mutation is None)
