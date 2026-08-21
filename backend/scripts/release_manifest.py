"""Create and validate immutable deployment release manifests.

The persisted manifest contains only public release metadata.  Target
attestation may inspect rendered configuration, but it normalizes secret values
before hashing or writing any evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import stat
from pathlib import Path
from typing import Any

HEX_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
MIGRATION_REVISION = re.compile(r'^revision:\s*(?:str\s*=\s*)?["\']([0-9a-f]+)["\']', re.MULTILINE)
MIGRATION_DOWN_REVISION = re.compile(
    r'^down_revision:.*?=\s*(?:["\']([0-9a-f]+)["\']|None)', re.MULTILINE
)

DEFAULT_BUNDLE_FILES = {
    "findb": (
        "docker-compose.prod.yml",
        "infra/nginx/nginx.conf",
        "infra/nginx/source-allowlist.conf",
        "infra/nginx/cloudflare-real-ip.conf",
        "infra/nginx/serve-key.conf",
        "backend/scripts/render_nginx_public_host.py",
        "backend/scripts/render_nginx_source_allowlist.py",
        "backend/scripts/render_nginx_cloudflare_real_ip.py",
        "backend/scripts/render_nginx_serve_key.py",
        "backend/scripts/release_manifest.py",
        "backend/scripts/target_acceptance.py",
    ),
    "fetcher": (
        "fetcher/Dockerfile",
        "fetcher/Dockerfile.finlab",
        "fetcher/Dockerfile.shioaji",
        "fetcher/pyproject.toml",
        "fetcher/uv.lock",
        "fetcher/configs/daily_scheduler.v2.json",
        "fetcher/configs/shioaji_tw_pilot.v1.json",
    ),
}

DEFAULT_NGINX_ARTIFACTS = (
    "nginx.conf",
    "source-allowlist.conf",
    "cloudflare-real-ip.conf",
    "serve-key.conf",
)
DEFAULT_SECRET_NGINX_ARTIFACTS = ("serve-key.conf",)
ATTESTATION_SCHEMA_VERSION = 1
NGINX_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)
NGINX_MODE_PATTERN = re.compile(r"0[0-7]{3}")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON artifact {path}: {exc}") from exc


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"nginx artifact path must be relative: {relative}")
    return path


def _normalize_host(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if not NGINX_HOST_PATTERN.fullmatch(host):
        raise ValueError("public host must be a valid DNS hostname")
    return host


def _canonical_cidrs(raw_value: str) -> list[str]:
    values: list[str] = []
    for raw in raw_value.replace(",", "\n").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            values.append(str(ipaddress.ip_network(value, strict=False)))
    return sorted(set(values))


def _parse_directive_values(content: str, directive: str) -> list[str]:
    pattern = re.compile(rf"(?m)^\s*{re.escape(directive)}\s+([^;\s]+);\s*$")
    return sorted(set(match.group(1) for match in pattern.finditer(content)))


def _rendered_public_host(content: str) -> str:
    hosts = [
        match.group(1).lower().rstrip(".")
        for match in re.finditer(r"(?m)^\s*server_name\s+([^;\s]+);", content)
    ]
    if len(hosts) != 2 or len(set(hosts)) != 1:
        raise ValueError("nginx bundle must bind one public host in both server blocks")
    return _normalize_host(hosts[0])


def _expected_referer_regex(public_host: str) -> str:
    host = _normalize_host(public_host)
    return (
        rf"^https?://{re.escape(host)}/"
        r"(?:instrument-lookup(?:[/?#]|$)|dashboard/lookup(?:[/?#]|$))"
    )


def _serve_key_policy(content: str, public_host: str) -> dict[str, object]:
    """Extract non-secret serve-key policy without retaining the key value."""

    entry_pattern = re.compile(r'(?m)^\s*"(?P<regex>~[^"\n]+)"\s+"(?P<value>[^"\n]*)";\s*$')
    entries = list(entry_pattern.finditer(content))
    if len(entries) > 1:
        raise ValueError("serve-key.conf has multiple credential injection entries")
    expected_regex = _expected_referer_regex(public_host)
    if entries:
        referer_regex = entries[0].group("regex")[1:]
        if referer_regex != expected_regex:
            raise ValueError("serve-key.conf referer scope does not match public host")
        present = bool(entries[0].group("value"))
    else:
        referer_regex = None
        present = False
    return {
        "present": present,
        "referer_regex": referer_regex,
        "header_binding": "proxy_set_header X-API-Key $findb_serve_proxy_key;" in content,
    }


def _redact_nginx_secret(relative: str, content: bytes) -> bytes:
    """Normalize the injected Serve key before hashing the rendered artifact.

    The raw key, its length, and a digest of the raw key are deliberately
    excluded.  The renderer rejects quotes, backslashes, and newlines, so a
    single quoted map value can be replaced with a fixed marker.  Hashing this
    normalized representation binds the non-secret map structure without
    creating a credential comparison oracle.
    """

    if relative != "serve-key.conf":
        return content
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"nginx artifact is not UTF-8: {relative}") from exc

    value_pattern = re.compile(
        r'(?m)^(?P<prefix>\s*"~[^"\n]+"\s+")(?P<value>[^"\n]*)(?P<suffix>";\s*)$'
    )
    redacted, replacements = value_pattern.subn(r"\g<prefix><redacted>\g<suffix>", text)
    if replacements > 1:
        raise ValueError(f"nginx secret artifact has unexpected credential entries: {relative}")
    return redacted.encode("utf-8")


def nginx_attestation(
    root: Path,
    *,
    artifacts: tuple[str, ...] = DEFAULT_NGINX_ARTIFACTS,
    secret_artifacts: tuple[str, ...] = DEFAULT_SECRET_NGINX_ARTIFACTS,
    public_host: str | None = None,
    source_allowlist_cidrs: str | None = None,
) -> dict[str, Any]:
    """Describe target-rendered nginx outputs without exposing secrets.

    The attestation is target-specific: it binds the rendered public host,
    Source allowlist, exact Cloudflare CIDRs, and Serve-key injection policy in
    addition to the transferred file bytes and modes.  Only the Serve-key
    value is normalized; all other policy is recorded verbatim/canonically.
    """

    if not artifacts:
        raise ValueError("at least one nginx artifact is required")
    secret_set = set(secret_artifacts)
    entries: list[dict[str, object]] = []
    for relative in artifacts:
        path = _safe_relative_path(relative)
        artifact_path = root / path
        if not artifact_path.is_file():
            raise ValueError(f"nginx artifact does not exist: {relative}")
        content = artifact_path.read_bytes()
        mode = f"{stat.S_IMODE(artifact_path.stat().st_mode):04o}"
        if relative in secret_set:
            redacted = _redact_nginx_secret(relative, content)
            entries.append(
                {
                    "path": relative,
                    "sha256": _sha256_bytes(redacted),
                    "secret_redacted": True,
                    "mode": mode,
                }
            )
        else:
            entries.append(
                {
                    "path": relative,
                    "sha256": _sha256_bytes(content),
                    "size": len(content),
                    "secret_redacted": False,
                    "mode": mode,
                }
            )
    entries.sort(key=lambda item: str(item["path"]))
    if set(artifacts) >= {"nginx.conf", "source-allowlist.conf", "cloudflare-real-ip.conf"}:
        nginx_text = (root / "nginx.conf").read_text(encoding="utf-8")
        public_host_value = _rendered_public_host(nginx_text)
        if public_host is not None and public_host_value != _normalize_host(public_host):
            raise ValueError("rendered nginx public host does not match target policy")
        source_text = (root / "source-allowlist.conf").read_text(encoding="utf-8")
        source_cidrs = _parse_directive_values(source_text, "allow")
        if "deny all;" not in source_text:
            raise ValueError("source allowlist must deny all non-allowlisted clients")
        if source_allowlist_cidrs is not None:
            expected_source = sorted(
                set(("127.0.0.1/32", "::1/128")) | set(_canonical_cidrs(source_allowlist_cidrs))
            )
            if source_cidrs != expected_source:
                raise ValueError("rendered source allowlist does not match target policy")
        cloudflare_text = (root / "cloudflare-real-ip.conf").read_text(encoding="utf-8")
        cloudflare_cidrs = _parse_directive_values(cloudflare_text, "set_real_ip_from")
        serve_path = root / "serve-key.conf"
        serve_text = serve_path.read_text(encoding="utf-8") if serve_path.is_file() else ""
        serve_policy = _serve_key_policy(serve_text, public_host_value)
        serve_policy["header_binding"] = (
            "proxy_set_header X-API-Key $findb_serve_proxy_key;" in nginx_text
        )
        serve_policy["include_binding"] = "include /etc/nginx/serve-key.conf;" in nginx_text
        serve_policy["mode"] = (
            f"{stat.S_IMODE(serve_path.stat().st_mode):04o}" if serve_path.is_file() else None
        )
    else:
        public_host_value = None
        source_cidrs = []
        cloudflare_cidrs = []
        serve_policy = {
            "present": False,
            "referer_regex": None,
            "header_binding": False,
            "include_binding": False,
            "mode": None,
        }
    payload: dict[str, Any] = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "kind": "target-rendered-nginx",
        "artifacts": entries,
        "transferred_files": sorted(str(entry["path"]) for entry in entries),
        "security_policy": {
            "public_host": public_host_value,
            "source_allowlist_cidrs": source_cidrs,
            "cloudflare_cidrs": cloudflare_cidrs,
            "serve_key_injection": serve_policy,
        },
        "secret_policy": {
            "excluded_material": ["FINDB_LOOKUP_SERVE_API_KEY"],
            "normalized_fields": [
                {
                    "path": "serve-key.conf",
                    "field": "injected_map_value",
                    "replacement": "<redacted>",
                }
            ],
            "raw_value_digest_recorded": False,
            "raw_value_length_recorded": False,
        },
    }
    payload["attestation_sha256"] = _sha256_bytes(_canonical_json(payload))
    return payload


def validate_nginx_attestation(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("nginx attestation must be a JSON object")
    if payload.get("schema_version") != ATTESTATION_SCHEMA_VERSION:
        raise ValueError("unsupported nginx attestation schema")
    if payload.get("kind") != "target-rendered-nginx":
        raise ValueError("invalid nginx attestation kind")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("nginx attestation must contain artifacts")
    transferred_files = payload.get("transferred_files")
    if not isinstance(transferred_files, list) or not all(
        isinstance(value, str) for value in transferred_files
    ):
        raise ValueError("nginx attestation must declare transferred files")
    paths: set[str] = set()
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise ValueError("invalid nginx attestation artifact")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative in paths
            or not re.fullmatch(r"[0-9a-f]{64}", str(digest))
        ):
            raise ValueError("invalid nginx attestation artifact path or digest")
        if not isinstance(entry.get("secret_redacted"), bool):
            raise ValueError("nginx attestation artifact must declare secret redaction")
        if relative in DEFAULT_SECRET_NGINX_ARTIFACTS and not entry["secret_redacted"]:
            raise ValueError("secret nginx artifact must use fixed normalization")
        if relative not in DEFAULT_SECRET_NGINX_ARTIFACTS and entry["secret_redacted"]:
            raise ValueError("non-secret nginx artifact must not use secret normalization")
        if entry["secret_redacted"] and "size" in entry:
            raise ValueError("secret nginx artifact must not record its raw size")
        if not entry["secret_redacted"] and (
            not isinstance(entry.get("size"), int) or entry["size"] < 0
        ):
            raise ValueError("non-secret nginx artifact must declare a non-negative size")
        if not isinstance(entry.get("mode"), str) or not NGINX_MODE_PATTERN.fullmatch(
            entry["mode"]
        ):
            raise ValueError("nginx attestation artifact must declare a valid mode")
        paths.add(relative)
    if sorted(paths) != sorted(transferred_files):
        raise ValueError("nginx attestation transferred file list is inconsistent")
    policy = payload.get("security_policy")
    if not isinstance(policy, dict):
        raise ValueError("nginx attestation security policy is missing")
    for key in ("public_host", "source_allowlist_cidrs", "cloudflare_cidrs", "serve_key_injection"):
        if key not in policy:
            raise ValueError("nginx attestation security policy is incomplete")
    public_host = policy["public_host"]
    if public_host is not None:
        _normalize_host(str(public_host))
    for key in ("source_allowlist_cidrs", "cloudflare_cidrs"):
        values = policy[key]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("nginx attestation CIDR policy is invalid")
        try:
            canonical = sorted(
                set(str(ipaddress.ip_network(value, strict=False)) for value in values)
            )
        except ValueError as exc:
            raise ValueError("nginx attestation CIDR policy is invalid") from exc
        if values != canonical:
            raise ValueError("nginx attestation CIDR policy is not canonical")
    serve_policy = policy["serve_key_injection"]
    if not isinstance(serve_policy, dict) or set(serve_policy) != {
        "present",
        "referer_regex",
        "header_binding",
        "include_binding",
        "mode",
    }:
        raise ValueError("nginx attestation Serve-key policy is invalid")
    if (
        not isinstance(serve_policy["present"], bool)
        or not isinstance(serve_policy["header_binding"], bool)
        or not isinstance(serve_policy["include_binding"], bool)
    ):
        raise ValueError("nginx attestation Serve-key policy is invalid")
    if serve_policy["referer_regex"] is not None and not isinstance(
        serve_policy["referer_regex"], str
    ):
        raise ValueError("nginx attestation Serve-key referer policy is invalid")
    if serve_policy["mode"] is not None and (
        not isinstance(serve_policy["mode"], str)
        or not NGINX_MODE_PATTERN.fullmatch(serve_policy["mode"])
    ):
        raise ValueError("nginx attestation Serve-key mode is invalid")
    secret_policy = payload.get("secret_policy")
    if not isinstance(secret_policy, dict):
        raise ValueError("nginx attestation secret policy is missing")
    expected_secret_policy = {
        "excluded_material": ["FINDB_LOOKUP_SERVE_API_KEY"],
        "normalized_fields": [
            {
                "path": "serve-key.conf",
                "field": "injected_map_value",
                "replacement": "<redacted>",
            }
        ],
        "raw_value_digest_recorded": False,
        "raw_value_length_recorded": False,
    }
    if secret_policy != expected_secret_policy:
        raise ValueError("nginx attestation secret policy is not the fixed secret-free contract")
    expected_digest = _sha256_bytes(
        _canonical_json(
            {key: value for key, value in payload.items() if key != "attestation_sha256"}
        )
    )
    if payload.get("attestation_sha256") != expected_digest:
        raise ValueError("nginx attestation checksum is invalid")
    return payload


def verify_nginx_attestation(root: Path, payload: object) -> dict[str, Any]:
    """Verify an attestation against the exact files transferred to a target.

    Failure messages intentionally do not include file digests or file
    contents.  In particular, the Serve key is compared only after the same
    fixed normalization used during attestation generation.
    """

    attestation = validate_nginx_attestation(payload)
    paths = [str(value["path"]) for value in attestation["artifacts"]]
    expected_paths = {str(path) for path in DEFAULT_NGINX_ARTIFACTS}
    if set(paths) != expected_paths:
        raise ValueError("nginx attestation must cover the complete rendered bundle")
    actual_paths = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    if actual_paths != expected_paths:
        raise ValueError("transferred nginx directory contains an unexpected file set")
    calculated = nginx_attestation(root, artifacts=tuple(sorted(expected_paths)))
    if _canonical_json(
        {key: value for key, value in calculated.items() if key != "attestation_sha256"}
    ) != _canonical_json(
        {key: value for key, value in attestation.items() if key != "attestation_sha256"}
    ):
        raise ValueError("transferred nginx files do not match their attestation")
    return attestation


def migration_head(repo_root: Path) -> str:
    revisions: set[str] = set()
    parents: set[str] = set()
    versions = repo_root / "backend" / "migrations" / "versions"
    for path in sorted(versions.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = MIGRATION_REVISION.search(source)
        if revision is None:
            continue
        revisions.add(revision.group(1))
        for parent in MIGRATION_DOWN_REVISION.findall(source):
            if parent:
                parents.add(parent)
    heads = sorted(revisions - parents)
    if len(heads) != 1:
        raise ValueError(f"expected exactly one migration head, found {heads}")
    return heads[0]


def contract_versions(repo_root: Path) -> list[dict[str, object]]:
    contracts: list[dict[str, object]] = []
    for manifest_path in (
        repo_root / "contracts" / "manifest.json",
        repo_root / "contracts" / "archive" / "manifest.json",
    ):
        payload = _read_json(manifest_path)
        if not isinstance(payload, dict) or not isinstance(payload.get("contracts"), list):
            raise ValueError(f"invalid contract manifest: {manifest_path}")
        for entry in payload["contracts"]:
            if not isinstance(entry, dict):
                raise ValueError(f"invalid contract entry: {manifest_path}")
            required = ("path", "schema_id", "schema_version", "sha256")
            if any(key not in entry for key in required):
                raise ValueError(f"contract entry missing field: {manifest_path}")
            digest = str(entry["sha256"])
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"invalid contract digest in {manifest_path}")
            contracts.append({key: entry[key] for key in required})
    return sorted(contracts, key=lambda item: str(item["path"]))


def bundle_checksum(
    repo_root: Path,
    unit: str,
    *,
    target_attestation: dict[str, Any] | None = None,
) -> str:
    files = DEFAULT_BUNDLE_FILES.get(unit)
    if files is None:
        raise ValueError(f"unsupported deployment unit: {unit}")
    digest = hashlib.sha256()
    for relative in files:
        path = repo_root / relative
        if not path.is_file():
            raise ValueError(f"release bundle file does not exist: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    if target_attestation is not None:
        validate_nginx_attestation(target_attestation)
        digest.update(b"target-rendered-nginx-attestation\0")
        digest.update(_canonical_json(target_attestation))
        digest.update(b"\0")
    return digest.hexdigest()


def _parse_images(raw_images: list[str]) -> dict[str, dict[str, str]]:
    images: dict[str, dict[str, str]] = {}
    for raw in raw_images:
        try:
            label, reference = raw.split("=", 1)
            name, digest = reference.rsplit("@", 1)
        except ValueError as exc:
            raise ValueError(f"image must be LABEL=NAME@sha256:DIGEST: {raw}") from exc
        if not label or not name or not HEX_SHA256.fullmatch(digest):
            raise ValueError(f"image must be LABEL=NAME@sha256:DIGEST: {raw}")
        if label in images:
            raise ValueError(f"duplicate image label: {label}")
        images[label] = {"name": name, "digest": digest}
    if not images:
        raise ValueError("at least one image is required")
    return dict(sorted(images.items()))


def create_manifest(
    *,
    repo_root: Path,
    unit: str,
    deployment_target: str,
    commit_sha: str,
    run_id: str,
    raw_images: list[str],
) -> dict[str, object]:
    if not COMMIT_SHA.fullmatch(commit_sha):
        raise ValueError("commit SHA must be a 40-character lowercase hexadecimal value")
    if not deployment_target or deployment_target not in {"staging", "production"}:
        raise ValueError("deployment target must be staging or production")
    if not run_id:
        raise ValueError("workflow run ID is required")
    return {
        "schema_version": 1,
        "deployment_target": deployment_target,
        "deployment_unit": unit,
        "commit_sha": commit_sha,
        "images": _parse_images(raw_images),
        "migration_revision": migration_head(repo_root),
        "contract_versions": contract_versions(repo_root),
        "deployment_bundle_sha256": bundle_checksum(repo_root, unit),
        "created_by_run_id": run_id,
    }


def validate_manifest(
    *,
    repo_root: Path,
    manifest_path: Path,
    expected_unit: str | None = None,
    expected_target: str | None = None,
    expected_commit: str | None = None,
    expected_images: list[str] | None = None,
) -> dict[str, object]:
    payload = _read_json(manifest_path)
    if not isinstance(payload, dict):
        raise ValueError("release manifest must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported release manifest schema")
    unit = payload.get("deployment_unit")
    if not isinstance(unit, str) or unit not in DEFAULT_BUNDLE_FILES:
        raise ValueError("release manifest has unsupported deployment unit")
    if expected_unit and unit != expected_unit:
        raise ValueError(f"release manifest unit mismatch: expected {expected_unit}, got {unit}")
    target = payload.get("deployment_target")
    if target not in {"staging", "production"}:
        raise ValueError("release manifest has invalid deployment target")
    if expected_target and target != expected_target:
        raise ValueError(
            f"release manifest target mismatch: expected {expected_target}, got {target}"
        )
    commit = payload.get("commit_sha")
    if not isinstance(commit, str) or not COMMIT_SHA.fullmatch(commit):
        raise ValueError("release manifest has invalid commit SHA")
    if expected_commit and commit != expected_commit:
        raise ValueError("release manifest commit does not match the deployment revision")
    raw_images = payload.get("images")
    if not isinstance(raw_images, dict) or not raw_images:
        raise ValueError("release manifest must contain images")
    for label, image in raw_images.items():
        if not isinstance(label, str) or not isinstance(image, dict):
            raise ValueError("release manifest image entry is invalid")
        if not isinstance(image.get("name"), str) or not HEX_SHA256.fullmatch(
            str(image.get("digest", ""))
        ):
            raise ValueError(f"release manifest image {label!r} is not digest-pinned")
    if expected_images:
        expected = _parse_images(expected_images)
        actual = {
            label: {"name": image["name"], "digest": image["digest"]}
            for label, image in raw_images.items()
        }
        if actual != expected:
            raise ValueError("release manifest image references do not match build outputs")
    if payload.get("migration_revision") != migration_head(repo_root):
        raise ValueError("release manifest migration revision is not the current head")
    if payload.get("contract_versions") != contract_versions(repo_root):
        raise ValueError("release manifest contract versions are stale")
    target_attestation = payload.get("target_artifacts")
    source_bundle_sha256 = payload.get("source_bundle_sha256")
    if target_attestation is not None:
        if not isinstance(target_attestation, dict):
            raise ValueError("release manifest target artifacts are invalid")
        validate_nginx_attestation(target_attestation)
        if source_bundle_sha256 != bundle_checksum(repo_root, unit):
            raise ValueError("release manifest source bundle checksum is stale")
        expected_bundle = bundle_checksum(repo_root, unit, target_attestation=target_attestation)
    else:
        expected_bundle = bundle_checksum(repo_root, unit)
    if payload.get("deployment_bundle_sha256") != expected_bundle:
        raise ValueError("release manifest deployment bundle checksum is stale")
    if not str(payload.get("created_by_run_id", "")):
        raise ValueError("release manifest is missing workflow run ID")
    return payload


def bind_target_attestation(
    *,
    repo_root: Path,
    manifest_path: Path,
    attestation_path: Path,
    output_path: Path,
) -> dict[str, object]:
    """Bind post-render target evidence to a candidate manifest.

    The original build manifest remains immutable.  The returned candidate
    carries both its source bundle checksum and a combined checksum that also
    covers the target-rendered nginx attestation.
    """

    payload = _read_json(manifest_path)
    if not isinstance(payload, dict):
        raise ValueError("release manifest must be a JSON object")
    attestation = validate_nginx_attestation(_read_json(attestation_path))
    unit = payload.get("deployment_unit")
    if not isinstance(unit, str) or unit not in DEFAULT_BUNDLE_FILES:
        raise ValueError("release manifest has unsupported deployment unit")
    source_bundle = bundle_checksum(repo_root, unit)
    if payload.get("deployment_bundle_sha256") != source_bundle:
        raise ValueError("release manifest source bundle checksum is stale")
    payload["source_bundle_sha256"] = source_bundle
    payload["target_artifacts"] = attestation
    payload["deployment_bundle_sha256"] = bundle_checksum(
        repo_root,
        unit,
        target_attestation=attestation,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--repo-root", type=Path, default=Path.cwd())
    create.add_argument("--unit", required=True, choices=sorted(DEFAULT_BUNDLE_FILES))
    create.add_argument("--deployment-target", required=True)
    create.add_argument("--commit-sha", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--image", action="append", default=[])
    create.add_argument("--output", required=True, type=Path)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--repo-root", type=Path, default=Path.cwd())
    validate.add_argument("--manifest", required=True, type=Path)
    validate.add_argument("--unit")
    validate.add_argument("--deployment-target")
    validate.add_argument("--commit-sha")
    validate.add_argument("--image", action="append", default=[])

    attest = subparsers.add_parser("attest-nginx")
    attest.add_argument("--root", required=True, type=Path)
    attest.add_argument("--public-host")
    attest.add_argument("--source-allowlist-cidrs")
    attest.add_argument("--output", required=True, type=Path)

    bind = subparsers.add_parser("bind-nginx-attestation")
    bind.add_argument("--repo-root", type=Path, default=Path.cwd())
    bind.add_argument("--manifest", required=True, type=Path)
    bind.add_argument("--attestation", required=True, type=Path)
    bind.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "create":
            payload = create_manifest(
                repo_root=args.repo_root.resolve(),
                unit=args.unit,
                deployment_target=args.deployment_target,
                commit_sha=args.commit_sha,
                run_id=args.run_id,
                raw_images=args.image,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif args.command == "validate":
            validate_manifest(
                repo_root=args.repo_root.resolve(),
                manifest_path=args.manifest,
                expected_unit=args.unit,
                expected_target=args.deployment_target,
                expected_commit=args.commit_sha,
                expected_images=args.image or None,
            )
        elif args.command == "attest-nginx":
            payload = nginx_attestation(
                args.root.resolve(),
                public_host=args.public_host,
                source_allowlist_cidrs=args.source_allowlist_cidrs,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        else:
            payload = bind_target_attestation(
                repo_root=args.repo_root.resolve(),
                manifest_path=args.manifest,
                attestation_path=args.attestation,
                output_path=args.output,
            )
            # Keep the command's success output free of target credential
            # material; only the schema-level operation is reported.
            del payload
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"release manifest {args.command}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
