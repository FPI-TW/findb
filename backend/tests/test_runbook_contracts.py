"""Operator runbooks must only reference scripts and IaC objects that exist."""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "infra" / "tofu" / "README.md",
    *sorted((REPO_ROOT / "docs").rglob("*.md")),
)
TOFU_ROOT = REPO_ROOT / "infra" / "tofu"


def _tofu_sources() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in TOFU_ROOT.rglob("*.tf"))


def _missing_references(document: str) -> list[str]:
    missing: list[str] = []
    for name in re.findall(r"(?<![\w/.-])(?:backend/)?scripts/(\w+\.py)", document):
        if not any(
            (REPO_ROOT / unit / "scripts" / name).is_file() for unit in ("backend", "fetcher")
        ):
            missing.append(f"scripts/{name}")
    for name in re.findall(r"/app/scripts/(\w+\.py)", document):
        if not (REPO_ROOT / "backend" / "scripts" / name).is_file():
            missing.append(f"/app/scripts/{name}")
    for name in re.findall(r"/opt/findb/runtime-secrets/([\w.-]+)", document):
        if not (REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / name).is_file():
            missing.append(f"/opt/findb/runtime-secrets/{name}")
    for name in re.findall(r"-chdir=infra/tofu/([\w-]+)", document):
        if not (TOFU_ROOT / name).is_dir():
            missing.append(f"infra/tofu/{name}")
    sources = None
    for kind, name in re.findall(r"-replace='(\w+)\.(\w+)'", document):
        sources = sources if sources is not None else _tofu_sources()
        if f'resource "{kind}" "{name}"' not in sources:
            missing.append(f"{kind}.{name}")
    for name in re.findall(r"\btofu -chdir=\S+ output -json (\w+)", document):
        sources = sources if sources is not None else _tofu_sources()
        if f'output "{name}"' not in sources:
            missing.append(f"output {name}")
    return missing


@pytest.mark.parametrize("runbook", RUNBOOKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_runbook_references_resolve_to_repository_objects(runbook: Path) -> None:
    assert _missing_references(runbook.read_text(encoding="utf-8")) == []


def test_reference_scanner_detects_missing_objects() -> None:
    assert _missing_references(
        "uv run python scripts/does_not_exist.py\n"
        "/opt/findb/runtime-secrets/missing.sh\n"
        "tofu -chdir=infra/tofu/missing plan -replace='aws_sns_topic.missing'\n"
    ) == [
        "scripts/does_not_exist.py",
        "/opt/findb/runtime-secrets/missing.sh",
        "infra/tofu/missing",
        "aws_sns_topic.missing",
    ]
