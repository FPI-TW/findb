from __future__ import annotations

import ast
from pathlib import Path


def test_fetcher_does_not_import_backend_application() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    forbidden: list[str] = []

    for source_file in source_root.rglob("*.py"):
        tree = ast.parse(source_file.read_text(), filename=str(source_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if (
                    name == "backend"
                    or name.startswith("backend.")
                    or name == "app"
                    or name.startswith("app.")
                ):
                    forbidden.append(f"{source_file.relative_to(source_root)} imports {name}")

    assert forbidden == []
