"""Tests for bounded, secret-free target acceptance evidence."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

import scripts.target_acceptance as acceptance
from scripts.release_manifest import nginx_attestation
from scripts.render_nginx_serve_key import referer_regex_for_host, render_serve_key

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BACKEND_IMAGE = "ghcr.io/fpi-tw/findb@sha256:" + "a" * 64
DASHBOARD_IMAGE = "ghcr.io/fpi-tw/findb-dashboard@sha256:" + "b" * 64
NGINX_IMAGE = "nginx@sha256:" + "c" * 64
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://findb:findb@localhost:5435/findb_test",
)


def _manifest(path: Path, *, target_artifacts: dict[str, object] | None = None) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "deployment_target": "staging",
        "deployment_unit": "findb",
        "commit_sha": COMMIT,
        "images": {
            "backend": {"name": "ghcr.io/fpi-tw/findb", "digest": "sha256:" + "a" * 64},
            "dashboard": {
                "name": "ghcr.io/fpi-tw/findb-dashboard",
                "digest": "sha256:" + "b" * 64,
            },
            "nginx": {"name": "nginx", "digest": "sha256:" + "c" * 64},
        },
        "migration_revision": "c5d6e7f8a9b0",
        "contract_versions": [
            {
                "path": "contracts/example.json",
                "schema_id": "findb.example",
                "schema_version": 1,
                "sha256": "c" * 64,
            }
        ],
        "deployment_bundle_sha256": "d" * 64,
        "created_by_run_id": "12345",
    }
    if target_artifacts is not None:
        payload["target_artifacts"] = target_artifacts
        payload["source_bundle_sha256"] = "e" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")


def _nginx_candidate(root: Path) -> None:
    host = "staging.example.com"
    root.mkdir()
    (root / "nginx.conf").write_text(
        "include /etc/nginx/serve-key.conf;\n"
        f"server {{\n    server_name {host};\n}}\n"
        f"server {{\n    server_name {host};\n"
        "    proxy_set_header X-API-Key $findb_serve_proxy_key;\n}\n",
        encoding="utf-8",
    )
    (root / "source-allowlist.conf").write_text(
        "allow 127.0.0.1/32;\nallow ::1/128;\nallow 10.0.0.0/8;\ndeny all;\n",
        encoding="utf-8",
    )
    (root / "cloudflare-real-ip.conf").write_text(
        "set_real_ip_from 1.2.3.0/24;\n",
        encoding="utf-8",
    )
    (root / "serve-key.conf").write_text(
        render_serve_key("runtime-secret", referer_regex_for_host(host)),
        encoding="utf-8",
    )
    for artifact in root.iterdir():
        artifact.chmod(0o600 if artifact.name == "serve-key.conf" else 0o644)


@pytest.mark.asyncio
async def test_database_acceptance_mutates_one_row_and_rolls_back(
    test_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with test_engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) NOT NULL)")
        )
        await connection.execute(text("DELETE FROM alembic_version"))
        await connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": "c5d6e7f8a9b0"},
        )
        await connection.execute(
            text(
                "INSERT INTO dataset_registry "
                "(dataset_key, name, asset_class, market, frequency, is_active, "
                "config, created_at, updated_at) "
                "VALUES ('acceptance_probe', 'Acceptance probe', 'equity', 'US', "
                "'daily', true, '{}'::jsonb, :created_at, :updated_at)"
            ),
            {"created_at": original_updated_at, "updated_at": original_updated_at},
        )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    checks = await acceptance._database_checks("c5d6e7f8a9b0")

    async with test_engine.connect() as connection:
        persisted_updated_at = await connection.scalar(
            text("SELECT updated_at FROM dataset_registry WHERE dataset_key = 'acceptance_probe'")
        )
    assert persisted_updated_at == original_updated_at
    assert checks[-1] == {
        "name": "rds_transaction",
        "status": "passed",
        "detail": "one-row UPDATE write smoke; explicit rollback",
    }


@pytest.mark.asyncio
async def test_acceptance_records_manifest_identity_and_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)

    async def fake_database_checks(revision: str) -> list[dict[str, str]]:
        assert revision == "c5d6e7f8a9b0"
        return [{"name": "rds_revision", "status": "passed", "detail": revision}]

    def fake_http_check(name: str, url: str) -> dict[str, str]:
        return {"name": name, "status": "passed", "detail": url}

    monkeypatch.setattr(acceptance, "_database_checks", fake_database_checks)
    monkeypatch.setattr(acceptance, "_http_check", fake_http_check)

    evidence = await acceptance.run_acceptance(
        manifest_path=manifest,
        target="staging",
        health_urls=[("serve", "http://serve:8080/health")],
        expected_images=[
            f"backend={BACKEND_IMAGE}",
            f"dashboard={DASHBOARD_IMAGE}",
            f"nginx={NGINX_IMAGE}",
        ],
        expected_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )

    assert evidence["commit_sha"] == COMMIT
    assert len(evidence["manifest_sha256"]) == 64
    assert evidence["migration_revision"] == "c5d6e7f8a9b0"
    assert evidence["images"]["backend"]["digest"] == "sha256:" + "a" * 64
    assert evidence["checks"] == [
        {"name": "rds_revision", "status": "passed", "detail": "c5d6e7f8a9b0"},
        {"name": "serve", "status": "passed", "detail": "http://serve:8080/health"},
    ]


@pytest.mark.asyncio
async def test_acceptance_verifies_transferred_nginx_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nginx_root = tmp_path / "nginx"
    _nginx_candidate(nginx_root)
    attestation = nginx_attestation(
        nginx_root,
        public_host="staging.example.com",
        source_allowlist_cidrs="10.0.0.0/8",
    )
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, target_artifacts=attestation)
    transferred = tmp_path / "nginx-attestation.json"
    transferred.write_text(json.dumps(attestation), encoding="utf-8")

    async def fake_database_checks(revision: str) -> list[dict[str, str]]:
        return [{"name": "rds_revision", "status": "passed", "detail": revision}]

    monkeypatch.setattr(acceptance, "_database_checks", fake_database_checks)

    evidence = await acceptance.run_acceptance(
        manifest_path=manifest,
        target="staging",
        health_urls=[],
        expected_images=[
            f"backend={BACKEND_IMAGE}",
            f"dashboard={DASHBOARD_IMAGE}",
            f"nginx={NGINX_IMAGE}",
        ],
        nginx_root=nginx_root,
        nginx_attestation_path=transferred,
    )

    assert evidence["target_attestation_sha256"] == attestation["attestation_sha256"]
    assert evidence["checks"][-1] == {
        "name": "nginx_bundle_attestation",
        "status": "passed",
        "detail": attestation["attestation_sha256"],
    }


@pytest.mark.asyncio
async def test_acceptance_rejects_truncated_or_changed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)
    expected_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload.pop("contract_versions")
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    async def fake_database_checks(revision: str) -> list[dict[str, str]]:
        raise AssertionError(f"database must not be reached for {revision}")

    monkeypatch.setattr(acceptance, "_database_checks", fake_database_checks)

    with pytest.raises(ValueError, match="SHA-256 does not match"):
        await acceptance.run_acceptance(
            manifest_path=manifest,
            target="staging",
            health_urls=[],
            expected_images=[],
            expected_manifest_sha256=expected_sha256,
        )

    with pytest.raises(ValueError, match="contract versions are missing"):
        await acceptance.run_acceptance(
            manifest_path=manifest,
            target="staging",
            health_urls=[],
            expected_images=[],
        )


@pytest.mark.parametrize(
    "expected",
    [
        ["backend=ghcr.io/fpi-tw/findb:latest"],
        [
            "backend=ghcr.io/fpi-tw/findb@sha256:" + "a" * 64,
            "backend=ghcr.io/fpi-tw/findb@sha256:" + "b" * 64,
        ],
        ["malformed"],
    ],
)
def test_expected_images_are_digest_pinned_and_unique(expected: list[str]) -> None:
    with pytest.raises(ValueError):
        acceptance._expected_image_map(expected)


def test_target_acceptance_rejects_manifest_image_mismatch(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    _manifest(manifest)

    with pytest.raises(ValueError, match="images do not match"):
        import asyncio

        asyncio.run(
            acceptance.run_acceptance(
                manifest_path=manifest,
                target="staging",
                health_urls=[],
                expected_images=["backend=ghcr.io/fpi-tw/findb@sha256:" + "c" * 64],
            )
        )
