"""Native release acceptance never conflates profiles or substitutes no_data for real data."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "native_assembler", ROOT / "infra/acceptance/native_feed_evidence.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def complete_evidence():
    backend = {
        "environment": "staging",
        "execution_profile": "pilot",
        "trade_date": "2026-10-15",
        "feeds": [],
    }
    fetchers = {}
    for feed in json.loads((ROOT / "backend/configs/execution_feeds.v1.json").read_text())["feeds"]:
        provider, dataset = feed["provider"], feed["dataset_key"]
        plan = {
            "plan_id": dataset,
            "dataset_key": dataset,
            "provider": provider,
            "execution_profile": "pilot",
            "trade_date": "2026-10-15",
            "release_id": "universe",
            "parts_sha256": "parts",
            "summary": {
                "execution_profile": "pilot",
                "expected": 2,
                "data": 2,
                "no_data": 0,
                "missing": 0,
                "blocked": 0,
            },
        }
        sample = {
            "run_id": dataset + "-run",
            "member_key": "member",
            "work_item_id": "work",
            "idempotency_key": "idem",
            "source_raw_ref": "r2://account/bucket/object",
            "source_raw_sha256": "raw-sha",
            "source_request_sha256": "request-sha",
            "status": "completed",
            "job_status": "completed",
            "success_records": 1,
            "total_records": 1,
            "serve_probe": {"success": True},
        }
        backend["feeds"].append(
            {"provider": provider, "dataset_key": dataset, "plan": plan, "sample": sample}
        )
        exporter = fetchers.setdefault(
            provider,
            {
                "environment": "staging",
                "execution_profile": "pilot",
                "provider": provider,
                "plans": [],
            },
        )
        exporter["plans"].append(
            {
                **{k: v for k, v in plan.items() if k not in ("provider", "summary")},
                "work": [{"status": "complete", "count": 2}],
                "sample": {
                    **sample,
                    "raw_verified": True,
                    "verified_raw_bytes": 10,
                    "status": "complete",
                },
            }
        )
    return backend, fetchers


def test_exact_seven_feed_evidence_passes(complete_evidence):
    assert module.assess(*complete_evidence)["passed"]


@pytest.mark.parametrize(
    "environment,profile", [("staging", "pilot"), ("production", "full_market")]
)
def test_native_coordinator_scopes_readonly_probes(
    monkeypatch, tmp_path, complete_evidence, environment, profile
):
    import os
    import sys

    monkeypatch.syspath_prepend(str(ROOT / "infra/acceptance"))
    import collect_native_feed_evidence as collector

    backend, fetchers = deepcopy(complete_evidence)
    backend.update(environment=environment, execution_profile=profile)
    for feed in backend["feeds"]:
        feed["plan"]["execution_profile"] = profile
        feed["plan"]["summary"]["execution_profile"] = profile
    for provider in fetchers.values():
        provider.update(environment=environment, execution_profile=profile)
        for plan in provider["plans"]:
            plan["execution_profile"] = profile
    account = "439622209937" if environment == "staging" else "289112218471"
    monkeypatch.setattr(collector, "_aws_json", lambda args: {"Account": account})
    calls = []

    def probe(**kwargs):
        calls.append(kwargs)
        spec = json.loads(kwargs["environment"]["FINDB_NATIVE_EVIDENCE_SPEC"])
        assert spec["environment"] == environment and spec["execution_profile"] == profile
        provider = spec.get("provider")
        if provider:
            assert (
                kwargs["container"]
                == f"findb-{profile.replace('_', '-')}-{provider.replace('_', '-')}"
            )
            assert {p["dataset_key"] for p in spec["plans"]} == {
                f["dataset_key"] for f in backend["feeds"] if f["provider"] == provider
            }
        return {
            "command_id": str(len(calls)),
            "payload": fetchers[provider] if provider else backend,
        }

    monkeypatch.setattr(collector, "_ssm_probe", probe)
    output = tmp_path / "evidence.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect",
            "--environment",
            environment,
            "--profile",
            profile,
            "--trade-date",
            "2026-10-15",
            "--findb-instance-id",
            "backend",
            "--fetcher-instance-id",
            "fetcher",
            "--output",
            str(output),
        ],
    )
    assert collector.main() == 0
    assert len(calls) == 6
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert json.loads(output.read_text())["assessment"]["passed"]
    monkeypatch.setattr(collector, "_aws_json", lambda args: {"Account": "wrong"})
    with pytest.raises(ValueError, match="account"):
        collector.main()
    assert len(calls) == 6


@pytest.mark.parametrize(
    "mutation",
    [
        "all_no_data",
        "serve_failed",
        "raw_missing",
        "pending_checkpoint",
        "missing_feed",
        "wrong_profile",
        "other_run",
        "changed_plan",
    ],
)
def test_incomplete_or_cross_scope_evidence_never_passes(complete_evidence, mutation):
    backend, fetchers = deepcopy(complete_evidence)
    feed = backend["feeds"][0]
    exporter = fetchers[feed["provider"]]
    proof = exporter["plans"][0]
    if mutation == "all_no_data":
        feed["plan"]["summary"].update(data=0, no_data=2)
    elif mutation == "serve_failed":
        feed["sample"]["serve_probe"]["success"] = False
    elif mutation == "raw_missing":
        proof["sample"] = None
    elif mutation == "pending_checkpoint":
        proof["work"] = [{"status": "prepared", "count": 2}]
    elif mutation == "missing_feed":
        backend["feeds"].pop()
    elif mutation == "wrong_profile":
        exporter["execution_profile"] = "full_market"
    elif mutation == "other_run":
        proof["sample"]["run_id"] = "another-profile-run"
    elif mutation == "changed_plan":
        proof["parts_sha256"] = "changed"
    try:
        result = module.assess(backend, fetchers)
    except ValueError:
        assert mutation in {"missing_feed", "wrong_profile", "other_run", "changed_plan"}
    else:
        assert result["passed"] is False
