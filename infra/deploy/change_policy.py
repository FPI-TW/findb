#!/usr/bin/env python3
"""Classify an exact revision range for CI, staging rollout and infra planning.

The policy is intentionally a small stdlib program: all workflows consume the
same truth table instead of maintaining subtly different path filters.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

OUTPUTS = ("findb_ci", "fetcher_ci", "findb_staging", "fetcher_staging", "infra_plan")

FINDB_PREFIXES = ("backend/", "dashboard/", "infra/nginx/")
FETCHER_PREFIXES = ("fetcher/",)
SHARED_PREFIXES = (
    "contracts/",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "infra/deploy/",
)
INFRA_PREFIXES = ("infra/tofu/", "infra/env/")
FINDB_STAGING_WORKFLOWS = {
    ".github/workflows/findb-cd.yml",
    ".github/workflows/findb-deploy.yml",
}
FETCHER_STAGING_WORKFLOWS = {
    ".github/workflows/fetcher-cd.yml",
    ".github/workflows/fetcher-deploy.yml",
}


class PolicyError(ValueError):
    pass


def classify(paths: list[str]) -> dict[str, bool]:
    result = dict.fromkeys(OUTPUTS, False)
    for path in paths:
        if not path:
            continue
        if path.startswith(".github/workflows/"):
            # Workflow changes are shared delivery policy and require both CI
            # suites. A unit's staging caller or reusable deployment workflow
            # must also exercise that exact changed path on staging; CI and
            # production-control-plane changes remain non-deploying.
            result["findb_ci"] = result["fetcher_ci"] = True
            if path in FINDB_STAGING_WORKFLOWS:
                result["findb_staging"] = True
            if path in FETCHER_STAGING_WORKFLOWS:
                result["fetcher_staging"] = True
            continue
        if path.startswith("contracts/"):
            result["findb_ci"] = result["fetcher_ci"] = True
            continue
        if path.startswith(FINDB_PREFIXES):
            result["findb_ci"] = result["findb_staging"] = True
        if path.startswith(FETCHER_PREFIXES):
            result["fetcher_ci"] = result["fetcher_staging"] = True
        if path.startswith(SHARED_PREFIXES):
            result["findb_ci"] = result["fetcher_ci"] = True
            # Deploy helpers and compose contracts affect the host runtime.
            if path.startswith("infra/deploy/") or path == "docker-compose.prod.yml":
                result["findb_staging"] = result["fetcher_staging"] = True
        if path.startswith(INFRA_PREFIXES):
            result["infra_plan"] = True
    return result


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    for value in (base, head):
        if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value.lower()):
            raise PolicyError("revision_invalid")
    completed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "--no-renames", base, head],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise PolicyError("revision_range_unavailable")
    return completed.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        output = classify(changed_paths(args.repo, args.base, args.head))
    except PolicyError as exc:
        print(f"change_policy=failed reason={exc}", file=sys.stderr)
        return 1
    lines = [f"{key}={'true' if output[key] else 'false'}" for key in OUTPUTS]
    if args.github_output:
        args.github_output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
