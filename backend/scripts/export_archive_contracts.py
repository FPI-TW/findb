"""Export deterministic standalone minute archive contract artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "contracts" / "archive"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.archive_contracts import get_archive_contract_json_schema  # noqa: E402


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, separators=(",", ": "))
        + "\n"
    ).encode("utf-8")


def build_artifacts() -> dict[Path, bytes]:
    relative_path = Path("market_minute_archive") / "v1.schema.json"
    schema_bytes = _json_bytes(get_archive_contract_json_schema())
    return {
        relative_path: schema_bytes,
        Path("manifest.json"): _json_bytes(
            {
                "manifest_version": 1,
                "contracts": [
                    {
                        "path": relative_path.as_posix(),
                        "schema_id": "market_minute_archive",
                        "schema_version": 1,
                        "sha256": hashlib.sha256(schema_bytes).hexdigest(),
                    }
                ],
            }
        ),
    }


def export_artifacts(output_dir: Path) -> tuple[Path, ...]:
    expected = build_artifacts()
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in expected.items():
        destination = output_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return tuple(output_dir / path for path in expected)


def check_artifacts(output_dir: Path) -> tuple[str, ...]:
    expected = build_artifacts()
    diagnostics: list[str] = []
    for relative_path, content in expected.items():
        path = output_dir / relative_path
        if not path.is_file():
            diagnostics.append(f"missing artifact: {relative_path.as_posix()}")
        elif path.read_bytes() != content:
            diagnostics.append(f"artifact differs: {relative_path.as_posix()}")
    expected_paths = {path.as_posix() for path in expected}
    for path in output_dir.rglob("*") if output_dir.is_dir() else ():
        relative_path = path.relative_to(output_dir).as_posix()
        if path.is_file() and not path.name.startswith(".") and relative_path not in expected_paths:
            diagnostics.append(f"unexpected artifact: {relative_path}")
    return tuple(sorted(diagnostics))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export standalone archive contract artifacts.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    if args.check:
        diagnostics = check_artifacts(output_dir)
        if diagnostics:
            print("\n".join(diagnostics), file=sys.stderr)
            return 1
        return 0
    export_artifacts(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
