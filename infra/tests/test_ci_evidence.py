"""Post-merge CD may reuse pull-request CI only for an identical, fully tested tree."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ci_evidence", ROOT / "infra/deploy/ci_evidence.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

REPOSITORY = "FPI-TW/findb"
MERGE = "a" * 40
HEAD = "b" * 40
TREE = "c" * 40
FINDB_JOBS = {"backend", "backend-migrations", "infra-tests", "dashboard", "dashboard-browser"}


def _api(**overrides):
    pull = {
        "number": 329,
        "merge_commit_sha": MERGE,
        "merged_at": "2026-10-21T06:01:37Z",
        "base": {"ref": "main"},
        "head": {"sha": HEAD, "repo": {"full_name": REPOSITORY}},
    }
    run = {
        "id": 101,
        "head_sha": HEAD,
        "event": "pull_request",
        "head_repository": {"full_name": REPOSITORY},
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-10-21T05:47:24Z",
    }
    jobs = [{"name": "policy", "conclusion": "success"}]
    jobs += [{"name": f"findb / {key}", "conclusion": "success"} for key in sorted(FINDB_JOBS)]
    jobs += [
        {"name": "fetcher / test", "conclusion": "skipped"},
        {"name": "Required CI", "conclusion": "success"},
    ]
    data = {
        "pulls": [pull],
        "trees": {MERGE: TREE, HEAD: TREE},
        "runs": [run],
        "jobs": jobs,
    }
    data.update(overrides)
    calls = []

    def fetch(path):
        calls.append(path)
        if path == f"repos/{REPOSITORY}/commits/{MERGE}/pulls":
            return data["pulls"]
        if path.startswith(f"repos/{REPOSITORY}/git/commits/"):
            return {"tree": {"sha": data["trees"][path.rsplit("/", 1)[1]]}}
        if path.startswith(f"repos/{REPOSITORY}/actions/workflows/required-ci.yml/runs?"):
            assert "event=pull_request" in path and f"head_sha={HEAD}" in path
            return {"workflow_runs": data["runs"]}
        if path.startswith(f"repos/{REPOSITORY}/actions/runs/"):
            assert "filter=latest" in path
            return {"jobs": data["jobs"]}
        raise AssertionError(path)

    fetch.calls = calls
    return fetch, data


def _find(fetch, unit="findb", jobs=FINDB_JOBS):
    return module.find_evidence(fetch, REPOSITORY, MERGE, unit, set(jobs))


def test_identical_tree_with_complete_unit_ci_is_reusable():
    fetch, _ = _api()
    assert _find(fetch) == {"pull_request": 329, "head_sha": HEAD, "run_id": 101}


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda d: d.update(pulls=[]), "merged_pull_request_not_unique"),
        (lambda d: d.update(pulls=d["pulls"] * 2), "merged_pull_request_not_unique"),
        (lambda d: d["pulls"][0].update(merged_at=None), "merged_pull_request_not_unique"),
        (
            lambda d: d["pulls"][0].update(merge_commit_sha="d" * 40),
            "merged_pull_request_not_unique",
        ),
        (lambda d: d["pulls"][0]["base"].update(ref="release"), "pull_request_untrusted"),
        (
            lambda d: d["pulls"][0]["head"].update(repo={"full_name": "fork/findb"}),
            "pull_request_untrusted",
        ),
        (lambda d: d["pulls"][0]["head"].update(repo=None), "pull_request_untrusted"),
        (lambda d: d["trees"].update({HEAD: "e" * 40}), "tree_mismatch"),
        (lambda d: d.update(runs=[]), "required_ci_missing"),
        (lambda d: d["runs"][0].update(event="push"), "required_ci_missing"),
        (
            lambda d: d["runs"][0].update(head_repository={"full_name": "fork/findb"}),
            "required_ci_missing",
        ),
        (lambda d: d["runs"][0].update(conclusion="failure"), "required_ci_not_successful"),
        (
            lambda d: d["runs"][0].update(status="in_progress", conclusion=None),
            "required_ci_not_successful",
        ),
        (lambda d: d["jobs"][-1].update(conclusion="failure"), "required_ci_not_successful"),
        (lambda d: d["jobs"][1].update(conclusion="skipped"), "unit_ci_incomplete"),
        (lambda d: d["jobs"].pop(1), "unit_ci_incomplete"),
        (
            lambda d: d["jobs"].append({"name": "findb / unknown", "conclusion": "success"}),
            "unit_ci_incomplete",
        ),
        (lambda d: d["jobs"].append(dict(d["jobs"][1])), "job_name_ambiguous"),
    ],
)
def test_any_doubt_makes_evidence_unavailable(mutate, reason):
    fetch, data = _api()
    mutate(data)
    with pytest.raises(module.EvidenceUnavailableError, match=f"^{reason}$"):
        _find(fetch)


def test_newest_required_ci_run_wins_over_an_older_success():
    fetch, data = _api()
    data["runs"].append(
        {
            **data["runs"][0],
            "id": 102,
            "conclusion": "failure",
            "created_at": "2026-10-21T05:59:00Z",
        }
    )
    with pytest.raises(module.EvidenceUnavailableError, match="^required_ci_not_successful$"):
        _find(fetch)


def test_a_unit_skipped_in_the_pull_request_has_no_evidence():
    fetch, _ = _api()
    with pytest.raises(module.EvidenceUnavailableError, match="^unit_ci_incomplete$"):
        _find(fetch, unit="fetcher", jobs={"test"})


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
def test_expected_jobs_come_from_the_unit_ci_workflow(unit):
    keys = module.unit_ci_jobs((ROOT / f".github/workflows/{unit}-ci.yml").read_text())
    assert keys == (FINDB_JOBS if unit == "findb" else {"test"})


def test_cli_writes_output_only_for_reusable_evidence(tmp_path, capsys):
    output = tmp_path / "output"
    output.write_text("findb_ci=true\n")
    argv = [
        "--repository",
        REPOSITORY,
        "--revision",
        MERGE,
        "--unit",
        "findb",
        "--ci-workflow",
        str(ROOT / ".github/workflows/findb-ci.yml"),
        "--github-output",
        str(output),
    ]
    fetch, data = _api()
    assert module.main(argv, fetch) == 0
    assert output.read_text() == "findb_ci=true\nfindb_ci_evidence=101\n"
    assert "ci_evidence=reused unit=findb pull_request=329" in capsys.readouterr().out

    output.write_text("findb_ci=true\n")
    data["trees"][HEAD] = "e" * 40
    assert module.main(argv, fetch) == 0
    assert output.read_text() == "findb_ci=true\n"
    assert "ci_evidence=unavailable unit=findb reason=tree_mismatch" in capsys.readouterr().out


def test_cli_falls_back_to_running_ci_on_api_failure(tmp_path, capsys):
    output = tmp_path / "output"

    def broken(path):
        raise OSError("network down")

    argv = [
        "--repository",
        REPOSITORY,
        "--revision",
        MERGE,
        "--unit",
        "fetcher",
        "--ci-workflow",
        str(ROOT / ".github/workflows/fetcher-ci.yml"),
        "--github-output",
        str(output),
    ]
    assert module.main(argv, broken) == 0
    assert not output.exists()
    assert "reason=lookup_failed:OSError" in capsys.readouterr().out
