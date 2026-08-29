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
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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


def _validate_images_for_unit(unit: str, images: object) -> None:
    expected_images = IMAGE_REPOSITORIES[unit]
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
    unknown = set(manifest) - required
    missing = required - set(manifest)
    if unknown or missing:
        raise ManifestError(
            f"manifest keys invalid: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["deployment_target"] != "staging"
    ):
        raise ManifestError("schema_version or deployment_target invalid")
    unit = manifest["unit"]
    if not isinstance(unit, str) or unit not in IMAGE_REPOSITORIES:
        raise ManifestError("unit invalid")
    if not isinstance(manifest["commit_sha"], str) or not COMMIT_RE.fullmatch(
        manifest["commit_sha"]
    ):
        raise ManifestError("commit_sha must be a lowercase 40-character SHA")
    images = manifest["images"]
    _validate_images_for_unit(unit, images)
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
    created_by_run_id: str,
    images: dict[str, str],
) -> None:
    """Bind validation to the exact release inputs selected by the workflow."""
    _validate_images_for_unit(unit, images)
    expected = {
        "unit": unit,
        "commit_sha": commit_sha,
        "migration_revision": migration_revision,
        "created_by_run_id": created_by_run_id,
        "images": images,
    }
    for field, expected_value in expected.items():
        if manifest.get(field) != expected_value:
            raise ManifestError(f"manifest {field} does not match validation inputs")


def canonical_json(manifest: dict[str, Any]) -> bytes:
    return canonical_json_bytes(manifest) + b"\n"


def _require_output_descriptor_safety() -> None:
    required = (os.open, os.stat, os.unlink)
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
            "schema_version": 1,
            "deployment_target": "staging",
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
        }
        validate_manifest(manifest, contract_manifest, source_bundle)
    _write_manifest_atomically(_absolute_unresolved_path(args.output), canonical_json(manifest))


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
    generate_parser.add_argument("--output", type=Path, required=True)
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
            sys.stdout.write("findb-release-manifest-v1\n")
        elif args.command == "generate":
            generate(args)
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
