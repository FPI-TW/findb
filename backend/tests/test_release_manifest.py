"""Tests for immutable release manifest creation and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_manifest import (
    bundle_checksum,
    contract_versions,
    create_manifest,
    migration_head,
    nginx_attestation,
    validate_manifest,
    verify_nginx_attestation,
)
from scripts.render_nginx_serve_key import referer_regex_for_host, render_serve_key

REPO_ROOT = Path(__file__).resolve().parents[2]


def _images() -> list[str]:
    return [
        "backend=ghcr.io/fpi-tw/findb@sha256:" + "a" * 64,
        "dashboard=ghcr.io/fpi-tw/findb-dashboard@sha256:" + "b" * 64,
        "nginx=nginx@sha256:" + "c" * 64,
    ]


def test_manifest_inputs_are_deterministic() -> None:
    assert migration_head(REPO_ROOT) == "c5d6e7f8a9b0"
    assert len(contract_versions(REPO_ROOT)) == 3
    assert bundle_checksum(REPO_ROOT, "findb") == bundle_checksum(REPO_ROOT, "findb")


def test_manifest_create_and_validate_round_trip(tmp_path: Path) -> None:
    manifest_path = tmp_path / "release.json"
    payload = create_manifest(
        repo_root=REPO_ROOT,
        unit="findb",
        deployment_target="staging",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        run_id="12345",
        raw_images=_images(),
    )
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    validated = validate_manifest(
        repo_root=REPO_ROOT,
        manifest_path=manifest_path,
        expected_unit="findb",
        expected_target="staging",
        expected_commit="0123456789abcdef0123456789abcdef01234567",
        expected_images=_images(),
    )
    assert validated == payload


def test_manifest_rejects_tag_only_image(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="LABEL=NAME"):
        create_manifest(
            repo_root=REPO_ROOT,
            unit="findb",
            deployment_target="staging",
            commit_sha="0123456789abcdef0123456789abcdef01234567",
            run_id="12345",
            raw_images=["backend=ghcr.io/fpi-tw/findb:01234567"],
        )


def test_manifest_rejects_stale_bundle(tmp_path: Path) -> None:
    manifest_path = tmp_path / "release.json"
    payload = create_manifest(
        repo_root=REPO_ROOT,
        unit="findb",
        deployment_target="staging",
        commit_sha="0123456789abcdef0123456789abcdef01234567",
        run_id="12345",
        raw_images=_images(),
    )
    payload["deployment_bundle_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="bundle checksum"):
        validate_manifest(repo_root=REPO_ROOT, manifest_path=manifest_path)


def _write_nginx_candidate(
    root: Path, *, host: str = "staging.example.com", key: str = "secret"
) -> None:
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
        render_serve_key(key, referer_regex_for_host(host)),
        encoding="utf-8",
    )
    for path in root.iterdir():
        path.chmod(0o600 if path.name == "serve-key.conf" else 0o644)


def test_nginx_attestation_binds_policy_without_secret_comparison_material(tmp_path: Path) -> None:
    nginx_root = tmp_path / "nginx"
    nginx_root.mkdir()
    _write_nginx_candidate(nginx_root, key="secret-value")

    attestation = nginx_attestation(
        nginx_root,
        public_host="staging.example.com",
        source_allowlist_cidrs="10.0.0.0/8",
    )
    serialized = json.dumps(attestation, sort_keys=True)
    assert "secret-value" not in serialized
    assert attestation["secret_policy"]["raw_value_digest_recorded"] is False
    assert attestation["security_policy"] == {
        "public_host": "staging.example.com",
        "source_allowlist_cidrs": ["10.0.0.0/8", "127.0.0.1/32", "::1/128"],
        "cloudflare_cidrs": ["1.2.3.0/24"],
        "serve_key_injection": {
            "present": True,
            "referer_regex": "^https?://staging\\.example\\.com/(?:instrument-lookup(?:[/?#]|$)|dashboard/lookup(?:[/?#]|$))",
            "header_binding": True,
            "include_binding": True,
            "mode": "0600",
        },
    }
    verify_nginx_attestation(nginx_root, attestation)

    # The value is deliberately excluded from the attestation.  A rotated
    # Serve key must not turn this artifact into a reusable comparison oracle.
    _write_nginx_candidate(nginx_root, key="another-secret")
    verify_nginx_attestation(nginx_root, attestation)


def test_nginx_attestation_rejects_changed_non_secret_policy(tmp_path: Path) -> None:
    nginx_root = tmp_path / "nginx"
    nginx_root.mkdir()
    _write_nginx_candidate(nginx_root)
    attestation = nginx_attestation(nginx_root, public_host="staging.example.com")
    _write_nginx_candidate(nginx_root, host="evil.example.com")
    with pytest.raises(ValueError, match="do not match"):
        verify_nginx_attestation(nginx_root, attestation)
