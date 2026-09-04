"""Fail-closed contract tests for the stdlib staging release manifest tool."""

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "infra" / "deploy" / "release_manifest.py"
SPEC = importlib.util.spec_from_file_location("release_manifest", TOOL_PATH)
assert SPEC and SPEC.loader
release_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_manifest)

REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
COMMIT = "a" * 40
DIGEST = "b" * 64
CONTRACT_MANIFEST = REPO_ROOT / "contracts" / "manifest.json"
CONTRACT_DATA = release_manifest.load_contract_manifest(CONTRACT_MANIFEST)


def _images(unit: str) -> dict[str, str]:
    return {
        name: f"{repository}@sha256:{DIGEST}"
        for name, repository in release_manifest.IMAGE_REPOSITORIES[unit].items()
    }


def _manifest(unit: str = "findb") -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_target": "staging",
        "unit": unit,
        "commit_sha": COMMIT,
        "images": _images(unit),
        "migration_revision": "c" * 12 if unit == "findb" else "none",
        "contract_versions": list(CONTRACT_DATA.versions),
        "contract_manifest_sha256": CONTRACT_DATA.canonical_sha256,
        "deployment_source_bundle_sha256": "d" * 64,
        "created_by_run_id": "123456",
    }


def _write_selected_source_bundle(root: Path, unit: str) -> None:
    for relative in release_manifest.bundle_paths(unit):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    manifest_path = root / "contracts" / "manifest.json"
    manifest_path.write_bytes(CONTRACT_MANIFEST.read_bytes())
    for contract in CONTRACT_DATA.parsed["contracts"]:
        schema = root / "contracts" / contract["path"]
        schema.parent.mkdir(parents=True, exist_ok=True)
        schema.write_bytes((REPO_ROOT / "contracts" / contract["path"]).read_bytes())


def test_protocol_cli_emits_only_the_v2_marker() -> None:
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), "protocol"],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == b"findb-release-manifest-v2\n"
    assert result.stderr == b""


def test_manifest_schema_rejects_unknown_missing_and_image_contract_breaks() -> None:
    release_manifest.validate_manifest(_manifest(), CONTRACT_DATA)
    for mutate in (
        lambda item: item.pop("unit"),
        lambda item: item.update(extra=True),
        lambda item: item["images"].update(backend=f"{REGISTRY}/findb/staging/backend:latest"),
        lambda item: item["images"].update(backend=f"{REGISTRY}/findb/staging/backend:{DIGEST}"),
        lambda item: item["images"].update(
            backend=f"{REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}"
        ),
        lambda item: item.update(contract_versions=["market_minute.v1", "market_eod.v1"]),
        lambda item: item.pop("contract_manifest_sha256"),
        lambda item: item.update(contract_manifest_sha256="not-a-digest"),
        lambda item: item.update(contract_manifest_sha256="e" * 64),
        lambda item: item.update(deployment_bundle_sha256="d" * 64),
        lambda item: item.pop("deployment_source_bundle_sha256"),
        lambda item: item.update(created_by_run_id="run-123"),
    ):
        candidate = json.loads(json.dumps(_manifest()))
        mutate(candidate)
        with pytest.raises(release_manifest.ManifestError):
            release_manifest.validate_manifest(candidate, CONTRACT_DATA)

    release_manifest.validate_manifest(_manifest("fetcher"), CONTRACT_DATA)
    fetcher = _manifest("fetcher")
    fetcher["migration_revision"] = "not-none"
    with pytest.raises(release_manifest.ManifestError):
        release_manifest.validate_manifest(fetcher, CONTRACT_DATA)
    findb = _manifest()
    findb["migration_revision"] = "c" * 40
    with pytest.raises(release_manifest.ManifestError, match="Alembic head"):
        release_manifest.validate_manifest(findb, CONTRACT_DATA)
    for invalid_schema_version in (True, 1.0, "1"):
        candidate = _manifest()
        candidate["schema_version"] = invalid_schema_version
        with pytest.raises(release_manifest.ManifestError, match="schema_version"):
            release_manifest.validate_manifest(candidate, CONTRACT_DATA)


def test_contract_versions_must_match_the_exact_ingress_contract_set() -> None:
    for versions in (
        ["market_eod.v1"],
        ["market_eod.v1", "market_minute.v1", "unknown.v1"],
        ["market_eod.v1", "market_eod.v1", "market_minute.v1"],
        ["market_minute.v1", "market_eod.v1"],
    ):
        candidate = _manifest()
        candidate["contract_versions"] = versions
        with pytest.raises(release_manifest.ManifestError, match="selected contract manifest"):
            release_manifest.validate_manifest(candidate, CONTRACT_DATA)


def test_contract_versions_are_derived_from_contract_manifest() -> None:
    contract_manifest = json.loads(CONTRACT_MANIFEST.read_text())
    versions = tuple(
        sorted(
            f"{contract['schema_id']}.v{contract['schema_version']}"
            for contract in contract_manifest["contracts"]
        )
    )
    assert versions == release_manifest.load_contract_versions(CONTRACT_MANIFEST)
    assert CONTRACT_DATA.parsed == contract_manifest


def test_contract_manifest_semantic_digest_ignores_whitespace_and_entry_order(
    tmp_path: Path,
) -> None:
    source = json.loads(CONTRACT_MANIFEST.read_text())
    compact = tmp_path / "compact.json"
    pretty = tmp_path / "pretty.json"
    compact.write_text(json.dumps(source, separators=(",", ":")), encoding="utf-8")
    pretty.write_text(
        json.dumps({**source, "contracts": list(reversed(source["contracts"]))}, indent=2),
        encoding="utf-8",
    )
    assert (
        release_manifest.load_contract_manifest(compact).canonical_sha256
        == release_manifest.load_contract_manifest(pretty).canonical_sha256
    )


def test_contract_manifest_semantic_digest_changes_when_entry_checksum_changes(
    tmp_path: Path,
) -> None:
    changed = json.loads(CONTRACT_MANIFEST.read_text())
    changed["contracts"][0]["sha256"] = "e" * 64
    changed_path = tmp_path / "changed.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    assert (
        release_manifest.load_contract_manifest(changed_path).canonical_sha256
        != CONTRACT_DATA.canonical_sha256
    )


