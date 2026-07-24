"""Export deterministic ingress contract artifacts from the application registry."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "contracts"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.ingress_contracts import (  # noqa: E402
    get_contract_json_schema,
    supported_contracts,
)

MANIFEST_VERSION = 1


def _json_bytes(value: Any) -> bytes:
    """Serialize an artifact with stable key ordering and a trailing newline."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def build_artifacts() -> dict[Path, bytes]:
    """Build every generated artifact without touching the filesystem."""
    artifacts: dict[Path, bytes] = {}
    contracts: list[dict[str, Any]] = []

    for schema_id, schema_version in supported_contracts():
        relative_path = Path(schema_id) / f"v{schema_version}.schema.json"
        schema_bytes = _json_bytes(get_contract_json_schema(schema_id, schema_version))
        artifacts[relative_path] = schema_bytes
        contracts.append(
            {
                "path": relative_path.as_posix(),
                "schema_id": schema_id,
                "schema_version": schema_version,
                "sha256": hashlib.sha256(schema_bytes).hexdigest(),
            }
        )

    artifacts[Path("manifest.json")] = _json_bytes(
        {
            "manifest_version": MANIFEST_VERSION,
            "contracts": contracts,
        }
    )
    return artifacts


def export_artifacts(output_dir: Path) -> tuple[Path, ...]:
    """Write the current registry artifacts and remove stale generated schemas."""
    expected = build_artifacts()
    output_dir.mkdir(parents=True, exist_ok=True)

    expected_schema_paths = {
        output_dir / relative_path
        for relative_path in expected
        if relative_path.name.endswith(".schema.json")
    }
    for existing_path in output_dir.glob("**/*.schema.json"):
        if existing_path not in expected_schema_paths:
            existing_path.unlink()

    written: list[Path] = []
    for relative_path, content in expected.items():
        destination = output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        written.append(destination)

    return tuple(written)


def check_artifacts(output_dir: Path) -> tuple[str, ...]:
    """Return deterministic drift diagnostics without modifying artifacts."""
    expected = build_artifacts()
    diagnostics: list[str] = []

    for relative_path, expected_content in expected.items():
        artifact_path = output_dir / relative_path
        if not artifact_path.is_file():
            diagnostics.append(f"missing artifact: {relative_path.as_posix()}")
            continue
        if artifact_path.read_bytes() != expected_content:
            diagnostics.append(f"artifact differs: {relative_path.as_posix()}")

    expected_paths = {relative_path.as_posix() for relative_path in expected}
    if output_dir.is_dir():
        actual_paths = {
            path.relative_to(output_dir).as_posix()
            for path in output_dir.rglob("*")
            if path.is_file()
        }
        for unexpected_path in sorted(actual_paths - expected_paths):
            diagnostics.append(f"unexpected artifact: {unexpected_path}")

    return tuple(diagnostics)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export checked-in JSON Schema artifacts from the ingress registry."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Artifact directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail without writing when checked-in artifacts do not match the registry.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()

    if args.check:
        diagnostics = check_artifacts(output_dir)
        if diagnostics:
            for diagnostic in diagnostics:
                print(diagnostic, file=sys.stderr)
            print(
                "Ingress contract artifacts are out of date; run "
                "backend/scripts/export_ingress_contracts.py.",
                file=sys.stderr,
            )
            return 1
        print(f"Ingress contract artifacts are current: {output_dir}")
        return 0

    written = export_artifacts(output_dir)
    contract_count = len(written) - 1
    print(f"Exported {contract_count} ingress contract artifacts to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
