"""Trusted operator bridge: enroll an installed runtime without provider secrets.

Deployed evidence must be copied from the verified immutable release on the
runtime host after successful activation. Local uses the installed runtime
artifact directly. This command has management DB access, never Source auth.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.schemas.full_market_readiness import ReadinessDeclaration
from app.services.full_market_admission import enroll, reconcile_environment


def read_artifact(path: Path) -> tuple[bytes, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise ValueError("management evidence must be a bounded regular file")
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def verify_installation(
    declaration: ReadinessDeclaration,
    receipt_path: Path,
    artifact_path: Path,
    manifest_path: Path | None,
    accepted_path: Path | None,
    source_key_sha256: str,
) -> str:
    raw, digest = read_artifact(receipt_path)
    receipt = json.loads(raw)
    if artifact_path.is_dir() and declaration.environment == "local":
        files = sorted(artifact_path.rglob("*.py"))
        if (
            not files
            or len(files) > 1000
            or sum(path.stat().st_size for path in files) > 20_000_000
        ):
            raise ValueError("installed source artifact exceeds bound")
        artifact_hash = hashlib.sha256()
        for path in files:
            raw, _ = read_artifact(path)
            artifact_hash.update(path.relative_to(artifact_path).as_posix().encode() + b"\0" + raw)
        artifact_digest = artifact_hash.hexdigest()
    else:
        _, artifact_digest = read_artifact(artifact_path)
    expected = {
        "source_key_sha256": source_key_sha256,
        "environment": declaration.environment,
        "runtime_id": declaration.runtime_id,
        "artifact_sha256": artifact_digest,
        "config_sha256": declaration.config_sha256,
        "governor_identity": declaration.account.governor_identity,
        "account_allocation_sha256": declaration.account_allocation_sha256,
    }
    if (
        artifact_digest != declaration.artifact_sha256
        or any(receipt.get(key) != value for key, value in expected.items())
        or set(receipt.get("consumers", [])) != set(declaration.account.consumers)
    ):
        raise ValueError("installation receipt does not bind the enrolled runtime/account")
    from datetime import datetime

    installed = datetime.fromisoformat(receipt["installed_at"])
    if installed.tzinfo is None or installed > declaration.verified_at:
        raise ValueError("readiness must be verified after actual installation")
    if declaration.environment == "local":
        if manifest_path or accepted_path or receipt.get("kind") != "local-installed":
            raise ValueError("local installation evidence invalid")
    else:
        if not manifest_path or not accepted_path:
            raise ValueError("deployed installation requires manifest and accepted record")
        manifest_raw, manifest_digest = read_artifact(manifest_path)
        accepted_raw, accepted_digest = read_artifact(accepted_path)
        manifest, accepted = json.loads(manifest_raw), json.loads(accepted_raw)
        if (
            receipt.get("kind") != "deployed-installed"
            or receipt.get("manifest_sha256") != manifest_digest
            or receipt.get("accepted_sha256") != accepted_digest
        ):
            raise ValueError("installed receipt differs from immutable deployment evidence")
        if (
            accepted.get("state") != "accepted"
            or any(
                record.get("deployment_target") != declaration.environment
                or record.get("unit") != "fetcher"
                or record.get("runtime_profile", "bounded") != "full-market"
                for record in (manifest, accepted)
            )
            or manifest.get("commit_sha") != accepted.get("commit_sha")
            or manifest.get("images") != accepted.get("images")
            or receipt.get("bundle_sha256") != accepted.get("bundle_sha256")
            or declaration.artifact_sha256 != manifest_digest
        ):
            raise ValueError("deployment acceptance binding invalid")
    return digest


async def run(args: argparse.Namespace) -> dict:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await reconcile_environment(db, actor="full-market-management")
            result = {
                "enabled": settings.FULL_MARKET_ENABLED,
                "environment": settings.APP_ENVIRONMENT,
            }
            if args.declaration:
                declaration = ReadinessDeclaration.model_validate_json(
                    read_artifact(args.declaration)[0]
                )
                from app.models.registry import SourceClient

                client = await db.get(
                    SourceClient,
                    declaration.source_client_id,
                    with_for_update=True,
                    populate_existing=True,
                )
                if client is None:
                    raise ValueError("declared Source client unavailable")
                installation_digest = verify_installation(
                    declaration,
                    args.receipt,
                    args.artifact,
                    args.manifest,
                    args.accepted,
                    client.key_hash,
                )
                row = await enroll(db, declaration, installation_digest)
                result.update(
                    enrollment_id=str(row.enrollment_id), declaration_sha256=row.declaration_sha256
                )
            await db.commit()
            if args.declaration and args.output_dir:
                write_enrollment_files(row, args.output_dir)
            return result
    finally:
        await engine.dispose()


def write_enrollment_files(row, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{row.provider}.json").write_text(
        json.dumps(row.declaration, sort_keys=True) + "\n"
    )
    allocation = {
        key: row.declaration[key]
        for key in (
            "provider",
            "environment",
            "requests_per_day",
            "requests_per_minute",
            "requests_per_second",
            "bytes_per_day",
            "max_response_bytes",
            "source_requests_per_minute",
            "account",
        )
    }
    (output_dir / f"{row.provider}.account.json").write_text(
        json.dumps(allocation, sort_keys=True) + "\n"
    )
    (output_dir / f"{row.provider}.enrollment.json").write_text(
        json.dumps(
            {
                "enrollment_id": str(row.enrollment_id),
                "declaration_sha256": row.declaration_sha256,
                "runtime_id": row.runtime_id,
                "datasets": row.declaration["datasets"],
            },
            sort_keys=True,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("declaration", "receipt", "artifact", "manifest", "accepted"):
        parser.add_argument("--" + field, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.declaration and (not args.receipt or not args.artifact):
        parser.error("--declaration requires --receipt and --artifact")
    try:
        print(json.dumps(asyncio.run(run(args)), sort_keys=True))
        return 0
    except Exception:
        print("full_market_management=failed reason=evidence_or_binding_invalid")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