def test_contract_manifest_sha256_is_required_and_exact() -> None:
    for mutate in (
        lambda item: item.pop("contract_manifest_sha256"),
        lambda item: item.update(contract_manifest_sha256="bad"),
        lambda item: item.update(contract_manifest_sha256="e" * 64),
    ):
        candidate = _manifest()
        mutate(candidate)
        with pytest.raises(release_manifest.ManifestError, match="contract_manifest_sha256"):
            release_manifest.validate_manifest(candidate, CONTRACT_DATA)


def test_selected_source_contract_manifest_verifies_claims_against_schema_bytes(
    tmp_path: Path,
) -> None:
    valid_root = tmp_path / "valid"
    _write_selected_source_bundle(valid_root, "fetcher")
    with release_manifest.SourceBundleReader(valid_root) as source_bundle:
        selected = source_bundle.load_selected_contract_manifest(
            valid_root / "contracts" / "manifest.json"
        )
    assert selected.versions == CONTRACT_DATA.versions
    assert selected.canonical_sha256 == CONTRACT_DATA.canonical_sha256

    claimed_checksum_root = tmp_path / "claimed-checksum"
    _write_selected_source_bundle(claimed_checksum_root, "fetcher")
    claimed = json.loads((claimed_checksum_root / "contracts" / "manifest.json").read_text())
    claimed["contracts"][0]["sha256"] = "e" * 64
    (claimed_checksum_root / "contracts" / "manifest.json").write_text(json.dumps(claimed))
    with release_manifest.SourceBundleReader(claimed_checksum_root) as source_bundle:
        with pytest.raises(release_manifest.ManifestError, match="schema checksum mismatch"):
            source_bundle.load_selected_contract_manifest(
                claimed_checksum_root / "contracts" / "manifest.json"
            )

    changed_schema_root = tmp_path / "changed-schema"
    _write_selected_source_bundle(changed_schema_root, "fetcher")
    schema_path = changed_schema_root / "contracts" / CONTRACT_DATA.parsed["contracts"][0]["path"]
    schema_path.write_bytes(b'{"changed":true}\n')
    with release_manifest.SourceBundleReader(changed_schema_root) as source_bundle:
        with pytest.raises(release_manifest.ManifestError, match="schema checksum mismatch"):
            source_bundle.load_selected_contract_manifest(
                changed_schema_root / "contracts" / "manifest.json"
            )


def test_selected_source_contract_manifest_semantic_digest_ignores_format_and_entry_order(
    tmp_path: Path,
) -> None:
    compact_root = tmp_path / "compact"
    pretty_root = tmp_path / "pretty"
    _write_selected_source_bundle(compact_root, "fetcher")
    _write_selected_source_bundle(pretty_root, "fetcher")
    parsed = json.loads((pretty_root / "contracts" / "manifest.json").read_text())
    (pretty_root / "contracts" / "manifest.json").write_text(
        json.dumps({**parsed, "contracts": list(reversed(parsed["contracts"]))}, indent=2),
        encoding="utf-8",
    )
    with release_manifest.SourceBundleReader(compact_root) as source_bundle:
        compact = source_bundle.load_selected_contract_manifest(
            compact_root / "contracts" / "manifest.json"
        )
    with release_manifest.SourceBundleReader(pretty_root) as source_bundle:
        pretty = source_bundle.load_selected_contract_manifest(
            pretty_root / "contracts" / "manifest.json"
        )
    assert compact.canonical_sha256 == pretty.canonical_sha256
    assert compact.versions == pretty.versions


@pytest.mark.parametrize(
    "unsafe_kind",
    ("manifest_symlink", "manifest_hardlink", "schema_symlink", "schema_hardlink"),
)
def test_selected_source_contract_manifest_rejects_unsafe_manifest_and_schema_paths(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "fetcher")
    external = tmp_path / "external"
    external.write_bytes(CONTRACT_MANIFEST.read_bytes())
    if unsafe_kind.startswith("manifest_"):
        target = root / "contracts" / "manifest.json"
        target.unlink()
        if unsafe_kind == "manifest_symlink":
            target.symlink_to(external)
        else:
            os.link(external, target)
    else:
        target = root / "contracts" / CONTRACT_DATA.parsed["contracts"][0]["path"]
        target.unlink()
        if unsafe_kind == "schema_symlink":
            target.symlink_to(external)
        else:
            os.link(external, target)
    with release_manifest.SourceBundleReader(root) as source_bundle:
        with pytest.raises(release_manifest.ManifestError, match="regular file"):
            source_bundle.load_selected_contract_manifest(root / "contracts" / "manifest.json")


