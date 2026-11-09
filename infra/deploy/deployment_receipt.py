#!/usr/bin/env python3
"""Versioned acceptance identities; immutable release evidence is not a host lease."""

from __future__ import annotations

import argparse
import json
import re
import stat
from pathlib import Path

PROTOCOL = "single-pass-v1"


def validate(
    record: dict, manifest: dict, *, bundle_sha: str, transaction_id: str | None = None
) -> None:
    version = manifest["schema_version"]
    if type(record.get("schema_version")) is not int or record["schema_version"] != version:
        raise ValueError("acceptance schema mismatch")
    for field in (
        "unit",
        "deployment_target",
        "commit_sha",
        "release_tag",
        "images",
        "migration_revision",
    ):
        if record.get(field) != manifest[field]:
            raise ValueError("acceptance release identity mismatch")
    if manifest["unit"] == "fetcher" and record.get("runtime_profile") != manifest.get(
        "runtime_profile"
    ):
        raise ValueError("acceptance runtime profile mismatch")
    prefix = manifest["unit"] + (
        "/production" if manifest["deployment_target"] == "production" else ""
    )
    expected_key = f"{prefix}/accepted/{manifest['commit_sha']}/{bundle_sha}.tar"
    if (
        record.get("state") != "accepted"
        or record.get("bundle_sha256") != bundle_sha
        or record.get("bundle_key") != expected_key
    ):
        raise ValueError("acceptance bundle mismatch")
    if not re.fullmatch(
        r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", record.get("ssm_command_id", "")
    ):
        raise ValueError("acceptance command identity invalid")
    if version == 4:
        if record.get("deployment_protocol") != PROTOCOL or record.get("phase") != "healthy":
            raise ValueError("acceptance phase mismatch")
        tx = record.get("transaction_id", "")
        if not re.fullmatch(re.escape(bundle_sha) + r"-[0-9]+-[0-9]+", tx):
            raise ValueError("acceptance transaction invalid")
        if (
            tx
            != f"{bundle_sha}-{record.get('workflow_run_id')}-{record.get('workflow_run_attempt')}"
        ):
            raise ValueError("acceptance run mismatch")
        if transaction_id is not None and tx != transaction_id:
            raise ValueError("acceptance belongs to another attempt")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--bundle-sha", required=True)
    parser.add_argument("--transaction-id")
    args = parser.parse_args()
    validate(
        json.loads(args.record.read_text()),
        json.loads(args.manifest.read_text()),
        bundle_sha=args.bundle_sha,
        transaction_id=args.transaction_id,
    )


def publish(
    *,
    manifest_path: Path,
    bundle_path: Path,
    bucket: str,
    kms_key: str,
    command_id: str,
    validator_sha: str,
    run_id: str,
    attempt: str,
) -> str:
    """Runner-only conditional publication, including a fresh decision for replay."""
    import hashlib
    import subprocess
    import tempfile

    manifest = json.loads(manifest_path.read_text())
    sha = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    tx = f"{sha}-{run_id}-{attempt}"
    prefix = manifest["unit"] + (
        "/production" if manifest["deployment_target"] == "production" else ""
    )
    accepted_key = f"{prefix}/accepted/{manifest['commit_sha']}/{sha}.tar"
    record = {
        key: manifest[key]
        for key in (
            "schema_version",
            "deployment_protocol",
            "unit",
            "deployment_target",
            "commit_sha",
            "images",
            "migration_revision",
            "release_tag",
        )
    }
    if manifest["unit"] == "fetcher":
        record["runtime_profile"] = manifest["runtime_profile"]
    record.update(
        bundle_sha256=sha,
        bundle_key=accepted_key,
        transaction_id=tx,
        workflow_run_id=run_id,
        workflow_run_attempt=attempt,
        ssm_command_id=command_id,
        validator_sha256=validator_sha,
        state="accepted",
        phase="healthy",
    )
    validate(record, manifest, bundle_sha=sha, transaction_id=tx)
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        body = work / "record.json"
        body.write_text(json.dumps(record, sort_keys=True) + "\n")
        for key, source, decision in (
            (accepted_key, bundle_path, False),
            (accepted_key[:-4] + ".acceptance.json", body, False),
            (f"{prefix}/transactions/{tx}.json", body, True),
        ):
            result = subprocess.run(
                [
                    "aws",
                    "s3api",
                    "put-object",
                    "--bucket",
                    bucket,
                    "--key",
                    key,
                    "--body",
                    str(source),
                    "--if-none-match",
                    "*",
                    "--server-side-encryption",
                    "aws:kms",
                    "--ssekms-key-id",
                    kms_key,
                    "--cli-error-format",
                    "json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                continue
            try:
                error = json.loads(result.stderr)
            except ValueError:
                raise RuntimeError("acceptance upload failed") from None
            if error.get("Code") != "PreconditionFailed":
                raise RuntimeError("acceptance upload failed")
            existing = work / "existing"
            subprocess.run(
                ["aws", "s3api", "get-object", "--bucket", bucket, "--key", key, str(existing)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            if key.endswith(".tar"):
                if hashlib.sha256(existing.read_bytes()).hexdigest() != sha:
                    raise ValueError("accepted bundle collision")
            else:
                value = json.loads(existing.read_text())
                validate(value, manifest, bundle_sha=sha, transaction_id=tx if decision else None)
                if decision and value != record:
                    raise ValueError("transaction decision collision")
    return accepted_key


def committed_runtime(row, base=Path("/opt/fetcher")):
    labels = row["Config"].get("Labels") or {}
    transaction = labels.get("com.findb.deployment.transaction")
    if transaction is None:
        return labels.get("com.findb.fetcher.accepted") == "true"
    if not re.fullmatch(r"[0-9a-f]{64}-[0-9]+-[0-9]+", transaction):
        return False
    directory = base / "transactions" / transaction
    try:
        for path in (directory, directory / "journal.json", directory / "receipt.json"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                return False
        journal = json.loads((directory / "journal.json").read_text())
        receipt = json.loads((directory / "receipt.json").read_text())
        return (
            journal["phase"] == "committed"
            and receipt["transaction_id"] == transaction
            and receipt["schema_version"] == 4
            and receipt["phase"] == "healthy"
            and receipt["state"] == "accepted"
            and any(
                item["id"] == row["Id"] and item["image"] == row["Config"]["Image"]
                for item in journal["healthy"]
            )
        )
    except (OSError, ValueError, KeyError):
        return False


if __name__ == "__main__":
    main()
