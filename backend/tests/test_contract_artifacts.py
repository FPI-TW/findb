"""Tests for the checked-in ingress contract artifacts."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from app.services.ingress_contracts import get_contract_json_schema, supported_contracts
from scripts.export_archive_contracts import (
    check_artifacts as check_archive_artifacts,
)
from scripts.export_archive_contracts import (
    export_artifacts as export_archive_artifacts,
)
from scripts.export_ingress_contracts import check_artifacts, export_artifacts

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
CONTRACTS_ROOT = REPO_ROOT / "contracts"
EXPORT_SCRIPT = BACKEND_ROOT / "scripts" / "export_ingress_contracts.py"


def test_checked_in_artifacts_match_registry() -> None:
    assert check_artifacts(CONTRACTS_ROOT) == ()

    manifest = json.loads((CONTRACTS_ROOT / "manifest.json").read_text(encoding="utf-8"))
    expected_contracts = list(supported_contracts())
    assert [
        (entry["schema_id"], entry["schema_version"]) for entry in manifest["contracts"]
    ] == expected_contracts

    for entry in manifest["contracts"]:
        artifact_path = CONTRACTS_ROOT / entry["path"]
        artifact_bytes = artifact_path.read_bytes()
        assert hashlib.sha256(artifact_bytes).hexdigest() == entry["sha256"]
        assert json.loads(artifact_bytes) == get_contract_json_schema(
            entry["schema_id"], entry["schema_version"]
        )


def test_repeated_export_is_deterministic(tmp_path: Path) -> None:
    output_dir = tmp_path / "contracts"

    export_artifacts(output_dir)
    first_export = {
        path.relative_to(output_dir): path.read_bytes()
        for path in output_dir.rglob("*")
        if path.is_file()
    }
    export_artifacts(output_dir)
    second_export = {
        path.relative_to(output_dir): path.read_bytes()
        for path in output_dir.rglob("*")
        if path.is_file()
    }

    assert second_export == first_export
    assert check_artifacts(output_dir) == ()


def test_check_command_fails_for_changed_and_missing_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "contracts"
    export_artifacts(output_dir)
    (output_dir / "market_eod" / "v1.schema.json").write_text(
        '{"changed":true}\n',
        encoding="utf-8",
    )
    (output_dir / "market_minute" / "v1.schema.json").unlink()

    result = subprocess.run(
        [
            sys.executable,
            str(EXPORT_SCRIPT),
            "--output-dir",
            str(output_dir),
            "--check",
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "artifact differs: market_eod/v1.schema.json" in result.stderr
    assert "missing artifact: market_minute/v1.schema.json" in result.stderr
    assert (output_dir / "market_eod" / "v1.schema.json").read_text(
        encoding="utf-8"
    ) == '{"changed":true}\n'
    assert not (output_dir / "futures_continuous_eod" / "v1.schema.json").exists()


def test_check_rejects_unexpected_non_schema_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "contracts"
    export_artifacts(output_dir)
    (output_dir / "unexpected.txt").write_text("not generated\n", encoding="utf-8")
    nested_file = output_dir / "nested" / "extra.json"
    nested_file.parent.mkdir()
    nested_file.write_text('{"not":"generated"}\n', encoding="utf-8")

    assert check_artifacts(output_dir) == (
        "unexpected artifact: nested/extra.json",
        "unexpected artifact: unexpected.txt",
    )


def test_check_ignores_os_metadata_and_independent_archive_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "contracts"
    export_artifacts(output_dir)
    (output_dir / ".DS_Store").write_bytes(b"metadata")
    archive_path = output_dir / "archive" / "market_minute_archive" / "v1.schema.json"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_text("{}\n", encoding="utf-8")

    assert check_artifacts(output_dir) == ()


def test_archive_export_is_deterministic_and_reports_drift(tmp_path: Path) -> None:
    output_dir = tmp_path / "archive"
    export_archive_artifacts(output_dir)
    first = {
        path.relative_to(output_dir): path.read_bytes()
        for path in output_dir.rglob("*")
        if path.is_file()
    }
    export_archive_artifacts(output_dir)
    second = {
        path.relative_to(output_dir): path.read_bytes()
        for path in output_dir.rglob("*")
        if path.is_file()
    }
    assert first == second
    assert check_archive_artifacts(output_dir) == ()

    (output_dir / "market_minute_archive" / "v1.schema.json").write_text("{}\n", encoding="utf-8")
    assert check_archive_artifacts(output_dir) == (
        "artifact differs: market_minute_archive/v1.schema.json",
    )