def test_selected_source_contract_manifest_rejects_root_swap_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "fetcher")
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    original_open = release_manifest.os.open
    swapped = False

    def swap_root_before_open(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if path == root and dir_fd is None and not swapped:
            swapped = True
            root.rename(tmp_path / "saved-repo")
            root.symlink_to(attacker, target_is_directory=True)
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(release_manifest.os, "open", swap_root_before_open)
    monkeypatch.setattr(
        release_manifest.os,
        "supports_dir_fd",
        {*release_manifest.os.supports_dir_fd, swap_root_before_open},
    )
    with pytest.raises(release_manifest.ManifestError, match="root is not a directory"):
        with release_manifest.SourceBundleReader(root):
            pass


def test_deployment_bundle_is_deterministic_and_rejects_tampering(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "findb")
    manifest = _manifest("findb")
    with release_manifest.SourceBundleReader(root) as source_bundle:
        manifest["deployment_source_bundle_sha256"] = source_bundle.deployment_source_bundle_sha256(
            "findb"
        )
    manifest_path = tmp_path / "release.json"
    manifest_path.write_bytes(release_manifest.canonical_json(manifest))
    first = release_manifest.build_bundle(root, manifest_path, "findb")
    second = release_manifest.build_bundle(root, manifest_path, "findb")
    assert first == second
    digest = hashlib.sha256(first).hexdigest()
    validated = release_manifest.validate_bundle_bytes(first, expected_sha256=digest, unit="findb")
    assert validated["images"] == manifest["images"]
    with pytest.raises(release_manifest.ManifestError, match="SHA256"):
        release_manifest.validate_bundle_bytes(first + b"x", expected_sha256=digest, unit="findb")
    output = tmp_path / "release"
    release_manifest.materialize_validated_bundle(
        first, expected_sha256=digest, unit="findb", output=output
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert (output / "infra/deploy/release_manifest.py").is_file()
    for relative in release_manifest.EXECUTABLE_BUNDLE_FILES:
        if relative in release_manifest.FINDB_BUNDLE_FILES:
            assert stat.S_IMODE((output / relative).stat().st_mode) == 0o755
    for relative in (
        "docker-compose.prod.yml",
        "infra/deploy/runtime-secrets/findb.json",
        "contracts/manifest.json",
    ):
        assert stat.S_IMODE((output / relative).stat().st_mode) == 0o644
    assert not (output / "release-manifest.json").exists()
    with pytest.raises(release_manifest.ManifestError, match="already exists"):
        release_manifest.materialize_validated_bundle(
            first, expected_sha256=digest, unit="findb", output=output
        )


def test_bundle_rejects_tampered_member_mode_and_materializes_runnable_tools(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "findb")
    for relative in release_manifest.EXECUTABLE_BUNDLE_FILES:
        if relative not in release_manifest.FINDB_BUNDLE_FILES:
            continue
        source = REPO_ROOT / relative
        if source.is_file():
            (root / relative).write_bytes(source.read_bytes())
    manifest = _manifest("findb")
    with release_manifest.SourceBundleReader(root) as source_bundle:
        manifest["deployment_source_bundle_sha256"] = source_bundle.deployment_source_bundle_sha256(
            "findb"
        )
    manifest_path = tmp_path / "release.json"
    manifest_path.write_bytes(release_manifest.canonical_json(manifest))
    bundle = release_manifest.build_bundle(root, manifest_path, "findb")

    tampered_stream = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle), mode="r:") as source_archive,
        tarfile.open(
            fileobj=tampered_stream, mode="w", format=tarfile.GNU_FORMAT
        ) as tampered_archive,
    ):
        for member in source_archive.getmembers():
            content = source_archive.extractfile(member)
            assert content is not None
            copied = tarfile.TarInfo(member.name)
            copied.size = member.size
            copied.mode = (
                0o644 if member.name == "infra/deploy/release_manifest.py" else member.mode
            )
            copied.uid = member.uid
            copied.gid = member.gid
            copied.mtime = member.mtime
            copied.uname = member.uname
            copied.gname = member.gname
            copied.type = member.type
            tampered_archive.addfile(copied, content)
    tampered = tampered_stream.getvalue()
    with pytest.raises(release_manifest.ManifestError, match="noncanonical"):
        release_manifest.validate_bundle_bytes(
            tampered,
            expected_sha256=hashlib.sha256(tampered).hexdigest(),
            unit="findb",
        )

    output = tmp_path / "runnable-release"
    release_manifest.materialize_validated_bundle(
        bundle,
        expected_sha256=hashlib.sha256(bundle).hexdigest(),
        unit="findb",
        output=output,
    )
    validator = output / "infra/deploy/release_manifest.py"
    protocol = subprocess.run([str(validator), "protocol"], capture_output=True, check=False)
    assert protocol.returncode == 0
    assert protocol.stdout == b"findb-release-manifest-v2\n"
    for relative in (
        "infra/deploy/runtime-secrets/deploy_findb_aws.sh",
        "infra/deploy/runtime-secrets/runtime_secret_command.sh",
        "infra/deploy/runtime-secrets/render_nginx_runtime.sh",
    ):
        tool = output / relative
        assert stat.S_IMODE(tool.stat().st_mode) == 0o755
        assert subprocess.run(["bash", "-n", str(tool)], check=False).returncode == 0


def test_deployment_bundle_resource_limits_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "findb")
    manifest = _manifest("findb")
    with release_manifest.SourceBundleReader(root) as source_bundle:
        manifest["deployment_source_bundle_sha256"] = source_bundle.deployment_source_bundle_sha256(
            "findb"
        )
    manifest_path = tmp_path / "release.json"
    manifest_path.write_bytes(release_manifest.canonical_json(manifest))
    bundle = release_manifest.build_bundle(root, manifest_path, "findb")
    digest = hashlib.sha256(bundle).hexdigest()
    for name, value, message in (
        ("MAX_BUNDLE_BYTES", len(bundle) - 1, "maximum permitted size"),
        ("MAX_BUNDLE_MEMBERS", 1, "too many members"),
        ("MAX_BUNDLE_MEMBER_BYTES", 1, "member exceeds"),
        ("MAX_BUNDLE_UNCOMPRESSED_BYTES", 1, "maximum uncompressed size"),
    ):
        monkeypatch.setattr(release_manifest, name, value)
        with pytest.raises(release_manifest.ManifestError, match=message):
            release_manifest.validate_bundle_bytes(bundle, expected_sha256=digest, unit="findb")
        monkeypatch.undo()


def test_bundle_materialization_rejects_symlinked_output_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "findb")
    manifest = _manifest("findb")
    with release_manifest.SourceBundleReader(root) as source_bundle:
        manifest["deployment_source_bundle_sha256"] = source_bundle.deployment_source_bundle_sha256(
            "findb"
        )
    manifest_path = tmp_path / "release.json"
    manifest_path.write_bytes(release_manifest.canonical_json(manifest))
    bundle = release_manifest.build_bundle(root, manifest_path, "findb")
    external = tmp_path / "external"
    external.mkdir()
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(release_manifest.ManifestError, match="ancestor is not a directory"):
        release_manifest.materialize_validated_bundle(
            bundle,
            expected_sha256=hashlib.sha256(bundle).hexdigest(),
            unit="findb",
            output=link_parent / "release",
        )
    assert not (external / "release").exists()


