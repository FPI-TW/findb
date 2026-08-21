"""Run bounded, secret-free target acceptance checks inside the app image."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MIGRATION_REVISION = re.compile(r"^[0-9a-f]{12}$")


def _nginx_attestation_helpers(*, validate_only: bool = False):
    """Load release-manifest helpers in both image and repository layouts."""

    try:
        from scripts import release_manifest
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from scripts import release_manifest
    return (
        release_manifest.validate_nginx_attestation
        if validate_only
        else release_manifest.verify_nginx_attestation
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("release manifest schema is unsupported")
    if payload.get("deployment_target") not in {"staging", "production"}:
        raise ValueError("release manifest target is invalid")
    if payload.get("deployment_unit") not in {"findb", "fetcher"}:
        raise ValueError("release manifest deployment unit is invalid")
    if not COMMIT_SHA.fullmatch(str(payload.get("commit_sha", ""))):
        raise ValueError("release manifest commit SHA is invalid")
    images = payload.get("images")
    if not isinstance(images, dict) or not images:
        raise ValueError("release manifest has no images")
    for label, image in images.items():
        if not isinstance(label, str) or not isinstance(image, dict):
            raise ValueError("release manifest image entry is invalid")
        reference = f"{image.get('name', '')}@{image.get('digest', '')}"
        if not DIGEST.fullmatch(reference):
            raise ValueError(f"release manifest image {label!r} is not digest-pinned")
    if not MIGRATION_REVISION.fullmatch(str(payload.get("migration_revision", ""))):
        raise ValueError("release manifest migration revision is missing")
    contracts = payload.get("contract_versions")
    if not isinstance(contracts, list) or not contracts:
        raise ValueError("release manifest contract versions are missing")
    for contract in contracts:
        if not isinstance(contract, dict) or set(contract) != {
            "path",
            "schema_id",
            "schema_version",
            "sha256",
        }:
            raise ValueError("release manifest contract entry is invalid")
        if (
            not str(contract["path"])
            or not str(contract["schema_id"])
            or not isinstance(contract["schema_version"], int)
            or contract["schema_version"] < 1
            or not SHA256.fullmatch(str(contract["sha256"]))
        ):
            raise ValueError("release manifest contract entry is invalid")
    if not SHA256.fullmatch(str(payload.get("deployment_bundle_sha256", ""))):
        raise ValueError("release manifest deployment bundle checksum is invalid")
    if not str(payload.get("created_by_run_id", "")).strip():
        raise ValueError("release manifest workflow run ID is missing")
    if "target_artifacts" in payload:
        if not SHA256.fullmatch(str(payload.get("source_bundle_sha256", ""))):
            raise ValueError("release manifest source bundle checksum is invalid")
        _nginx_attestation_helpers(validate_only=True)(payload["target_artifacts"])
    return payload


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON evidence must be an object")
    return payload


def _expected_image_map(raw_images: list[str]) -> dict[str, str]:
    expected: dict[str, str] = {}
    for raw in raw_images:
        try:
            label, reference = raw.split("=", 1)
        except ValueError as exc:
            raise ValueError(f"expected image must be LABEL=NAME@sha256:DIGEST: {raw}") from exc
        if not label:
            raise ValueError(f"expected image label is empty: {raw}")
        if not DIGEST.fullmatch(reference):
            raise ValueError(f"expected image {label!r} is not digest-pinned")
        if label in expected:
            raise ValueError(f"duplicate expected image label: {label}")
        expected[label] = reference
    return expected


async def _database_checks(expected_revision: str) -> list[dict[str, str]]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required for target acceptance")
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                revision = str(
                    await connection.scalar(text("SELECT version_num FROM alembic_version"))
                )
                dataset_key = await connection.scalar(
                    text(
                        "SELECT dataset_key FROM dataset_registry "
                        "ORDER BY dataset_key LIMIT 1 FOR UPDATE"
                    )
                )
                if dataset_key is None:
                    raise ValueError("dataset registry is empty during write-path acceptance")
                updated_key = await connection.scalar(
                    text(
                        "UPDATE dataset_registry "
                        "SET updated_at = updated_at + interval '1 microsecond' "
                        "WHERE dataset_key = :dataset_key RETURNING dataset_key"
                    ),
                    {"dataset_key": dataset_key},
                )
                if updated_key != dataset_key:
                    raise ValueError("database write-path acceptance did not update one row")
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
    if revision != expected_revision:
        raise ValueError(
            f"database migration revision mismatch: expected {expected_revision}, got {revision}"
        )
    return [
        {"name": "rds_revision", "status": "passed", "detail": revision},
        {
            "name": "rds_transaction",
            "status": "passed",
            "detail": "one-row UPDATE write smoke; explicit rollback",
        },
    ]


def _http_check(name: str, url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} health URL must not contain credentials, query, or fragment")
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - target-local URL.
        if response.status < 200 or response.status >= 400:
            raise ValueError(f"{name} returned HTTP {response.status}")
    return {"name": name, "status": "passed", "detail": url}


async def run_acceptance(
    *,
    manifest_path: Path,
    target: str,
    health_urls: list[tuple[str, str]],
    expected_images: list[str],
    expected_manifest_sha256: str | None = None,
    nginx_root: Path | None = None,
    nginx_attestation_path: Path | None = None,
) -> dict[str, Any]:
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if expected_manifest_sha256 is not None:
        if not SHA256.fullmatch(expected_manifest_sha256):
            raise ValueError("expected release manifest SHA-256 is invalid")
        if manifest_sha256 != expected_manifest_sha256:
            raise ValueError("transferred release manifest SHA-256 does not match")
    manifest = _load_manifest(manifest_path)
    if manifest["deployment_target"] != target:
        raise ValueError("release manifest target does not match deployment target")
    actual_images = {
        label: f"{image['name']}@{image['digest']}" for label, image in manifest["images"].items()
    }
    expected = _expected_image_map(expected_images)
    if expected and actual_images != expected:
        raise ValueError("release manifest images do not match target image references")

    checks = await _database_checks(str(manifest["migration_revision"]))
    target_artifacts = manifest.get("target_artifacts")
    if target_artifacts is not None:
        if nginx_root is None:
            raise ValueError("nginx root is required when release manifest has target artifacts")
        if nginx_attestation_path is not None:
            transferred_attestation = _load_json_object(nginx_attestation_path)
            if transferred_attestation != target_artifacts:
                raise ValueError("transferred nginx attestation does not match the manifest")
        verify_nginx_attestation = _nginx_attestation_helpers()
        verify_nginx_attestation(nginx_root, target_artifacts)
        checks.append(
            {
                "name": "nginx_bundle_attestation",
                "status": "passed",
                "detail": str(target_artifacts["attestation_sha256"]),
            }
        )
    elif nginx_root is not None:
        raise ValueError("release manifest has no nginx target attestation")
    elif nginx_attestation_path is not None:
        raise ValueError("release manifest has no nginx target attestation")
    for name, url in health_urls:
        checks.append(_http_check(name, url))
    evidence = {
        "schema_version": 1,
        "deployment_target": target,
        "deployment_unit": manifest["deployment_unit"],
        "commit_sha": manifest["commit_sha"],
        "manifest_sha256": manifest_sha256,
        "migration_revision": manifest["migration_revision"],
        "images": manifest["images"],
        "checks": checks,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }
    if target_artifacts is not None:
        # The attestation contains only normalized secret-free policy.  Keep
        # the binding in evidence so a later reviewer can prove which bundle
        # was accepted without receiving the Serve key.
        evidence["target_attestation_sha256"] = target_artifacts["attestation_sha256"]
    return evidence


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target", required=True, choices=("staging", "production"))
    parser.add_argument(
        "--health-url", action="append", nargs=2, metavar=("NAME", "URL"), default=[]
    )
    parser.add_argument("--expected-image", action="append", default=[])
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--nginx-root", type=Path)
    parser.add_argument("--nginx-attestation", type=Path)
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    try:
        evidence = await run_acceptance(
            manifest_path=args.manifest,
            target=args.target,
            health_urls=[(str(name), str(url)) for name, url in args.health_url],
            expected_images=args.expected_image,
            expected_manifest_sha256=args.expected_manifest_sha256,
            nginx_root=args.nginx_root,
            nginx_attestation_path=args.nginx_attestation,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"target acceptance: ok ({args.target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
