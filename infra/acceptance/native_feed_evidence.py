"""Assemble seven-feed native evidence exported by authenticated host-local probes.

This module never acquires provider data or mutates runtime state. Deployment
identity/digests and SSM command IDs accompany the saved operational evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def assess(backend: dict[str, Any], fetchers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scope = {key: backend[key] for key in ("environment", "execution_profile", "trade_date")}
    if scope["environment"] not in ("staging", "production") or scope["execution_profile"] not in (
        "pilot",
        "full_market",
    ):
        raise ValueError("invalid native evidence scope")
    catalog = json.loads((ROOT / "backend/configs/execution_feeds.v1.json").read_text())["feeds"]
    expected = {(f["provider"], f["dataset_key"]) for f in catalog}
    rows = backend["feeds"]
    actual = [(f["provider"], f["dataset_key"]) for f in rows]
    if len(rows) != 7 or len(set(actual)) != 7 or set(actual) != expected:
        raise ValueError("native evidence must contain exactly seven catalog feeds")
    results = []
    for row in rows:
        provider, dataset = row["provider"], row["dataset_key"]
        result = {
            "provider": provider,
            "dataset_key": dataset,
            "passed": False,
            "reason": row.get("reason") or "native_evidence_incomplete",
        }
        results.append(result)
        plan, sample = row.get("plan"), row.get("sample")
        if not plan or not sample:
            continue
        identity = {
            "provider": provider,
            "dataset_key": dataset,
            "execution_profile": scope["execution_profile"],
            "trade_date": scope["trade_date"],
        }
        if any(plan.get(k) != v for k, v in identity.items()):
            raise ValueError("backend plan scope mismatch")
        summary = plan["summary"]
        if not (
            summary["execution_profile"] == scope["execution_profile"]
            and summary["expected"] > 0
            and summary["data"] > 0
            and summary["data"] + summary["no_data"] == summary["expected"]
            and summary["missing"] == summary["blocked"] == 0
            and sample["status"] == "completed"
            and sample["job_status"] == "completed"
            and sample["success_records"] == sample["total_records"] > 0
            and sample["serve_probe"]["success"] is True
        ):
            continue
        exported = fetchers.get(provider)
        if not exported:
            result["reason"] = "fetcher_evidence_missing"
            continue
        if any(
            exported.get(k) != v
            for k, v in {
                "environment": scope["environment"],
                "execution_profile": scope["execution_profile"],
                "provider": provider,
            }.items()
        ):
            raise ValueError("fetcher scope mismatch")
        matches = [p for p in exported["plans"] if p["plan_id"] == plan["plan_id"]]
        if len(matches) != 1:
            result["reason"] = "checkpoint_missing_or_ambiguous"
            continue
        proof = matches[0]
        if any(
            proof.get(k) != plan[k]
            for k in (
                "plan_id",
                "dataset_key",
                "trade_date",
                "release_id",
                "execution_profile",
                "parts_sha256",
            )
        ):
            raise ValueError("frozen plan evidence mismatch")
        raw = proof.get("sample")
        if (
            not raw
            or raw["raw_verified"] is not True
            or raw["verified_raw_bytes"] <= 0
            or raw["status"] != "complete"
        ):
            result["reason"] = "verified_raw_checkpoint_missing"
            continue
        if any(
            raw.get(k) != sample[k]
            for k in (
                "run_id",
                "member_key",
                "work_item_id",
                "idempotency_key",
                "source_raw_ref",
                "source_raw_sha256",
                "source_request_sha256",
            )
        ):
            raise ValueError("Source and Fetcher receipt mismatch")
        if not sample["source_raw_ref"] or not sample["source_raw_sha256"]:
            raise ValueError("raw provenance absent")
        if sum(w["count"] for w in proof["work"]) != summary["expected"] or any(
            w["status"] != "complete" for w in proof["work"]
        ):
            result["reason"] = "checkpoint_work_incomplete"
            continue
        result.update(passed=True, reason=None, plan_id=plan["plan_id"], run_id=sample["run_id"])
    return {**scope, "feeds": results, "passed": all(f["passed"] for f in results)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--fetcher", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fetchers = {}
    for path in args.fetcher:
        data = json.loads(path.read_text())
        if data["provider"] in fetchers:
            raise ValueError("duplicate provider evidence")
        fetchers[data["provider"]] = data
    result = assess(json.loads(args.backend.read_text()), fetchers)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "output": str(args.output)}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