def test_contract_manifest_parser_rejects_malformed_or_ambiguous_entries(tmp_path: Path) -> None:
    valid = json.loads(CONTRACT_MANIFEST.read_text())
    contract_path = tmp_path / "contracts.json"
    variants = [
        {"manifest_version": 1, "contracts": valid["contracts"], "extra": True},
        {"manifest_version": 1},
        {"manifest_version": True, "contracts": valid["contracts"]},
        {
            "manifest_version": 1,
            "contracts": [{**valid["contracts"][0], "schema_version": 1.0}],
        },
        {
            "manifest_version": 1,
            "contracts": [{**valid["contracts"][0], "path": "other/v1.schema.json"}],
        },
        {
            "manifest_version": 1,
            "contracts": [{**valid["contracts"][0], "schema_id": "Not_safe"}],
        },
        {"manifest_version": 1, "contracts": [valid["contracts"][0], valid["contracts"][0]]},
    ]
    for variant in variants:
        contract_path.write_text(json.dumps(variant), encoding="utf-8")
        with pytest.raises(release_manifest.ManifestError):
            release_manifest.load_contract_versions(contract_path)
    contract_path.write_text(
        '{"manifest_version":1,"manifest_version":1,"contracts":[]}', encoding="utf-8"
    )
    with pytest.raises(release_manifest.ManifestError, match="duplicate JSON key"):
        release_manifest.load_contract_versions(contract_path)
    contract_path.write_text("{not JSON", encoding="utf-8")
    with pytest.raises(release_manifest.ManifestError, match="invalid contract manifest JSON"):
        release_manifest.load_contract_versions(contract_path)


def test_manifest_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(release_manifest.ManifestError, match="duplicate JSON key"):
        release_manifest.load_manifest(manifest_path)


def test_bundle_digest_is_sorted_and_rejects_symlink_or_content_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for relative in release_manifest.FINDB_BUNDLE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    first = release_manifest.deployment_source_bundle_sha256(root, "findb")
    assert first == release_manifest.deployment_source_bundle_sha256(root, "findb")
    changed = root / "contracts" / "manifest.json"
    changed.write_text("changed", encoding="utf-8")
    assert release_manifest.deployment_source_bundle_sha256(root, "findb") != first
    changed.write_text("contracts/manifest.json", encoding="utf-8")
    changed = root / release_manifest.FINDB_BUNDLE_FILES[-1]
    changed.unlink()
    changed.symlink_to(root / release_manifest.FINDB_BUNDLE_FILES[0])
    with pytest.raises(release_manifest.ManifestError, match="regular file"):
        release_manifest.deployment_source_bundle_sha256(root, "findb")


def test_bundle_allowlists_are_unit_scoped() -> None:
    assert release_manifest.FINDB_RUNTIME_SECRET_FILES == (
        "backend/scripts/render_nginx_cloudflare_real_ip.py",
        "backend/scripts/render_nginx_public_host.py",
        "backend/scripts/render_nginx_source_allowlist.py",
        "infra/deploy/runtime-secrets/deploy_findb_aws.sh",
        "infra/deploy/runtime-secrets/findb.json",
        "infra/deploy/runtime-secrets/install_findb_bootstrap.sh",
        "infra/deploy/runtime-secrets/render_nginx_runtime.sh",
        "infra/deploy/runtime-secrets/render_serve_key.py",
    )
    assert release_manifest.FETCHER_RUNTIME_SECRET_FILES == (
        "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh",
        "infra/deploy/runtime-secrets/fetcher.json",
        "infra/deploy/runtime-secrets/release_fetcher_provider.sh",
    )
    assert "contracts/manifest.json" in release_manifest.COMMON_BUNDLE_FILES
    assert "infra/deploy/runtime-secrets/build_ecr_image_if_missing.sh" not in (
        release_manifest.COMMON_BUNDLE_FILES
    )
    assert "infra/deploy/release_manifest.py" in release_manifest.FINDB_BUNDLE_FILES
    assert "infra/deploy/release_manifest.py" in release_manifest.FETCHER_BUNDLE_FILES
    assert "infra/deploy/runtime-secrets/deploy_findb_aws.sh" in release_manifest.FINDB_BUNDLE_FILES
    assert "infra/deploy/runtime-secrets/findb.json" in release_manifest.FINDB_BUNDLE_FILES
    assert "infra/deploy/runtime-secrets/release_fetcher_provider.sh" in (
        release_manifest.FETCHER_BUNDLE_FILES
    )
    assert "infra/deploy/runtime-secrets/fetcher.json" in release_manifest.FETCHER_BUNDLE_FILES
    assert "infra/deploy/runtime-secrets/release_fetcher_provider.sh" not in (
        release_manifest.FINDB_BUNDLE_FILES
    )
    assert "infra/deploy/runtime-secrets/fetcher.json" not in release_manifest.FINDB_BUNDLE_FILES
    assert "infra/deploy/runtime-secrets/deploy_findb_aws.sh" not in (
        release_manifest.FETCHER_BUNDLE_FILES
    )
    assert "infra/deploy/runtime-secrets/findb.json" not in release_manifest.FETCHER_BUNDLE_FILES
    assert "infra/nginx/serve-key.conf" not in release_manifest.FINDB_BUNDLE_FILES
    assert "backend/scripts/render_nginx_serve_key.py" not in release_manifest.FINDB_BUNDLE_FILES


def test_generate_is_canonical_and_validates_real_bundle(tmp_path: Path) -> None:
    output_one = tmp_path / "one.json"
    output_two = tmp_path / "two.json"
    args = [
        "generate",
        "--repo-root",
        str(REPO_ROOT),
        "--unit",
        "findb",
        "--commit-sha",
        COMMIT,
        "--migration-revision",
        "c" * 12,
        "--contract-manifest",
        str(CONTRACT_MANIFEST),
        "--image",
        f"dashboard={REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}",
        "--image",
        f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
        "--created-by-run-id",
        "123456",
        "--output",
    ]
    assert release_manifest.main([*args, str(output_one)]) == 0
    assert release_manifest.main([*args, str(output_two)]) == 0
    assert output_one.read_bytes() == output_two.read_bytes()
    assert (
        release_manifest.main(
            [
                "validate",
                "--repo-root",
                str(REPO_ROOT),
                "--unit",
                "findb",
                "--commit-sha",
                COMMIT,
                "--migration-revision",
                "c" * 12,
                "--contract-manifest",
                str(CONTRACT_MANIFEST),
                "--image",
                f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
                "--image",
                f"dashboard={REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}",
                "--created-by-run-id",
                "123456",
                "--manifest",
                str(output_one),
            ]
        )
        == 0
    )


