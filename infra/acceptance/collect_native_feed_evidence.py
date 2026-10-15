"""Read seven-feed native evidence from existing units, without provider acquisition.

Uses the existing bounded SSM transport. AWS_PROFILE selects operator credentials;
only the containers read application credentials. Release/digest verification and
staging-before-production authorization remain separate operational prerequisites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from collect_staging_feed_evidence import REPO_ROOT, _aws_json, _ssm_probe
from native_feed_evidence import assess


def fetcher_spec(backend: dict[str, Any], provider: str) -> dict[str, Any]:
    plans = []
    for feed in backend["feeds"]:
        if feed["provider"] != provider or not feed.get("plan") or not feed.get("sample"):
            continue
        plan, sample = feed["plan"], feed["sample"]
        plans.append(
            {
                **{
                    key: plan[key]
                    for key in (
                        "plan_id",
                        "dataset_key",
                        "trade_date",
                        "release_id",
                        "execution_profile",
                        "parts_sha256",
                    )
                },
                **{
                    key: sample[key]
                    for key in (
                        "run_id",
                        "idempotency_key",
                        "source_request_sha256",
                        "source_raw_ref",
                        "source_raw_sha256",
                        "member_key",
                        "work_item_id",
                    )
                },
            }
        )
    return {
        "environment": backend["environment"],
        "execution_profile": backend["execution_profile"],
        "provider": provider,
        "plans": plans,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("staging", "production"), required=True)
    parser.add_argument("--profile", choices=("pilot", "full_market"), required=True)
    parser.add_argument("--trade-date", type=date.fromisoformat, required=True)
    parser.add_argument("--findb-instance-id", required=True)
    parser.add_argument("--fetcher-instance-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = json.loads((REPO_ROOT / "infra/env/bootstrap.json").read_text())["targets"][
        args.environment
    ]
    identity = _aws_json(["sts", "get-caller-identity", "--region", target["region"]])
    if identity["Account"] != target["account"]:
        raise ValueError("AWS account does not match requested environment")
    spec = {
        "environment": args.environment,
        "execution_profile": args.profile,
        "trade_date": args.trade_date.isoformat(),
    }
    backend = _ssm_probe(
        region=target["region"],
        instance_id=args.findb_instance_id,
        script=REPO_ROOT / "backend/scripts/export_native_feed_evidence.py",
        container="findb-ingest",
        environment={"FINDB_NATIVE_EVIDENCE_SPEC": json.dumps(spec)},
    )
    fetchers = {}
    for provider in ("twelve_data", "finlab", "shioaji", "taifex"):
        fetchers[provider] = _ssm_probe(
            region=target["region"],
            instance_id=args.fetcher_instance_id,
            script=REPO_ROOT / "infra/acceptance/export_native_fetcher_evidence.py",
            container=f"findb-{args.profile.replace('_', '-')}-{provider.replace('_', '-')}",
            environment={
                "FINDB_NATIVE_EVIDENCE_SPEC": json.dumps(fetcher_spec(backend["payload"], provider))
            },
        )
    result = {
        "schema_version": 1,
        **spec,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "account": identity["Account"],
        "backend": backend,
        "fetchers": fetchers,
        "probe_sha256": {
            str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                REPO_ROOT / "backend/scripts/export_native_feed_evidence.py",
                REPO_ROOT / "infra/acceptance/export_native_fetcher_evidence.py",
            )
        },
        "assessment": assess(backend["payload"], {p: r["payload"] for p, r in fetchers.items()}),
    }
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"passed": result["assessment"]["passed"], "output": str(args.output)}))
    return 0 if result["assessment"]["passed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit("native evidence collection failed") from None
