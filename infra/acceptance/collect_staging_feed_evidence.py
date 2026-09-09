#!/usr/bin/env python3
"""Collect one sanitized staging active-feed acceptance evidence package.

The coordinator runs locally. It injects read-only probes into the existing
Fetcher and FinDB execution units through SSM; it neither adds a host nor
creates a cross-host runtime dependency.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FETCHER_PROBE = REPO_ROOT / "infra" / "acceptance" / "export_fetcher_feed_evidence.py"
FINDB_PROBE = REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py"
FETCHER_CONTAINERS = {
    "twelve_data": "findb-fetcher-scheduler",
    "finlab": "findb-fetcher-finlab-scheduler",
    "shioaji": "findb-fetcher-shioaji-scheduler",
}


def _run(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _aws_json(arguments: list[str], *, timeout: int = 60) -> dict[str, Any]:
    result = _run(["aws", *arguments, "--output", "json"], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"AWS CLI command failed: {' '.join(arguments[:3])}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS CLI returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("AWS CLI returned an unexpected response")
    return value


def _probe_command(
    script: Path,
    container: str,
    provider: str | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    encoded = base64.b64encode(script.read_bytes()).decode("ascii")
    variables = dict(environment or {})
    if provider:
        variables["FINDB_EVIDENCE_PROVIDER"] = provider
    environment_args = "".join(
        f"-e {shlex.quote(name)}={shlex.quote(value)} " for name, value in sorted(variables.items())
    )
    return (
        "set -eu\n"
        f"printf '%s' '{encoded}' | base64 -d | "
        f"docker exec -i {environment_args}{container} python -"
    )


def _ssm_probe(
    *, region: str, instance_id: str, command: str, timeout_seconds: int = 180
) -> dict[str, Any]:
    sent = _aws_json(
        [
            "ssm",
            "send-command",
            "--region",
            region,
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            "FinDB staging active-feed read-only evidence probe",
            "--parameters",
            json.dumps({"commands": [command]}, separators=(",", ":")),
        ]
    )
    command_id = str(sent["Command"]["CommandId"])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = _aws_json(
            [
                "ssm",
                "get-command-invocation",
                "--region",
                region,
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            ]
        )
        status = response.get("Status")
        if status == "Success":
            try:
                payload = json.loads(str(response["StandardOutputContent"]).strip())
            except (KeyError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"SSM probe {command_id} returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError(f"SSM probe {command_id} returned an invalid payload")
            return {"command_id": command_id, "payload": payload}
        if status in {"Cancelled", "Failed", "TimedOut", "Undeliverable", "Terminated"}:
            raise RuntimeError(f"SSM probe {command_id} finished with status {status}")
        time.sleep(2)
    raise RuntimeError(f"SSM probe {command_id} did not finish before timeout")


def _assessment(findb: dict[str, Any], fetcher: dict[str, Any]) -> dict[str, Any]:
    backend_feeds = {(item["source"], item["dataset_key"]): item for item in findb["feeds"]}
    fetcher_by_provider = {item["provider"]: item for item in fetcher.values()}
    source_202 = {}
    for provider, dataset in (("finlab", "tw_equity_eod"), ("twelve_data", "us_equity_eod")):
        rows = backend_feeds[(provider, dataset)]["runs"]
        source_202[f"{provider}/{dataset}"] = any(
            row["source_http_status"] == 202 and row["attempt_status"] in {"accepted", "duplicate"}
            for row in rows
        )
    minute_boundaries = {
        dataset: backend_feeds[("shioaji", dataset)]["lineage_contract"]["serve"]
        for dataset in ("tw_equity_minute", "tw_etf_minute")
    }
    backend_multi_date = all(item["multi_trade_date_ready"] for item in findb["feeds"])
    fetcher_multi_date = all(
        len(
            {
                str(row.get("target_data_date") or row.get("target_date"))
                for row in item["recent_trade_dates"]
            }
        )
        >= 2
        for item in fetcher_by_provider.values()
    )
    return {
        "four_feed_scope_complete": len(backend_feeds) == 4,
        "backend_multi_trade_date_complete": backend_multi_date,
        "fetcher_multi_trade_date_complete": fetcher_multi_date,
        "persistent_source_202": source_202,
        "minute_serve_boundary": minute_boundaries,
        "minute_serve_boundary_complete": set(minute_boundaries.values()) == {"not_applicable"},
    }


def _git_sha() -> str:
    result = _run(["git", "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _upload(*, path: Path, region: str, bucket: str, key: str, kms_key_arn: str) -> dict[str, Any]:
    response = _aws_json(
        [
            "s3api",
            "put-object",
            "--region",
            region,
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(path),
            "--content-type",
            "application/json",
            "--server-side-encryption",
            "aws:kms",
            "--ssekms-key-id",
            kms_key_arn,
            "--if-none-match",
            "*",
        ]
    )
    return {
        "bucket": bucket,
        "key": key,
        "version_id": response.get("VersionId"),
        "server_side_encryption": response.get("ServerSideEncryption"),
        "ssekms_key_id": response.get("SSEKMSKeyId"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--findb-instance-id", required=True)
    parser.add_argument("--fetcher-instance-id", required=True)
    parser.add_argument("--phase", choices=("pre", "post"), required=True)
    parser.add_argument("--pre-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--kms-key-arn")
    parser.add_argument("--s3-key")
    args = parser.parse_args()
    if args.phase == "post" and args.pre_manifest is None:
        parser.error("--pre-manifest is required for post phase")
    if bool(args.bucket) != bool(args.kms_key_arn):
        parser.error("--bucket and --kms-key-arn must be supplied together")

    findb_result = _ssm_probe(
        region=args.region,
        instance_id=args.findb_instance_id,
        command=_probe_command(FINDB_PROBE, "findb-ingest"),
    )
    fetcher_results = {
        provider: _ssm_probe(
            region=args.region,
            instance_id=args.fetcher_instance_id,
            command=_probe_command(FETCHER_PROBE, container, provider),
        )
        for provider, container in FETCHER_CONTAINERS.items()
    }
    captured_at = datetime.now(timezone.utc).isoformat()
    pre_sha256 = None
    if args.pre_manifest:
        pre_sha256 = hashlib.sha256(args.pre_manifest.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "environment": "staging",
        "phase": args.phase,
        "captured_at": captured_at,
        "repository_commit": _git_sha(),
        "topology": {
            "kind": "single_coordinator_unit_local_probes",
            "additional_hosts": 0,
            "runtime_cross_host_dependency": False,
        },
        "pre_manifest_sha256": pre_sha256,
        "probe_command_ids": {
            "findb": findb_result["command_id"],
            **{
                f"fetcher_{provider}": result["command_id"]
                for provider, result in fetcher_results.items()
            },
        },
        "findb": findb_result["payload"],
        "fetcher": {provider: result["payload"] for provider, result in fetcher_results.items()},
    }
    manifest["assessment"] = _assessment(manifest["findb"], manifest["fetcher"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    args.output.write_text(serialized, encoding="utf-8")
    os.chmod(args.output, 0o600)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    upload = None
    if args.bucket:
        key = args.s3_key or (
            f"evidence/staging/active-feeds/{captured_at[:10]}/{args.phase}-{digest[:16]}.json"
        )
        upload = _upload(
            path=args.output,
            region=args.region,
            bucket=args.bucket,
            key=key,
            kms_key_arn=args.kms_key_arn,
        )
    print(json.dumps({"sha256": digest, "output": str(args.output), "upload": upload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