def test_validate_cli_binds_every_expected_release_input(tmp_path: Path) -> None:
    output = tmp_path / "release.json"
    generate_arguments = [
        "generate",
        "--repo-root",
        str(REPO_ROOT),
        "--unit",
        "findb",
        "--commit-sha",
        COMMIT,
        "--migration-revision",
        "c" * 12,
        "--contract-manifest",
        str(CONTRACT_MANIFEST),
        "--image",
        f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
        "--image",
        f"dashboard={REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}",
        "--created-by-run-id",
        "123456",
        "--output",
        str(output),
    ]
    assert release_manifest.main(generate_arguments) == 0
    validate_arguments = [
        "validate",
        "--repo-root",
        str(REPO_ROOT),
        "--unit",
        "findb",
        "--commit-sha",
        COMMIT,
        "--migration-revision",
        "c" * 12,
        "--contract-manifest",
        str(CONTRACT_MANIFEST),
        "--image",
        f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
        "--image",
        f"dashboard={REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}",
        "--created-by-run-id",
        "123456",
        "--manifest",
        str(output),
    ]
    assert release_manifest.main(validate_arguments) == 0

    for option, replacement in (
        ("--commit-sha", "e" * 40),
        ("--migration-revision", "d" * 12),
        ("--created-by-run-id", "654321"),
    ):
        candidate = list(validate_arguments)
        candidate[candidate.index(option) + 1] = replacement
        assert release_manifest.main(candidate) == 1

    candidate = list(validate_arguments)
    candidate[candidate.index(f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}")] = (
        f"backend={REGISTRY}/findb/staging/backend@sha256:{'e' * 64}"
    )
    assert release_manifest.main(candidate) == 1

    unit_mismatch = list(validate_arguments)
    unit_mismatch[unit_mismatch.index("--unit") + 1] = "fetcher"
    assert release_manifest.main(unit_mismatch) == 1

    duplicate = [
        *validate_arguments[:-2],
        "--image",
        f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
        *validate_arguments[-2:],
    ]
    assert release_manifest.main(duplicate) == 1

    missing_expected = list(validate_arguments)
    run_id_index = missing_expected.index("--created-by-run-id")
    del missing_expected[run_id_index : run_id_index + 2]
    with pytest.raises(SystemExit) as missing_error:
        release_manifest.main(missing_expected)
    assert missing_error.value.code == 2


def test_manifest_output_atomically_replaces_existing_and_dangling_symlinks(
    tmp_path: Path,
) -> None:
    dangling = tmp_path / "dangling.json"
    missing_target = tmp_path / "does-not-exist.json"
    dangling.symlink_to(missing_target)
    release_manifest._write_manifest_atomically(dangling, b"dangling replacement\n")
    assert not dangling.is_symlink()
    assert dangling.read_bytes() == b"dangling replacement\n"
    assert not missing_target.exists()

    existing = tmp_path / "existing.json"
    external_target = tmp_path / "external.json"
    external_target.write_bytes(b"must not be followed\n")
    existing.symlink_to(external_target)
    release_manifest._write_manifest_atomically(existing, b"symlink replacement\n")
    assert not existing.is_symlink()
    assert existing.read_bytes() == b"symlink replacement\n"
    assert external_target.read_bytes() == b"must not be followed\n"


def test_manifest_output_uses_a_mode_600_temp_and_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "release.json"
    output.write_bytes(b"old\n")
    original_replace = release_manifest.os.replace
    replacements: list[tuple[Path, Path]] = []

    def record_replace(source: str | Path, destination: str | Path, **kwargs: object) -> None:
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(release_manifest.os, "replace", record_replace)
    release_manifest._write_manifest_atomically(output, b"new\n")
    assert len(replacements) == 1
    assert replacements[0][1] == Path(output.name)
    assert replacements[0][0].parent == Path(".")
    assert replacements[0][0].name.startswith(".release.json.tmp-")
    assert output.read_bytes() == b"new\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_manifest_output_rejects_directory_and_cleans_temporary_file_on_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "manifest-directory"
    directory.mkdir()
    with pytest.raises(release_manifest.ManifestError, match="regular file or symlink"):
        release_manifest._write_manifest_atomically(directory, b"nope\n")

    output = tmp_path / "release.json"
    output.write_bytes(b"unchanged\n")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(release_manifest.os, "fsync", fail_fsync)
    with pytest.raises(release_manifest.ManifestError, match="securely access or write"):
        release_manifest._write_manifest_atomically(output, b"partial\n")
    assert output.read_bytes() == b"unchanged\n"
    assert list(tmp_path.glob(".release.json.tmp-*")) == []


@pytest.mark.parametrize("capability", ("O_NOFOLLOW", "O_DIRECTORY", "dir_fd"))
def test_manifest_output_rejects_unsafe_parent_and_missing_descriptor_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(external, target_is_directory=True)
    with pytest.raises(release_manifest.ManifestError, match="securely access"):
        release_manifest._write_manifest_atomically(linked_parent / "release.json", b"nope\n")

    ancestor_link = tmp_path / "ancestor-link"
    ancestor_link.symlink_to(external, target_is_directory=True)
    with pytest.raises(release_manifest.ManifestError, match="securely access"):
        release_manifest._write_manifest_atomically(
            ancestor_link / "nested" / "release.json",
            b"nope\n",
        )
    assert not (external / "nested" / "release.json").exists()

    missing_parent = tmp_path / "missing" / "nested"
    with pytest.raises(release_manifest.ManifestError, match="securely access"):
        release_manifest._write_manifest_atomically(missing_parent / "release.json", b"nope\n")
    assert not missing_parent.exists()

    non_directory = tmp_path / "not-a-directory"
    non_directory.write_bytes(b"nope\n")
    with pytest.raises(release_manifest.ManifestError, match="securely access"):
        release_manifest._write_manifest_atomically(non_directory / "release.json", b"nope\n")

    if capability == "dir_fd":
        monkeypatch.setattr(
            release_manifest.os,
            "supports_dir_fd",
            set(release_manifest.os.supports_dir_fd) - {release_manifest.os.open},
        )
    else:
        monkeypatch.delattr(release_manifest.os, capability)
    with pytest.raises(release_manifest.ManifestError, match="unavailable"):
        release_manifest._write_manifest_atomically(tmp_path / "release.json", b"nope\n")


