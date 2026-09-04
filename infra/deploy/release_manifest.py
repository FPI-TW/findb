#!/usr/bin/env python3
"""Generate and validate fail-closed staging release manifests.

This utility deliberately uses only the Python standard library so the release
record can be verified on an otherwise minimal GitHub Actions runner or host.
It is a foundation artifact: callers still deploy by the existing SHA tag.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import io
import json
import os
import re
import secrets
import stat
import sys
import tarfile
from pathlib import Path
from typing import Any

REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_MEMBERS = 128
MAX_BUNDLE_MEMBER_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_UNCOMPRESSED_BYTES = 12 * 1024 * 1024
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[0-9]+$")
ALEMBIC_REVISION_RE = re.compile(r"^[0-9a-f]{12}$")
IMAGE_RE = re.compile(r"^(?P<repository>[^@]+)@sha256:(?P<digest>[0-9a-f]{64})$")
CONTRACT_SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
CONTRACT_PATH_RE = re.compile(
    r"^(?P<schema_id>[a-z][a-z0-9_]*)/v(?P<version>[1-9][0-9]*)\.schema\.json$"
)

IMAGE_REPOSITORIES = {
    "findb": {
        "backend": f"{REGISTRY}/findb/staging/backend",
        "dashboard": f"{REGISTRY}/findb/staging/dashboard",
    },
    "fetcher": {
        "twelve_data": f"{REGISTRY}/findb/staging/fetcher/twelve-data",
        "finlab": f"{REGISTRY}/findb/staging/fetcher/finlab",
        "shioaji": f"{REGISTRY}/findb/staging/fetcher/shioaji",
    },
}


def image_repositories(target: str, unit: str, registry: str = REGISTRY) -> dict[str, str]:
    """Return the only repositories that a target/unit release may name.

    The account is deliberately an input to the v2 manifest, rather than a
    staging constant hidden in a host script.  The caller validates its AWS
    identity before accepting this registry value.
    """
    if target not in {"staging", "production"} or unit not in IMAGE_REPOSITORIES:
        raise ManifestError("target or unit invalid")
    if not re.fullmatch(r"[0-9]{12}[.]dkr[.]ecr[.]ap-southeast-1[.]amazonaws[.]com", registry):
        raise ManifestError("registry invalid")
    suffixes = {
        "findb": {"backend": "backend", "dashboard": "dashboard"},
        "fetcher": {
            "twelve_data": "fetcher/twelve-data",
            "finlab": "fetcher/finlab",
            "shioaji": "fetcher/shioaji",
        },
    }[unit]
    return {name: f"{registry}/findb/{target}/{suffix}" for name, suffix in suffixes.items()}


COMMON_BUNDLE_FILES = (
    "contracts/manifest.json",
    "infra/deploy/runtime-secrets/load_runtime_secrets.py",
    "infra/deploy/runtime-secrets/runtime_secret_command.sh",
    "infra/deploy/release_manifest.py",
)
FINDB_RUNTIME_SECRET_FILES = (
    "backend/scripts/render_nginx_cloudflare_real_ip.py",
    "backend/scripts/render_nginx_public_host.py",
    "backend/scripts/render_nginx_source_allowlist.py",
    "infra/deploy/runtime-secrets/deploy_findb_aws.sh",
    "infra/deploy/runtime-secrets/findb.json",
    "infra/deploy/runtime-secrets/install_findb_bootstrap.sh",
    "infra/deploy/runtime-secrets/render_nginx_runtime.sh",
    "infra/deploy/runtime-secrets/render_serve_key.py",
)
FETCHER_RUNTIME_SECRET_FILES = (
    "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh",
    "infra/deploy/runtime-secrets/fetcher.json",
    "infra/deploy/runtime-secrets/release_fetcher_provider.sh",
)
FINDB_BUNDLE_FILES = (
    "docker-compose.prod.yml",
    "infra/nginx/cloudflare-real-ip.conf",
    "infra/nginx/nginx.conf",
    "infra/nginx/source-allowlist.conf",
    *COMMON_BUNDLE_FILES,
    *FINDB_RUNTIME_SECRET_FILES,
)
FETCHER_BUNDLE_FILES = (*COMMON_BUNDLE_FILES, *FETCHER_RUNTIME_SECRET_FILES)

# This map is part of the bundle format, not a reflection of the source
# checkout mode.  The bundle executes these selected tools directly on the
# host, so their root-owned materialized form must remain executable.  Every
# other allowlisted member is data/configuration and is deliberately 0644.
EXECUTABLE_BUNDLE_FILES = frozenset(
    (
        "infra/deploy/release_manifest.py",
        "infra/deploy/runtime-secrets/load_runtime_secrets.py",
        "infra/deploy/runtime-secrets/runtime_secret_command.sh",
        "backend/scripts/render_nginx_cloudflare_real_ip.py",
        "backend/scripts/render_nginx_public_host.py",
        "backend/scripts/render_nginx_source_allowlist.py",
        "infra/deploy/runtime-secrets/deploy_findb_aws.sh",
        "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh",
        "infra/deploy/runtime-secrets/install_findb_bootstrap.sh",
        "infra/deploy/runtime-secrets/render_nginx_runtime.sh",
        "infra/deploy/runtime-secrets/render_serve_key.py",
        "infra/deploy/runtime-secrets/release_fetcher_provider.sh",
    )
)
BUNDLE_FILE_MODES = {path: 0o755 for path in EXECUTABLE_BUNDLE_FILES}
BUNDLE_DATA_MODE = 0o644


class ManifestError(ValueError):
    """Raised for a release record that does not meet the closed schema."""


class ContractManifest:
    """Strictly parsed selected contract manifest and its semantic identity."""

    __slots__ = ("parsed", "versions", "canonical_sha256")

    def __init__(
        self,
        *,
        parsed: dict[str, Any],
        versions: tuple[str, ...],
        canonical_sha256: str,
    ) -> None:
        self.parsed = parsed
        self.versions = versions
        self.canonical_sha256 = canonical_sha256


class SourceBundleReader:
    """Pin one selected source root while reading all manifest inputs."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._root_fd: int | None = None
        self._snapshot: dict[str, bytes] = {}

    def __enter__(self) -> SourceBundleReader:
        self._root_fd = _open_source_bundle_root(self.repo_root)
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    def read(self, relative: str) -> bytes:
        if self._root_fd is None:
            raise ManifestError("source bundle root is not open")
        normalized = _normalize_source_relative_path(relative)
        if normalized not in self._snapshot:
            self._snapshot[normalized] = _safe_source_file_bytes_from_root_fd(
                self._root_fd,
                normalized,
            )
        return self._snapshot[normalized]

    @property
    def snapshot_paths(self) -> frozenset[str]:
        """The exact selected-source paths frozen into this reader snapshot."""
        return frozenset(self._snapshot)

    def deployment_source_bundle_sha256(self, unit: str) -> str:
        records = [
            {
                "path": relative,
                "sha256": hashlib.sha256(self.read(relative)).hexdigest(),
            }
            for relative in sorted(bundle_paths(unit))
        ]
        canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def load_selected_contract_manifest(self, contract_manifest_path: Path) -> ContractManifest:
        expected_path = _absolute_unresolved_path(self.repo_root / "contracts" / "manifest.json")
        provided_path = (
            contract_manifest_path
            if contract_manifest_path.is_absolute()
            else self.repo_root / contract_manifest_path
        )
        if _absolute_unresolved_path(provided_path) != expected_path:
            raise ManifestError(
                "contract manifest must be the selected source contracts/manifest.json"
            )
        contract_manifest = _parse_contract_manifest_bytes(self.read("contracts/manifest.json"))
        for contract in contract_manifest.parsed["contracts"]:
            schema_path = f"contracts/{contract['path']}"
            schema_sha256 = hashlib.sha256(self.read(schema_path)).hexdigest()
            if schema_sha256 != contract["sha256"]:
                raise ManifestError(f"contract schema checksum mismatch: {contract['path']}")
        return contract_manifest


