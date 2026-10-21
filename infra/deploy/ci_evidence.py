#!/usr/bin/env python3
"""Find reusable pull-request CI evidence for an exact post-merge revision.

A squash merge creates a new commit SHA, but when its tree equals the merged
pull request head's tree the code, lockfiles and CI definitions are identical.
Evidence is reusable only when Required CI succeeded on that head and every job
of the unit's CI workflow succeeded in its latest attempt. Any mismatch, missing
data or API failure reports ``unavailable`` so the caller runs CI normally.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

UNITS = ("findb", "fetcher")
REQUIRED_CI_WORKFLOW = "required-ci.yml"
REQUIRED_CI_JOB = "Required CI"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
JOB_KEY_PATTERN = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")

Fetch = Callable[[str], Any]


class EvidenceUnavailableError(Exception):
    pass


def unit_ci_jobs(workflow_text: str) -> set[str]:
    """Return the top-level job keys of a unit CI workflow."""
    keys: set[str] = set()
    in_jobs = False
    for line in workflow_text.splitlines():
        if line == "jobs:":
            in_jobs = True
            continue
        if in_jobs and line and not line.startswith(" "):
            break
        match = JOB_KEY_PATTERN.match(line) if in_jobs else None
        if match:
            keys.add(match.group(1))
    if not keys:
        raise EvidenceUnavailableError("unit_ci_jobs_unknown")
    return keys


def _tree(fetch: Fetch, repository: str, sha: str) -> str:
    tree = fetch(f"repos/{repository}/git/commits/{sha}")["tree"]["sha"]
    if not isinstance(tree, str) or not SHA_PATTERN.match(tree):
        raise EvidenceUnavailableError("tree_invalid")
    return tree


def find_evidence(
    fetch: Fetch, repository: str, revision: str, unit: str, ci_jobs: set[str]
) -> dict[str, Any]:
    if unit not in UNITS or not SHA_PATTERN.match(revision):
        raise EvidenceUnavailableError("input_invalid")

    pulls = [
        pull
        for pull in fetch(f"repos/{repository}/commits/{revision}/pulls")
        if pull.get("merge_commit_sha") == revision and pull.get("merged_at")
    ]
    if len(pulls) != 1:
        raise EvidenceUnavailableError("merged_pull_request_not_unique")
    pull = pulls[0]
    head = pull.get("head") or {}
    head_sha = head.get("sha")
    if (
        (pull.get("base") or {}).get("ref") != "main"
        or (head.get("repo") or {}).get("full_name") != repository
        or not isinstance(head_sha, str)
        or not SHA_PATTERN.match(head_sha)
    ):
        raise EvidenceUnavailableError("pull_request_untrusted")

    if _tree(fetch, repository, revision) != _tree(fetch, repository, head_sha):
        raise EvidenceUnavailableError("tree_mismatch")

    runs = fetch(
        f"repos/{repository}/actions/workflows/{REQUIRED_CI_WORKFLOW}/runs"
        f"?event=pull_request&head_sha={head_sha}&per_page=100"
    )["workflow_runs"]
    runs = [
        run
        for run in runs
        if run.get("head_sha") == head_sha
        and run.get("event") == "pull_request"
        and (run.get("head_repository") or {}).get("full_name") == repository
    ]
    if not runs:
        raise EvidenceUnavailableError("required_ci_missing")
    # Only the newest run counts; an older success never masks a later failure.
    run = max(runs, key=lambda item: (item.get("created_at") or "", item.get("id") or 0))
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise EvidenceUnavailableError("required_ci_not_successful")

    jobs = fetch(f"repos/{repository}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100")[
        "jobs"
    ]
    conclusions: dict[str, str | None] = {}
    for job in jobs:
        name = job.get("name")
        if name in conclusions:
            raise EvidenceUnavailableError("job_name_ambiguous")
        conclusions[name] = job.get("conclusion")
    if conclusions.get(REQUIRED_CI_JOB) != "success":
        raise EvidenceUnavailableError("required_ci_not_successful")
    # A skipped unit suite in the pull request is no evidence for this unit.
    observed = {
        name for name in conclusions if isinstance(name, str) and name.startswith(f"{unit} / ")
    }
    expected = {f"{unit} / {key}" for key in ci_jobs}
    if observed != expected or any(conclusions[name] != "success" for name in expected):
        raise EvidenceUnavailableError("unit_ci_incomplete")

    return {"pull_request": pull.get("number"), "head_sha": head_sha, "run_id": run["id"]}


def github_fetch(token: str, api_url: str) -> Fetch:
    def fetch(path: str) -> Any:
        request = urllib.request.Request(
            f"{api_url.rstrip('/')}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)

    return fetch


def main(argv: list[str] | None = None, fetch: Fetch | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--unit", choices=UNITS, required=True)
    parser.add_argument("--ci-workflow", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        if fetch is None:
            token = os.environ.get("GH_TOKEN", "")
            if not token:
                raise EvidenceUnavailableError("token_missing")
            fetch = github_fetch(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))
        ci_jobs = unit_ci_jobs(args.ci_workflow.read_text(encoding="utf-8"))
        evidence = find_evidence(fetch, args.repository, args.revision, args.unit, ci_jobs)
    except EvidenceUnavailableError as exc:
        print(f"ci_evidence=unavailable unit={args.unit} reason={exc}")
        return 0
    except Exception as exc:  # Any API or payload fault falls back to running CI.
        print(f"ci_evidence=unavailable unit={args.unit} reason=lookup_failed:{type(exc).__name__}")
        return 0
    print(
        f"ci_evidence=reused unit={args.unit} pull_request={evidence['pull_request']} "
        f"head_sha={evidence['head_sha']} run_id={evidence['run_id']}"
    )
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"{args.unit}_ci_evidence={evidence['run_id']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