def test_manifest_output_parent_fd_survives_parent_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "ancestor" / "parent"
    parent.parent.mkdir()
    parent.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    output = parent / "release.json"
    original_open = release_manifest.os.open
    swapped = False

    def swap_parent_after_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "ancestor" and dir_fd is not None and not swapped:
            swapped = True
            ancestor = parent.parent
            ancestor.rename(tmp_path / "saved-ancestor")
            ancestor.symlink_to(external, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(release_manifest.os, "open", swap_parent_after_open)
    monkeypatch.setattr(
        release_manifest.os,
        "supports_dir_fd",
        {*release_manifest.os.supports_dir_fd, swap_parent_after_open},
    )
    release_manifest._write_manifest_atomically(output, b"pinned parent\n")
    saved_parent = tmp_path / "saved-ancestor" / "parent"
    assert (saved_parent / "release.json").read_bytes() == b"pinned parent\n"
    assert not (external / "parent" / "release.json").exists()
    assert list(saved_parent.glob(".release.json.tmp-*")) == []


@pytest.mark.parametrize("attack", ("hardlink", "mutation"))
def test_manifest_output_rejects_temp_races_and_cleans_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    output = tmp_path / "release.json"
    output.write_bytes(b"old\n")
    original_open = release_manifest.os.open
    original_fsync = release_manifest.os.fsync
    original_close = release_manifest.os.close
    parent_fd: int | None = None
    temporary_fd: int | None = None
    temporary_name: str | None = None
    closed: list[int] = []

    def record_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal parent_fd, temporary_fd, temporary_name
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == tmp_path.name and dir_fd is not None:
            parent_fd = descriptor
        elif (
            dir_fd == parent_fd and isinstance(path, str) and path.startswith(".release.json.tmp-")
        ):
            temporary_name = path
            temporary_fd = descriptor
        return descriptor

    def inject_race(descriptor: int) -> None:
        if descriptor == parent_fd:
            original_fsync(descriptor)
            return
        assert parent_fd is not None and temporary_name is not None
        if attack == "hardlink":
            os.link(
                temporary_name,
                "attacker-link",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        else:
            os.write(descriptor, b"x")
        original_fsync(descriptor)

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(release_manifest.os, "open", record_open)
    monkeypatch.setattr(release_manifest.os, "fsync", inject_race)
    monkeypatch.setattr(release_manifest.os, "close", record_close)
    monkeypatch.setattr(
        release_manifest.os,
        "supports_dir_fd",
        {*release_manifest.os.supports_dir_fd, record_open},
    )
    with pytest.raises(release_manifest.ManifestError, match="temporary file"):
        release_manifest._write_manifest_atomically(output, b"new\n")
    assert output.read_bytes() == b"old\n"
    assert list(tmp_path.glob(".release.json.tmp-*")) == []
    assert parent_fd in closed
    assert temporary_fd in closed
    if attack == "hardlink":
        (tmp_path / "attacker-link").unlink()


def test_manifest_output_cleans_temp_on_replace_failure_and_reports_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "release.json"
    output.write_bytes(b"old\n")

    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(release_manifest.os, "replace", fail_replace)
    with pytest.raises(release_manifest.ManifestError, match="securely access or write"):
        release_manifest._write_manifest_atomically(output, b"new\n")
    assert output.read_bytes() == b"old\n"
    assert list(tmp_path.glob(".release.json.tmp-*")) == []

    monkeypatch.undo()
    original_open = release_manifest.os.open
    original_fsync = release_manifest.os.fsync
    parent_fd: int | None = None

    def record_parent_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal parent_fd
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == tmp_path.name and dir_fd is not None:
            parent_fd = descriptor
        return descriptor

    def fail_parent_fsync(descriptor: int) -> None:
        if descriptor == parent_fd:
            raise OSError("simulated directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(release_manifest.os, "open", record_parent_open)
    monkeypatch.setattr(release_manifest.os, "fsync", fail_parent_fsync)
    monkeypatch.setattr(
        release_manifest.os,
        "supports_dir_fd",
        {*release_manifest.os.supports_dir_fd, record_parent_open},
    )
    with pytest.raises(release_manifest.ManifestError, match="securely access or write"):
        release_manifest._write_manifest_atomically(output, b"new\n")
    assert output.read_bytes() == b"new\n"
    assert list(tmp_path.glob(".release.json.tmp-*")) == []


def test_manifest_cli_rejects_non_file_bundle_input(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "fetcher")
    target = root / release_manifest.FETCHER_BUNDLE_FILES[-1]
    target.unlink()
    target.symlink_to(root / release_manifest.FETCHER_BUNDLE_FILES[1])
    result = subprocess.run(
        [
            sys.executable,
            str(TOOL_PATH),
            "generate",
            "--repo-root",
            str(root),
            "--unit",
            "fetcher",
            "--commit-sha",
            COMMIT,
            "--migration-revision",
            "none",
            "--contract-manifest",
            str(root / "contracts" / "manifest.json"),
            "--image",
            f"twelve_data={REGISTRY}/findb/staging/fetcher/twelve-data@sha256:{DIGEST}",
            "--image",
            f"finlab={REGISTRY}/findb/staging/fetcher/finlab@sha256:{DIGEST}",
            "--image",
            f"shioaji={REGISTRY}/findb/staging/fetcher/shioaji@sha256:{DIGEST}",
            "--created-by-run-id",
            "123",
            "--output",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "regular file" in result.stderr


def test_manifest_cli_rejects_a_symlinked_repo_root_for_generate_and_validate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "fetcher")
    contract_manifest = root / "contracts" / "manifest.json"
    linked_root = tmp_path / "linked-repo"
    linked_root.symlink_to(root, target_is_directory=True)

    def generate_arguments(repo_root: Path, output: Path) -> list[str]:
        return [
            "generate",
            "--repo-root",
            str(repo_root),
            "--unit",
            "fetcher",
            "--commit-sha",
            COMMIT,
            "--migration-revision",
            "none",
            "--contract-manifest",
            str(contract_manifest),
            "--image",
            f"twelve_data={REGISTRY}/findb/staging/fetcher/twelve-data@sha256:{DIGEST}",
            "--image",
            f"finlab={REGISTRY}/findb/staging/fetcher/finlab@sha256:{DIGEST}",
            "--image",
            f"shioaji={REGISTRY}/findb/staging/fetcher/shioaji@sha256:{DIGEST}",
            "--created-by-run-id",
            "123",
            "--output",
            str(output),
        ]

    rejected_generate = subprocess.run(
        [sys.executable, str(TOOL_PATH), *generate_arguments(linked_root, tmp_path / "bad.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected_generate.returncode == 1
    assert "root is not a directory" in rejected_generate.stderr

    manifest = tmp_path / "release.json"
    assert release_manifest.main(generate_arguments(root, manifest)) == 0
    rejected_validate = subprocess.run(
        [
            sys.executable,
            str(TOOL_PATH),
            "validate",
            "--repo-root",
            str(linked_root),
            "--unit",
            "fetcher",
            "--commit-sha",
            COMMIT,
            "--migration-revision",
            "none",
            "--contract-manifest",
            str(contract_manifest),
            "--image",
            f"twelve_data={REGISTRY}/findb/staging/fetcher/twelve-data@sha256:{DIGEST}",
            "--image",
            f"finlab={REGISTRY}/findb/staging/fetcher/finlab@sha256:{DIGEST}",
            "--image",
            f"shioaji={REGISTRY}/findb/staging/fetcher/shioaji@sha256:{DIGEST}",
            "--created-by-run-id",
            "123",
            "--manifest",
            str(manifest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected_validate.returncode == 1
    assert "root is not a directory" in rejected_validate.stderr


def test_bundle_checksum_rejects_external_hardlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for relative in release_manifest.FETCHER_BUNDLE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    target = root / release_manifest.FETCHER_BUNDLE_FILES[-1]
    external = tmp_path / "external-helper"
    external.write_text("external hardlink", encoding="utf-8")
    target.unlink()
    os.link(external, target)
    with pytest.raises(release_manifest.ManifestError, match="regular file"):
        release_manifest.deployment_source_bundle_sha256(root, "fetcher")


def test_bundle_checksum_rejects_directory_input(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for relative in release_manifest.FETCHER_BUNDLE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    (root / "contracts" / "manifest.json").write_bytes(CONTRACT_MANIFEST.read_bytes())
    directory = root / release_manifest.FETCHER_BUNDLE_FILES[-1]
    directory.unlink()
    directory.mkdir()
    with pytest.raises(release_manifest.ManifestError, match="regular file"):
        release_manifest.deployment_source_bundle_sha256(root, "fetcher")


@pytest.mark.parametrize("parent_kind", ("symlink", "non_directory"))
def test_source_bundle_rejects_unsafe_parent_paths(tmp_path: Path, parent_kind: str) -> None:
    root = tmp_path / "repo"
    for relative in release_manifest.FETCHER_BUNDLE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    if parent_kind == "symlink":
        external = tmp_path / "external"
        external.mkdir()
        (root / "infra").rename(root / "saved-infra")
        (root / "infra").symlink_to(external, target_is_directory=True)
    else:
        (root / "infra").rename(root / "saved-infra")
        (root / "infra").write_text("not a directory", encoding="utf-8")
    with pytest.raises(release_manifest.ManifestError, match="source bundle"):
        release_manifest.deployment_source_bundle_sha256(root, "fetcher")


def test_source_bundle_rejects_in_place_leaf_mutation_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    leaf = root / "contracts" / "manifest.json"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("first", encoding="utf-8")
    original_read = release_manifest.os.read
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, size)
        if chunk and not mutated:
            mutated = True
            leaf.write_text("in-place mutation with another size", encoding="utf-8")
        return chunk

    monkeypatch.setattr(release_manifest.os, "read", mutate_after_read)
    with pytest.raises(release_manifest.ManifestError, match="changed while reading"):
        release_manifest._safe_source_file_bytes(root, "contracts/manifest.json")


def test_source_bundle_rejects_a_symlinked_root(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual"
    (actual_root / "contracts").mkdir(parents=True)
    (actual_root / "contracts" / "manifest.json").write_text("safe", encoding="utf-8")
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    with pytest.raises(release_manifest.ManifestError, match="root is not a directory"):
        release_manifest._safe_source_file_bytes(linked_root, "contracts/manifest.json")


def test_source_bundle_descriptor_walk_stays_bound_across_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    original_parent = root / "contracts"
    original_parent.mkdir(parents=True)
    (original_parent / "manifest.json").write_text("selected", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "manifest.json").write_text("attacker", encoding="utf-8")
    original_open = release_manifest.os.open
    swapped = False

    def swap_after_parent_open(path: str | Path, flags: int, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        if path == "contracts" and not swapped:
            swapped = True
            original_parent.rename(root / "saved-contracts")
            original_parent.symlink_to(external, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(release_manifest.os, "open", swap_after_parent_open)
    monkeypatch.setattr(
        release_manifest.os,
        "supports_dir_fd",
        {*release_manifest.os.supports_dir_fd, swap_after_parent_open},
    )
    assert release_manifest._safe_source_file_bytes(root, "contracts/manifest.json") == b"selected"


def test_bundle_checksum_keeps_one_open_root_across_a_root_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    for relative in release_manifest.FETCHER_BUNDLE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    expected = release_manifest.deployment_source_bundle_sha256(root, "fetcher")
    original_open = release_manifest.os.open
    swapped = False

    def swap_root_after_first_leaf(
        path: str | Path, flags: int, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, dir_fd=dir_fd)
        if path == "manifest.json" and dir_fd is not None and not swapped:
            swapped = True
            root.rename(tmp_path / "saved-repo")
            root.mkdir()
            (root / "contracts").mkdir()
            (root / "contracts" / "manifest.json").write_text("attacker", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(release_manifest.os, "open", swap_root_after_first_leaf)
    monkeypatch.setattr(
        release_manifest.os,
        "supports_dir_fd",
        {*release_manifest.os.supports_dir_fd, swap_root_after_first_leaf},
    )
    assert release_manifest.deployment_source_bundle_sha256(root, "fetcher") == expected


def test_selected_source_snapshot_caches_contract_and_bundle_bytes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "fetcher")
    original_open = release_manifest.os.open
    manifest_opens = 0

    def reject_second_manifest_open(
        path: str | Path,
        flags: int,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal manifest_opens
        if path == "manifest.json" and dir_fd is not None:
            manifest_opens += 1
            if manifest_opens > 1:
                raise OSError("contract manifest must come from the frozen snapshot")
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(release_manifest.os, "open", reject_second_manifest_open)
    monkeypatch.setattr(
        release_manifest.os,
        "supports_dir_fd",
        {*release_manifest.os.supports_dir_fd, reject_second_manifest_open},
    )
    with release_manifest.SourceBundleReader(root) as source_bundle:
        contract_manifest = source_bundle.load_selected_contract_manifest(
            root / "contracts" / "manifest.json"
        )
        bundle_digest = source_bundle.deployment_source_bundle_sha256("fetcher")
        manifest = _manifest("fetcher")
        manifest["contract_versions"] = list(contract_manifest.versions)
        manifest["contract_manifest_sha256"] = contract_manifest.canonical_sha256
        manifest["deployment_source_bundle_sha256"] = bundle_digest
        release_manifest.validate_manifest(manifest, contract_manifest, source_bundle)
        expected_snapshot_paths = set(release_manifest.bundle_paths("fetcher")) | {
            f"contracts/{contract['path']}" for contract in CONTRACT_DATA.parsed["contracts"]
        }
        assert source_bundle.snapshot_paths == expected_snapshot_paths
    assert manifest_opens == 1


@pytest.mark.parametrize(
    "alias",
    ("./contracts/manifest.json", "contracts/./manifest.json", "contracts/x/../manifest.json"),
)
def test_selected_source_snapshot_rejects_noncanonical_cache_key_aliases(
    tmp_path: Path,
    alias: str,
) -> None:
    root = tmp_path / "repo"
    _write_selected_source_bundle(root, "fetcher")
    with release_manifest.SourceBundleReader(root) as source_bundle:
        assert source_bundle.read("contracts/manifest.json") == CONTRACT_MANIFEST.read_bytes()
        with pytest.raises(release_manifest.ManifestError, match="invalid source bundle path"):
            source_bundle.read(alias)
        assert source_bundle.snapshot_paths == frozenset({"contracts/manifest.json"})


@pytest.mark.parametrize("capability", ("O_NOFOLLOW", "O_DIRECTORY", "dir_fd"))
def test_source_bundle_requires_descriptor_safety_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    root = tmp_path / "repo"
    (root / "contracts").mkdir(parents=True)
    (root / "contracts" / "manifest.json").write_text("safe", encoding="utf-8")
    if capability == "dir_fd":
        monkeypatch.setattr(release_manifest.os, "supports_dir_fd", set())
    else:
        monkeypatch.delattr(release_manifest.os, capability)
    with pytest.raises(release_manifest.ManifestError, match="unavailable"):
        release_manifest._safe_source_file_bytes(root, "contracts/manifest.json")


def test_source_bundle_closes_every_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "contracts").mkdir(parents=True)
    (root / "contracts" / "manifest.json").write_text("safe", encoding="utf-8")
    original_close = release_manifest.os.close
    closed: list[int] = []

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(release_manifest.os, "close", record_close)
    assert release_manifest._safe_source_file_bytes(root, "contracts/manifest.json") == b"safe"
    assert len(closed) == 3
    assert len(set(closed)) == 3


def test_generate_rejects_duplicate_image_name(tmp_path: Path) -> None:
    result = release_manifest.main(
        [
            "generate",
            "--repo-root",
            str(REPO_ROOT),
            "--unit",
            "findb",
            "--commit-sha",
            COMMIT,
            "--migration-revision",
            "c" * 12,
            "--contract-manifest",
            str(CONTRACT_MANIFEST),
            "--image",
            f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
            "--image",
            f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
            "--image",
            f"dashboard={REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}",
            "--created-by-run-id",
            "123",
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    assert result == 1


def test_validate_rejects_a_different_selected_contract_manifest(tmp_path: Path) -> None:
    output = tmp_path / "release.json"
    assert (
        release_manifest.main(
            [
                "generate",
                "--repo-root",
                str(REPO_ROOT),
                "--unit",
                "findb",
                "--commit-sha",
                COMMIT,
                "--migration-revision",
                "c" * 12,
                "--contract-manifest",
                str(CONTRACT_MANIFEST),
                "--image",
                f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
                "--image",
                f"dashboard={REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}",
                "--created-by-run-id",
                "123",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    different_contracts = tmp_path / "selected-contracts.json"
    changed = json.loads(CONTRACT_MANIFEST.read_text())
    changed["contracts"][0]["sha256"] = "e" * 64
    different_contracts.write_text(json.dumps(changed), encoding="utf-8")
    assert (
        release_manifest.main(
            [
                "validate",
                "--repo-root",
                str(REPO_ROOT),
                "--unit",
                "findb",
                "--commit-sha",
                COMMIT,
                "--migration-revision",
                "c" * 12,
                "--contract-manifest",
                str(different_contracts),
                "--image",
                f"backend={REGISTRY}/findb/staging/backend@sha256:{DIGEST}",
                "--image",
                f"dashboard={REGISTRY}/findb/staging/dashboard@sha256:{DIGEST}",
                "--created-by-run-id",
                "123",
                "--manifest",
                str(output),
            ]
        )
        == 1
    )