def bundle_paths(unit: str) -> tuple[str, ...]:
    try:
        return {"findb": FINDB_BUNDLE_FILES, "fetcher": FETCHER_BUNDLE_FILES}[unit]
    except KeyError as exc:
        raise ManifestError("unit must be findb or fetcher") from exc


def bundle_source_paths(unit: str, contract_manifest: ContractManifest) -> tuple[str, ...]:
    """Return the complete allowlist captured in a deployable unit bundle.

    Contract schemas are included even though they are not runtime scripts: the
    selected manifest tool validates their claimed checksums before it accepts
    the release manifest.  Keeping them in the archive makes that validation
    self-contained on the SSM host.
    """
    contract_paths = tuple(
        f"contracts/{contract['path']}" for contract in contract_manifest.parsed["contracts"]
    )
    return (*bundle_paths(unit), *contract_paths)


def bundle_file_mode(relative: str) -> int:
    """Return the one canonical archive/materialization mode for a member."""
    return BUNDLE_FILE_MODES.get(relative, BUNDLE_DATA_MODE)


def _require_descriptor_safety() -> None:
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
    ):
        raise ManifestError("descriptor-safe source bundle reads are unavailable on this platform")


def _open_source_bundle_root(repo_root: Path) -> int:
    """Open the un-resolved root once, rejecting a symlink or a root swap."""
    _require_descriptor_safety()
    try:
        root_info = os.lstat(repo_root)
    except OSError as exc:
        raise ManifestError("source bundle root unavailable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ManifestError("source bundle root is not a directory")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    root_fd: int | None = None
    try:
        root_fd = os.open(repo_root, directory_flags)
        opened_root = os.fstat(root_fd)
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ManifestError("source bundle root is not a directory") from exc
        raise ManifestError("unable to securely open source bundle root") from exc
    if (opened_root.st_dev, opened_root.st_ino) != (root_info.st_dev, root_info.st_ino):
        os.close(root_fd)
        raise ManifestError("source bundle root changed while opening")
    return root_fd


def _safe_source_file_bytes_from_root_fd(root_fd: int, relative: str) -> bytes:
    """Read an allowlisted input below an already-open root descriptor."""
    relative_path = Path(_normalize_source_relative_path(relative))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    directory_fds: list[int] = []
    leaf_fd: int | None = None
    try:
        current_fd = root_fd
        for part in relative_path.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            directory_fds.append(next_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                raise ManifestError(f"source bundle parent is not a directory: {relative}")
            current_fd = next_fd
        leaf_fd = os.open(relative_path.name, file_flags, dir_fd=current_fd)
        before = os.fstat(leaf_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ManifestError(f"source bundle path is not a regular file: {relative}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(leaf_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        after = os.fstat(leaf_fd)
        stable = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_nlink,
        )
        if (
            stable
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
            )
            or len(content) != before.st_size
        ):
            raise ManifestError(f"source bundle path changed while reading: {relative}")
        return content
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManifestError(f"source bundle path is not a regular file: {relative}") from exc
        raise ManifestError(f"unable to securely traverse source bundle path: {relative}") from exc
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _safe_source_file_bytes(repo_root: Path, relative: str) -> bytes:
    """Single-file compatibility wrapper around the descriptor reader."""
    root_fd = _open_source_bundle_root(repo_root)
    try:
        return _safe_source_file_bytes_from_root_fd(root_fd, relative)
    finally:
        os.close(root_fd)


def _normalize_source_relative_path(relative: str) -> str:
    """Reject aliases so each snapshot key names one literal selected file."""
    if not isinstance(relative, str) or not relative:
        raise ManifestError("invalid source bundle path")
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or "\\" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
    ):
        raise ManifestError(f"invalid source bundle path: {relative}")
    return relative


def deployment_source_bundle_sha256(repo_root: Path, unit: str) -> str:
    """Hash deterministic, unrendered source/template inputs for one unit."""
    with SourceBundleReader(repo_root) as source_bundle:
        return source_bundle.deployment_source_bundle_sha256(unit)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid manifest JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ManifestError("manifest root must be an object")
    return parsed


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def load_contract_manifest(path: Path) -> ContractManifest:
    """Strictly parse a contract manifest for standalone, schema-free callers."""
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ManifestError("contract manifest must be a regular file")
        raw = path.read_bytes()
    except OSError as exc:
        raise ManifestError(f"invalid contract manifest JSON: {exc}") from exc
    return _parse_contract_manifest_bytes(raw)


def _parse_contract_manifest_bytes(raw: bytes) -> ContractManifest:
    """Parse a strict contract manifest after its source bytes are pinned."""
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid contract manifest JSON: {exc}") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"manifest_version", "contracts"}:
        raise ManifestError("contract manifest root keys invalid")
    if type(parsed["manifest_version"]) is not int or parsed["manifest_version"] != 1:
        raise ManifestError("contract manifest version invalid")
    contracts = parsed["contracts"]
    if not isinstance(contracts, list) or not contracts:
        raise ManifestError("contract manifest contracts must be a nonempty list")
    versions: list[str] = []
    seen: set[tuple[str, int]] = set()
    required = {"path", "schema_id", "schema_version", "sha256"}
    for contract in contracts:
        if not isinstance(contract, dict) or set(contract) != required:
            raise ManifestError("contract manifest entry keys invalid")
        schema_id = contract["schema_id"]
        schema_version = contract["schema_version"]
        relative_path = contract["path"]
        checksum = contract["sha256"]
        if not isinstance(schema_id, str) or not CONTRACT_SCHEMA_ID_RE.fullmatch(schema_id):
            raise ManifestError("contract schema_id invalid")
        if type(schema_version) is not int or schema_version < 1:
            raise ManifestError("contract schema_version invalid")
        if not isinstance(relative_path, str) or not isinstance(checksum, str):
            raise ManifestError("contract path or checksum invalid")
        path_match = CONTRACT_PATH_RE.fullmatch(relative_path)
        if (
            not path_match
            or path_match.group("schema_id") != schema_id
            or int(path_match.group("version")) != schema_version
            or not SHA256_RE.fullmatch(checksum)
        ):
            raise ManifestError("contract path, id, version, or checksum invalid")
        identity = (schema_id, schema_version)
        if identity in seen:
            raise ManifestError("duplicate contract schema_id and version")
        seen.add(identity)
        versions.append(f"{schema_id}.v{schema_version}")
    semantic_content = {
        "manifest_version": parsed["manifest_version"],
        "contracts": sorted(
            contracts,
            key=lambda contract: (contract["schema_id"], contract["schema_version"]),
        ),
    }
    return ContractManifest(
        parsed=parsed,
        versions=tuple(sorted(versions)),
        canonical_sha256=hashlib.sha256(canonical_json_bytes(semantic_content)).hexdigest(),
    )


def load_contract_versions(path: Path) -> tuple[str, ...]:
    """Compatibility helper for callers that only require the contract set."""
    return load_contract_manifest(path).versions


def _validate_images_for_unit(
    unit: str, images: object, *, target: str = "staging", registry: str = REGISTRY
) -> None:
    expected_images = image_repositories(target, unit, registry)
    if not isinstance(images, dict) or set(images) != set(expected_images):
        raise ManifestError("images keys do not match the unit contract")
    for name, repository in expected_images.items():
        reference = images[name]
        if not isinstance(reference, str):
            raise ManifestError(f"image reference invalid: {name}")
        matched = IMAGE_RE.fullmatch(reference)
        if not matched or matched.group("repository") != repository:
            raise ManifestError(f"image repository or digest invalid: {name}")


def validate_manifest(
    manifest: dict[str, Any],
    contract_manifest: ContractManifest,
    source_bundle: SourceBundleReader | None = None,
) -> None:
    schema_version = manifest.get("schema_version")
    if schema_version == 1:
        required = {
            "schema_version",
            "deployment_target",
            "unit",
            "commit_sha",
            "images",
            "migration_revision",
            "contract_versions",
            "contract_manifest_sha256",
            "deployment_source_bundle_sha256",
            "created_by_run_id",
        }
    elif schema_version == 2:
        required = {
            "schema_version",
            "deployment_target",
            "unit",
            "commit_sha",
            "images",
            "migration_revision",
            "contract_versions",
            "contract_manifest_sha256",
            "deployment_source_bundle_sha256",
            "created_by_run_id",
            "registry",
            "account_id",
            "release_tag",
        }
        if manifest.get("deployment_target") == "production":
            required.add("promotion_source")
    else:
        raise ManifestError("schema_version invalid")
    unknown = set(manifest) - required
    missing = required - set(manifest)
    if unknown or missing:
        raise ManifestError(
            f"manifest keys invalid: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    target = manifest["deployment_target"]
    if type(schema_version) is not int or target not in {"staging", "production"}:
        raise ManifestError("schema_version or deployment_target invalid")
    if schema_version == 1 and target != "staging":
        raise ManifestError("v1 manifests are staging-only")
    unit = manifest["unit"]
    if not isinstance(unit, str) or unit not in IMAGE_REPOSITORIES:
        raise ManifestError("unit invalid")
    if not isinstance(manifest["commit_sha"], str) or not COMMIT_RE.fullmatch(
        manifest["commit_sha"]
    ):
        raise ManifestError("commit_sha must be a lowercase 40-character SHA")
    images = manifest["images"]
    registry = REGISTRY
    if schema_version == 2:
        registry = manifest["registry"]
        account_id = manifest["account_id"]
        release_tag = manifest["release_tag"]
        expected_release_tag = "" if target == "staging" else rf"{unit}-v[0-9]+[.][0-9]+[.][0-9]+"
        if (
            not isinstance(registry, str)
            or not isinstance(account_id, str)
            or not re.fullmatch(r"[0-9]{12}", account_id)
            or registry != f"{account_id}.dkr.ecr.ap-southeast-1.amazonaws.com"
            or not isinstance(release_tag, str)
            or re.fullmatch(expected_release_tag, release_tag) is None
        ):
            raise ManifestError("registry or release_tag invalid")
        if target == "production":
            source = manifest["promotion_source"]
            if not isinstance(source, dict) or set(source) != {
                "accepted_bundle_key",
                "staging_registry",
            }:
                raise ManifestError("production promotion_source invalid")
            if (
                not isinstance(source["accepted_bundle_key"], str)
                or not re.fullmatch(
                    rf"{unit}/accepted/[0-9a-f]{{40}}/[0-9a-f]{{64}}[.]tar",
                    source["accepted_bundle_key"],
                )
                or not isinstance(source["staging_registry"], str)
                or re.fullmatch(
                    r"[0-9]{12}[.]dkr[.]ecr[.]ap-southeast-1[.]amazonaws[.]com",
                    source["staging_registry"],
                )
                is None
            ):
                raise ManifestError("production promotion_source invalid")
    _validate_images_for_unit(unit, images, target=target, registry=registry)
    migration = manifest["migration_revision"]
    if not isinstance(migration, str) or not migration:
        raise ManifestError("migration_revision must be nonempty")
    if unit == "fetcher" and migration != "none":
        raise ManifestError("migration_revision does not match the unit contract")
    if unit == "findb" and (
        not isinstance(migration, str) or not ALEMBIC_REVISION_RE.fullmatch(migration)
    ):
        raise ManifestError("migration_revision must be the exact 12-character Alembic head")
    versions = manifest["contract_versions"]
    if not isinstance(versions, list) or versions != list(contract_manifest.versions):
        raise ManifestError("contract_versions do not match the selected contract manifest")
    contract_digest = manifest["contract_manifest_sha256"]
    if (
        not isinstance(contract_digest, str)
        or not SHA256_RE.fullmatch(contract_digest)
        or contract_digest != contract_manifest.canonical_sha256
    ):
        raise ManifestError(
            "contract_manifest_sha256 does not match the selected contract manifest"
        )
    bundle_digest = manifest["deployment_source_bundle_sha256"]
    if not isinstance(bundle_digest, str) or not SHA256_RE.fullmatch(bundle_digest):
        raise ManifestError("deployment_source_bundle_sha256 invalid")
    run_id = manifest["created_by_run_id"]
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ManifestError("created_by_run_id must be a numeric, nonempty string")
    if source_bundle is not None and bundle_digest != source_bundle.deployment_source_bundle_sha256(
        unit
    ):
        raise ManifestError("deployment source bundle checksum mismatch")


def validate_expected_release_inputs(
    manifest: dict[str, Any],
    *,
    unit: str,
    commit_sha: str,
    migration_revision: str,
    created_by_run_id: str | None,
    images: dict[str, str],
    deployment_target: str | None = None,
) -> None:
    """Bind validation to the exact release inputs selected by the workflow."""
    target = deployment_target or str(manifest.get("deployment_target", ""))
    registry = str(manifest.get("registry", REGISTRY))
    _validate_images_for_unit(unit, images, target=target, registry=registry)
    expected = {
        "unit": unit,
        "commit_sha": commit_sha,
        "migration_revision": migration_revision,
        "images": images,
    }
    if created_by_run_id is not None:
        expected["created_by_run_id"] = created_by_run_id
    if deployment_target is not None:
        expected["deployment_target"] = deployment_target
    for field, expected_value in expected.items():
        if manifest.get(field) != expected_value:
            raise ManifestError(f"manifest {field} does not match validation inputs")


def canonical_json(manifest: dict[str, Any]) -> bytes:
    return canonical_json_bytes(manifest) + b"\n"


BUNDLE_MANIFEST_PATH = "release-manifest.json"


def _load_manifest_bytes(raw: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid manifest JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ManifestError("manifest root must be an object")
    return parsed


def _bundle_tarinfo(relative: str, content: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(relative)
    info.size = len(content)
    info.mode = bundle_file_mode(relative)
    info.uid = 0
    info.gid = 0
    info.mtime = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def build_bundle(repo_root: Path, manifest_path: Path, unit: str) -> bytes:
    """Build deterministic tar bytes for the manifest and selected unit files.

    The archive deliberately has no self-checksum.  Its external SHA256 is the
    immutable S3 object identity recorded by the workflow and acceptance
    record, avoiding a manifest/archive checksum cycle.
    """
    manifest_raw = manifest_path.read_bytes()
    manifest = _load_manifest_bytes(manifest_raw)
    with SourceBundleReader(repo_root) as source_bundle:
        contract_manifest = source_bundle.load_selected_contract_manifest(
            repo_root / "contracts" / "manifest.json"
        )
        validate_manifest(manifest, contract_manifest, source_bundle)
        if manifest["unit"] != unit:
            raise ManifestError("bundle unit does not match release manifest")
        entries = [(BUNDLE_MANIFEST_PATH, canonical_json(manifest))]
        entries.extend(
            (relative, source_bundle.read(relative))
            for relative in sorted(bundle_source_paths(unit, contract_manifest))
        )
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for relative, content in entries:
            archive.addfile(_bundle_tarinfo(relative, content), io.BytesIO(content))
    return stream.getvalue()


def _validate_bundle_member(info: tarfile.TarInfo, expected_path: str) -> None:
    if (
        info.name != expected_path
        or not info.isreg()
        or info.issym()
        or info.islnk()
        or info.linkname
        or info.uid != 0
        or info.gid != 0
        or info.mtime != 0
        or info.mode != bundle_file_mode(expected_path)
    ):
        raise ManifestError(f"bundle member is unsafe or noncanonical: {expected_path}")


def validate_bundle_bytes(
    raw: bytes,
    *,
    expected_sha256: str,
    unit: str,
    expected_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate an immutable bundle without extracting attacker-controlled paths."""
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ManifestError("bundle exceeds the maximum permitted size")
    if (
        not SHA256_RE.fullmatch(expected_sha256)
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        raise ManifestError("bundle SHA256 does not match the externally expected value")
    try:
        archive = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise ManifestError("bundle is not a valid uncompressed tar archive") from exc
    with archive:
        members = archive.getmembers()
        if len(members) > MAX_BUNDLE_MEMBERS:
            raise ManifestError("bundle has too many members")
        total_size = 0
        for member in members:
            if member.size < 0 or member.size > MAX_BUNDLE_MEMBER_BYTES:
                raise ManifestError("bundle member exceeds the maximum permitted size")
            total_size += member.size
            if total_size > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                raise ManifestError("bundle exceeds the maximum uncompressed size")
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ManifestError("bundle contains duplicate member paths")
        if any(
            Path(name).is_absolute()
            or "\\" in name
            or any(part in ("", ".", "..") for part in name.split("/"))
            for name in names
        ):
            raise ManifestError("bundle contains an unsafe member path")
        manifest_member = next(
            (member for member in members if member.name == BUNDLE_MANIFEST_PATH), None
        )
        if manifest_member is None:
            raise ManifestError("bundle release manifest is missing")
        _validate_bundle_member(manifest_member, BUNDLE_MANIFEST_PATH)
        extracted = archive.extractfile(manifest_member)
        if extracted is None:
            raise ManifestError("bundle release manifest is unreadable")
        manifest = _load_manifest_bytes(extracted.read())
        if manifest.get("unit") != unit:
            raise ManifestError("bundle unit does not match expected unit")
        expected_paths = set(bundle_paths(unit))
        # The contract manifest identifies the schema members expected in the archive.
        contract_member = next(
            (member for member in members if member.name == "contracts/manifest.json"), None
        )
        if contract_member is None:
            raise ManifestError("bundle contract manifest is missing")
        _validate_bundle_member(contract_member, "contracts/manifest.json")
        contract_raw = archive.extractfile(contract_member)
        if contract_raw is None:
            raise ManifestError("bundle contract manifest is unreadable")
        contract_manifest = _parse_contract_manifest_bytes(contract_raw.read())
        expected_paths.update(bundle_source_paths(unit, contract_manifest))
        if set(names) != ({BUNDLE_MANIFEST_PATH} | expected_paths):
            raise ManifestError("bundle members do not match the unit allowlist")
        contents: dict[str, bytes] = {}
        for member in members:
            _validate_bundle_member(member, member.name)
            fileobj = archive.extractfile(member)
            if fileobj is None:
                raise ManifestError(f"bundle member is unreadable: {member.name}")
            contents[member.name] = fileobj.read()
        for contract in contract_manifest.parsed["contracts"]:
            path = f"contracts/{contract['path']}"
            if hashlib.sha256(contents[path]).hexdigest() != contract["sha256"]:
                raise ManifestError(f"bundle contract schema checksum mismatch: {contract['path']}")
        source_records = [
            {"path": path, "sha256": hashlib.sha256(contents[path]).hexdigest()}
            for path in sorted(bundle_paths(unit))
        ]
        source_sha = hashlib.sha256(canonical_json_bytes(source_records)).hexdigest()
        if manifest.get("deployment_source_bundle_sha256") != source_sha:
            raise ManifestError("bundle deployment source checksum mismatch")
        validate_manifest(manifest, contract_manifest)
        if expected_inputs is not None:
            validate_expected_release_inputs(manifest, **expected_inputs)
        return manifest


def materialize_validated_bundle(
    raw: bytes,
    *,
    expected_sha256: str,
    unit: str,
    output: Path,
    expected_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate first, then write the fixed allowlist through directory FDs.

    `tarfile.extract*` is deliberately never used: archive paths are untrusted
    until validation completes and each output file is created with
    O_NOFOLLOW|O_EXCL below a newly-created root.
    """
    manifest = validate_bundle_bytes(
        raw,
        expected_sha256=expected_sha256,
        unit=unit,
        expected_inputs=expected_inputs,
    )
    root_fd = _create_materialization_root(output)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive.getmembers():
                if member.name == BUNDLE_MANIFEST_PATH:
                    continue
                content_file = archive.extractfile(member)
                if content_file is None:
                    raise ManifestError(f"bundle member is unreadable: {member.name}")
                _write_materialized_file(
                    root_fd,
                    member.name,
                    content_file.read(),
                    bundle_file_mode(member.name),
                )
    finally:
        os.close(root_fd)
    return manifest


def _create_materialization_root(output: Path) -> int:
    """Create and pin a new output directory without resolving any aliases.

    The caller has already validated the archive bytes.  This boundary still
    must treat the destination path as hostile: every ancestor is opened from
    ``/`` by descriptor, and the new leaf's inode is checked after opening so
    a symlink or rename race cannot redirect later writes.
    """
    _require_output_descriptor_safety()
    output = _absolute_unresolved_path(output)
    if not output.is_absolute() or output.name in ("", ".", ".."):
        raise ManifestError("bundle materialization output path is invalid")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC

    current_fd: int | None = None
    try:
        current_fd = os.open(os.sep, directory_flags)
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            raise ManifestError("bundle materialization output root is not a directory")
        for component in output.parts[1:-1]:
            current_fd = _open_or_create_output_directory(current_fd, component)
        try:
            os.mkdir(output.name, 0o700, dir_fd=current_fd)
        except FileExistsError as exc:
            raise ManifestError("bundle materialization output already exists") from exc
        try:
            created_info = os.stat(output.name, dir_fd=current_fd, follow_symlinks=False)
            if not stat.S_ISDIR(created_info.st_mode):
                raise ManifestError("bundle materialization output is not a directory")
            leaf_fd = os.open(output.name, directory_flags, dir_fd=current_fd)
            opened_info = os.fstat(leaf_fd)
            if (opened_info.st_dev, opened_info.st_ino) != (
                created_info.st_dev,
                created_info.st_ino,
            ):
                os.close(leaf_fd)
                raise ManifestError("bundle materialization output changed while opening")
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ManifestError("bundle materialization output is not a directory") from exc
            raise ManifestError("unable to securely create bundle materialization output") from exc
        os.close(current_fd)
        current_fd = None
        return leaf_fd
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ManifestError(
                "bundle materialization output ancestor is not a directory"
            ) from exc
        raise ManifestError("unable to securely open bundle materialization output") from exc
    finally:
        if current_fd is not None:
            os.close(current_fd)


def _open_or_create_output_directory(parent_fd: int, component: str) -> int:
    """Open one safe output ancestor, creating it only when absent."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    try:
        info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(component, 0o755, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise ManifestError("bundle materialization output ancestor changed") from exc
        info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise ManifestError("bundle materialization output ancestor is not a directory")
    opened_fd = os.open(component, directory_flags, dir_fd=parent_fd)
    opened_info = os.fstat(opened_fd)
    if (opened_info.st_dev, opened_info.st_ino) != (info.st_dev, info.st_ino):
        os.close(opened_fd)
        raise ManifestError("bundle materialization output ancestor changed while opening")
    os.close(parent_fd)
    return opened_fd


def _write_materialized_file(root_fd: int, relative: str, content: bytes, mode: int) -> None:
    relative = _normalize_source_relative_path(relative)
    parts = Path(relative).parts
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    directories: list[int] = []
    file_fd: int | None = None
    try:
        current_fd = root_fd
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            directories.append(next_fd)
            current_fd = next_fd
        if mode != bundle_file_mode(relative):
            raise ManifestError(f"bundle materialization mode is invalid: {relative}")
        file_fd = os.open(parts[-1], file_flags, mode, dir_fd=current_fd)
        os.fchmod(file_fd, mode)
        offset = 0
        while offset < len(content):
            written = os.write(file_fd, content[offset:])
            if written <= 0:
                raise ManifestError("bundle materialization write made no progress")
            offset += written
        os.fsync(file_fd)
        materialized = os.fstat(file_fd)
        if stat.S_IMODE(materialized.st_mode) != mode:
            raise ManifestError(f"bundle materialization mode changed while writing: {relative}")
    except OSError as exc:
        raise ManifestError(f"unable to safely materialize bundle path: {relative}") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for directory_fd in reversed(directories):
            os.close(directory_fd)


def _require_output_descriptor_safety() -> None:
    required = (os.open, os.stat, os.unlink, os.mkdir)
    if (
        not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or any(operation not in os.supports_dir_fd for operation in required)
    ):
        raise ManifestError("safe manifest output writes are unavailable on this platform")


def _stable_output_file_info(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ManifestError("manifest output temporary file is not a single-link regular file")
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _write_manifest_atomically(output: Path, content: bytes) -> None:
    """Durably replace an output entry through one pinned parent directory FD."""
    _require_output_descriptor_safety()
    output_name = output.name
    if output_name in ("", ".", ".."):
        raise ManifestError("output manifest filename invalid")
    output = _absolute_unresolved_path(output)
    parent_parts = output.parent.parts
    if not output.is_absolute() or not parent_parts or parent_parts[0] != os.sep:
        raise ManifestError("output manifest path must be absolute")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    parent_fd: int | None = None
    walk_fds: list[int] = []
    descriptor: int | None = None
    temporary_name: str | None = None
    try:
        root_fd = os.open(os.sep, directory_flags)
        walk_fds.append(root_fd)
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise ManifestError("output manifest parent is not a directory")
        parent_fd = root_fd
        for component in parent_parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            walk_fds.append(next_fd)
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                raise ManifestError("output manifest parent is not a directory")
            parent_fd = next_fd
        try:
            output_info = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            output_info = None
        if output_info is not None and not (
            stat.S_ISREG(output_info.st_mode) or stat.S_ISLNK(output_info.st_mode)
        ):
            raise ManifestError("output manifest path must be a regular file or symlink")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        for _ in range(16):
            temporary_name = f".{output_name}.tmp-{secrets.token_hex(16)}"
            try:
                descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
                break
            except FileExistsError:
                temporary_name = None
        if descriptor is None or temporary_name is None:
            raise ManifestError("unable to allocate manifest output temporary file")
        os.fchmod(descriptor, 0o600)
        _stable_output_file_info(os.fstat(descriptor))
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("manifest temporary file write made no progress")
            remaining = remaining[written:]
        written_info = _stable_output_file_info(os.fstat(descriptor))
        if written_info[2] != len(content):
            raise ManifestError("manifest output temporary file size invalid")
        os.fsync(descriptor)
        if _stable_output_file_info(os.fstat(descriptor)) != written_info:
            raise ManifestError("manifest output temporary file changed while writing")
        os.close(descriptor)
        descriptor = None
        # rename(2) replaces the directory entry itself, so a symlink at
        # ``output`` is never followed, including a dangling one.
        os.replace(
            temporary_name,
            output_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
    except ManifestError:
        raise
    except OSError as exc:
        raise ManifestError("unable to securely access or write manifest output") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_name is not None and parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for walk_fd in reversed(walk_fds):
            try:
                os.close(walk_fd)
            except OSError:
                pass


def parse_image(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ManifestError("image must be name=repository@sha256:digest")
    return tuple(value.split("=", 1))  # type: ignore[return-value]


def parse_images(values: list[str]) -> dict[str, str]:
    images: dict[str, str] = {}
    for value in values:
        name, reference = parse_image(value)
        if name in images:
            raise ManifestError(f"duplicate image name: {name}")
        images[name] = reference
    return images


def generate(args: argparse.Namespace) -> None:
    repo_root = _absolute_unresolved_path(args.repo_root)
    images = parse_images(args.image)
    with SourceBundleReader(repo_root) as source_bundle:
        contract_manifest = source_bundle.load_selected_contract_manifest(args.contract_manifest)
        manifest: dict[str, Any] = {
            "schema_version": 2,
            "deployment_target": args.deployment_target,
            "unit": args.unit,
            "commit_sha": args.commit_sha,
            "images": images,
            "migration_revision": args.migration_revision,
            "contract_versions": list(contract_manifest.versions),
            "contract_manifest_sha256": contract_manifest.canonical_sha256,
            "deployment_source_bundle_sha256": source_bundle.deployment_source_bundle_sha256(
                args.unit
            ),
            "created_by_run_id": args.created_by_run_id,
            "registry": args.registry,
            "account_id": args.account_id,
            "release_tag": args.release_tag,
        }
        if args.deployment_target == "production":
            manifest["promotion_source"] = {
                "accepted_bundle_key": args.promotion_source_bundle_key,
                "staging_registry": args.promotion_source_registry,
            }
        validate_manifest(manifest, contract_manifest, source_bundle)
    _write_manifest_atomically(_absolute_unresolved_path(args.output), canonical_json(manifest))


def bundle(args: argparse.Namespace) -> None:
    repo_root = _absolute_unresolved_path(args.repo_root)
    manifest_path = _absolute_unresolved_path(args.manifest)
    content = build_bundle(repo_root, manifest_path, args.unit)
    _write_manifest_atomically(_absolute_unresolved_path(args.output), content)


def validate_bundle(args: argparse.Namespace) -> None:
    bundle_path = _absolute_unresolved_path(args.bundle)
    if bundle_path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ManifestError("bundle exceeds the maximum permitted size")
    raw = bundle_path.read_bytes()
    expected_inputs = None
    if args.commit_sha is not None:
        if None in (args.migration_revision, args.image):
            raise ManifestError("expected bundle validation inputs must be complete")
        expected_inputs = {
            "unit": args.unit,
            "commit_sha": args.commit_sha,
            "migration_revision": args.migration_revision,
            "created_by_run_id": args.created_by_run_id,
            "images": parse_images(args.image),
        }
    validate_bundle_bytes(
        raw,
        expected_sha256=args.expected_bundle_sha256,
        unit=args.unit,
        expected_inputs=expected_inputs,
    )


def materialize_bundle(args: argparse.Namespace) -> None:
    bundle_path = _absolute_unresolved_path(args.bundle)
    if bundle_path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ManifestError("bundle exceeds the maximum permitted size")
    raw = bundle_path.read_bytes()
    expected_inputs = None
    if args.commit_sha is not None:
        if None in (args.migration_revision, args.image):
            raise ManifestError("expected bundle validation inputs must be complete")
        expected_inputs = {
            "unit": args.unit,
            "commit_sha": args.commit_sha,
            "migration_revision": args.migration_revision,
            "created_by_run_id": args.created_by_run_id,
            "images": parse_images(args.image),
        }
    materialize_validated_bundle(
        raw,
        expected_sha256=args.expected_bundle_sha256,
        unit=args.unit,
        output=_absolute_unresolved_path(args.output),
        expected_inputs=expected_inputs,
    )


def _absolute_unresolved_path(path: Path) -> Path:
    """Make an absolute path without resolving symlinks before the secure open."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("protocol")
    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--repo-root", type=Path, required=True)
    generate_parser.add_argument("--unit", choices=("findb", "fetcher"), required=True)
    generate_parser.add_argument("--commit-sha", required=True)
    generate_parser.add_argument("--migration-revision", required=True)
    generate_parser.add_argument("--contract-manifest", type=Path, required=True)
    generate_parser.add_argument("--image", action="append", required=True)
    generate_parser.add_argument("--created-by-run-id", required=True)
    generate_parser.add_argument(
        "--deployment-target", choices=("staging", "production"), default="staging"
    )
    generate_parser.add_argument("--registry", default=REGISTRY)
    generate_parser.add_argument("--account-id", default=REGISTRY.split(".", 1)[0])
    generate_parser.add_argument("--release-tag", default="")
    generate_parser.add_argument("--promotion-source-bundle-key", default="")
    generate_parser.add_argument("--promotion-source-registry", default="")
    generate_parser.add_argument("--output", type=Path, required=True)
    bundle_parser = subparsers.add_parser("bundle")
    bundle_parser.add_argument("--repo-root", type=Path, required=True)
    bundle_parser.add_argument("--unit", choices=("findb", "fetcher"), required=True)
    bundle_parser.add_argument("--manifest", type=Path, required=True)
    bundle_parser.add_argument("--output", type=Path, required=True)
    bundle_parser = subparsers.add_parser("validate-bundle")
    bundle_parser.add_argument("--bundle", type=Path, required=True)
    bundle_parser.add_argument("--expected-bundle-sha256", required=True)
    bundle_parser.add_argument("--unit", choices=("findb", "fetcher"), required=True)
    bundle_parser.add_argument("--commit-sha")
    bundle_parser.add_argument("--migration-revision")
    bundle_parser.add_argument("--image", action="append")
    bundle_parser.add_argument("--created-by-run-id")
    materialize_parser = subparsers.add_parser("materialize-bundle")
    materialize_parser.add_argument("--bundle", type=Path, required=True)
    materialize_parser.add_argument("--expected-bundle-sha256", required=True)
    materialize_parser.add_argument("--unit", choices=("findb", "fetcher"), required=True)
    materialize_parser.add_argument("--output", type=Path, required=True)
    materialize_parser.add_argument("--commit-sha")
    materialize_parser.add_argument("--migration-revision")
    materialize_parser.add_argument("--image", action="append")
    materialize_parser.add_argument("--created-by-run-id")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--repo-root", type=Path, required=True)
    validate_parser.add_argument("--unit", choices=("findb", "fetcher"), required=True)
    validate_parser.add_argument("--commit-sha", required=True)
    validate_parser.add_argument("--migration-revision", required=True)
    validate_parser.add_argument("--contract-manifest", type=Path, required=True)
    validate_parser.add_argument("--image", action="append", required=True)
    validate_parser.add_argument("--created-by-run-id", required=True)
    validate_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "protocol":
            sys.stdout.write("findb-release-manifest-v2\n")
        elif args.command == "generate":
            generate(args)
        elif args.command == "bundle":
            bundle(args)
        elif args.command == "validate-bundle":
            validate_bundle(args)
        elif args.command == "materialize-bundle":
            materialize_bundle(args)
        else:
            repo_root = _absolute_unresolved_path(args.repo_root)
            with SourceBundleReader(repo_root) as source_bundle:
                contract_manifest = source_bundle.load_selected_contract_manifest(
                    args.contract_manifest
                )
                manifest = load_manifest(args.manifest)
                validate_manifest(manifest, contract_manifest, source_bundle)
                validate_expected_release_inputs(
                    manifest,
                    unit=args.unit,
                    commit_sha=args.commit_sha,
                    migration_revision=args.migration_revision,
                    created_by_run_id=args.created_by_run_id,
                    images=parse_images(args.image),
                )
    except ManifestError as exc:
        print(f"release_manifest=failed reason={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
