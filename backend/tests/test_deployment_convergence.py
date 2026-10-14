"""Focused contracts for the single CD pipeline and its promotion primitives."""

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
STAGING_REGISTRY = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"


def _module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = _module("change_policy", "infra/deploy/change_policy.py")
manifest = _module("release_manifest_v2", "infra/deploy/release_manifest.py")
contracts = manifest.load_contract_manifest(ROOT / "contracts" / "manifest.json")


def _workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text()


def _job_block(text: str, job: str) -> str:
    match = re.search(
        rf"^  {re.escape(job)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text, re.M | re.S
    )
    assert match, job
    return match.group("body")


def _step(workflow: dict, job: str, name: str) -> dict:
    return next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == name)


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["contracts/market_eod/v1.schema.json"], (True, True, False, False, False)),
        (["backend/app/main.py"], (True, False, True, False, False)),
        (["fetcher/src/main.py"], (False, True, False, True, False)),
        (["infra/deploy/release_manifest.py"], (True, True, True, True, False)),
        (["infra/tofu/staging/main.tf"], (False, False, False, False, True)),
        (["infra/env/production/findb.json"], (False, False, False, False, True)),
        (["infra/env/control_plane.py"], (False, False, False, False, True)),
        ([".github/workflows/findb-ci.yml"], (True, True, False, False, False)),
        ([".github/workflows/cd.yml"], (True, True, True, True, False)),
        ([".github/workflows/findb-deploy.yml"], (True, True, True, False, False)),
        ([".github/workflows/fetcher-deploy.yml"], (True, True, False, True, False)),
        ([".github/workflows/fetcher-finlab-smoke.yml"], (True, True, False, False, False)),
        ([".github/workflows/required-ci.yml"], (True, True, False, False, False)),
        (["docs/operations/deployment.md"], (False, False, False, False, False)),
        ([""], (False, False, False, False, False)),
    ],
)
def test_change_policy_truth_table(paths: list[str], expected: tuple[bool, ...]) -> None:
    result = policy.classify(paths)
    assert tuple(result[key] for key in policy.OUTPUTS) == expected


def _v2_manifest(target: str) -> dict[str, object]:
    registry = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com"
    unit = "findb"
    images = {
        name: f"{repo}@sha256:{'b' * 64}"
        for name, repo in manifest.image_repositories(target, unit, registry).items()
    }
    result: dict[str, object] = {
        "schema_version": 2,
        "deployment_target": target,
        "unit": unit,
        "commit_sha": "a" * 40,
        "images": images,
        "migration_revision": "c" * 12,
        "contract_versions": list(contracts.versions),
        "contract_manifest_sha256": contracts.canonical_sha256,
        "deployment_source_bundle_sha256": "d" * 64,
        "created_by_run_id": "1",
        "registry": registry,
        "account_id": "123456789012",
        "release_tag": "" if target == "staging" else "findb-v1.2.3",
    }
    if target == "production":
        result["promotion_source"] = {
            "accepted_bundle_key": f"findb/accepted/{'a' * 40}/{'d' * 64}.tar",
            "staging_registry": STAGING_REGISTRY,
        }
    return result


def test_v2_manifest_is_target_aware_and_production_requires_promotion_source() -> None:
    manifest.validate_manifest(_v2_manifest("staging"), contracts)
    manifest.validate_manifest(_v2_manifest("production"), contracts)
    broken = _v2_manifest("production")
    broken.pop("promotion_source")
    with pytest.raises(manifest.ManifestError, match="keys invalid"):
        manifest.validate_manifest(broken, contracts)
    cross_target = _v2_manifest("production")
    cross_target["images"] = _v2_manifest("staging")["images"]
    with pytest.raises(manifest.ManifestError, match="repository or digest invalid"):
        manifest.validate_manifest(cross_target, contracts)
    account_mismatch = _v2_manifest("staging")
    account_mismatch["account_id"] = "000000000000"
    with pytest.raises(manifest.ManifestError, match="registry or release_tag invalid"):
        manifest.validate_manifest(account_mismatch, contracts)
    source_registry_mismatch = _v2_manifest("production")
    source_registry_mismatch["promotion_source"] = {
        "accepted_bundle_key": f"findb/accepted/{'a' * 40}/{'d' * 64}.tar",
        "staging_registry": "not-an-ecr-registry.example",
    }
    with pytest.raises(manifest.ManifestError, match="production promotion_source invalid"):
        manifest.validate_manifest(source_registry_mismatch, contracts)


def test_v1_manifests_are_rejected_in_every_target() -> None:
    for target in ("staging", "production"):
        legacy = {
            "schema_version": 1,
            "deployment_target": target,
            "unit": "fetcher",
            "commit_sha": "a" * 40,
            "images": {
                name: f"{repo}@sha256:{'b' * 64}"
                for name, repo in manifest.image_repositories(
                    "staging", "fetcher", STAGING_REGISTRY
                ).items()
            },
            "migration_revision": "none",
            "contract_versions": list(contracts.versions),
            "contract_manifest_sha256": contracts.canonical_sha256,
            "deployment_source_bundle_sha256": "d" * 64,
            "created_by_run_id": "1",
        }
        with pytest.raises(manifest.ManifestError, match="schema_version invalid"):
            manifest.validate_manifest(legacy, contracts)


def test_single_cd_workflow_replaces_every_target_specific_caller() -> None:
    names = {path.name for path in WORKFLOWS.glob("*.yml")}
    assert names == {
        "cd.yml",
        "findb-ci.yml",
        "findb-deploy.yml",
        "fetcher-ci.yml",
        "fetcher-deploy.yml",
        "fetcher-finlab-smoke.yml",
        "required-ci.yml",
        "staging-infra-plan.yml",
    }
    text = _workflow_text("cd.yml")
    workflow = yaml.safe_load(text)
    assert workflow[True]["push"] == {"branches": ["main"]}
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["target"]["options"] == ["staging", "production"]
    assert inputs["mode"]["options"] == ["candidate", "replay", "promote", "rollback"]
    assert inputs["units"]["options"] == ["both", "findb", "fetcher"]
    assert set(inputs) == {
        "target",
        "mode",
        "units",
        "findb_release_tag",
        "fetcher_release_tag",
        "findb_bundle_key",
        "fetcher_bundle_key",
        "confirmation",
    }
    assert set(workflow["jobs"]) == {
        "resolve",
        "verify-findb",
        "verify-fetcher",
        "publish-findb",
        "publish-fetcher",
        "release-findb",
        "release-fetcher",
        "deploy-findb",
        "deploy-fetcher",
        "report-findb-failure",
        "report-fetcher-failure",
    }
    assert "vars." not in text and "secrets." not in text
    assert "PRODUCTION_DEPLOY_ENABLED" not in text
    assert "STAGING_ECR_CUTOVER_ENABLED" not in text
    assert "image_tag" not in text
    assert not {"ghcr.io", "appleboy", "FINDB_EC2_", "FETCHER_EC2_", "scp"} & set(text.split())


def test_production_promotion_is_manual_and_promotes_exact_staging_digests() -> None:
    text = _workflow_text("cd.yml")
    resolve = _job_block(text, "resolve")
    for confirmation in (
        "PROMOTE_FINDB_PRODUCTION",
        "PROMOTE_FETCHER_PRODUCTION",
        "PROMOTE_FINDB_FETCHER_PRODUCTION",
    ):
        assert confirmation in resolve
    assert '[ "$CONFIRMATION" = "$expected_confirmation" ]' in resolve
    assert 'git fetch --no-tags origin "+refs/tags/$tag:refs/tags/$tag"' in resolve
    assert '[ "$(git cat-file -t "refs/tags/$tag")" = tag ]' in resolve
    assert 'peeled_commit_sha="$(git rev-parse "refs/tags/$tag^{commit}")"' in resolve
    assert 'git merge-base --is-ancestor "$peeled_commit_sha" refs/remotes/origin/main' in resolve
    for unit in ("findb", "fetcher"):
        release = _job_block(text, f"release-{unit}")
        assert "promote_image.sh" in release and "docker build" not in release
        assert "STAGING_PROMOTION_READ_ROLE_ARN" in release
        assert "AWS_PROMOTION_ROLE_ARN" in release
        assert '.registry "$RUNNER_TEMP/staging-manifest.json"' in release
        assert '.account_id "$RUNNER_TEMP/staging-manifest.json"' in release
        assert '[ "$STAGING_AWS_ACCOUNT_ID" != "$AWS_ACCOUNT_ID" ]' in release
        assert f"{unit}/production/candidates/" in release
        assert "infra/deploy/bind_release_tag.sh" in release
        assert release.count("infra/deploy/bind_release_tag.sh") == 2  # promote and rollback forms
        assert "Download and validate exact accepted staging bundle" in release
        assert (
            release.index("Download and validate exact accepted staging bundle")
            < release.index("Bind or validate immutable release tag")
            < release.index("Copy exact digests and create production candidate bundle")
        )
        assert release.count("Verify exact accepted staging binding source") == 0
        assert f"PROMOTE_{unit.upper()}_PRODUCTION" not in release
    assert "secrets: inherit" not in text
    for action in re.findall(r"uses:\s*([^\s#}]+)", text):
        if not action.startswith("./"):
            assert re.search(r"@[0-9a-f]{40}$", action), action


def test_production_does_not_rerun_ci_on_an_already_accepted_bundle() -> None:
    text = _workflow_text("cd.yml")
    resolve = _job_block(text, "resolve")
    assert 'findb_ci="$deploy_findb"; fetcher_ci="$deploy_fetcher"' in resolve
    assert resolve.index('if [ "$MODE" = candidate ]; then') < resolve.index(
        'findb_ci="$deploy_findb"; fetcher_ci="$deploy_fetcher"'
    )
    workflow = yaml.safe_load(text)
    for unit in ("findb", "fetcher"):
        verify = workflow["jobs"][f"verify-{unit}"]
        assert verify["uses"] == f"./.github/workflows/{unit}-ci.yml"
        assert verify["if"] == f"${{{{ needs.resolve.outputs.{unit}_ci == 'true' }}}}"


def test_production_release_tags_require_annotated_object_identity(tmp_path: Path) -> None:
    repository = tmp_path / "tag-repository"
    repository.mkdir()
    for command in (
        ("git", "init", "-q"),
        ("git", "config", "user.name", "FinDB Test"),
        ("git", "config", "user.email", "findb-test@example.invalid"),
    ):
        subprocess.run(command, cwd=repository, check=True)
    (repository / "release.txt").write_text("release\n", encoding="utf-8")
    subprocess.run(("git", "add", "release.txt"), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "release"), cwd=repository, check=True)
    commit_sha = subprocess.check_output(
        ("git", "rev-parse", "HEAD"), cwd=repository, text=True
    ).strip()

    subprocess.run(("git", "tag", "findb-v1.0.0"), cwd=repository, check=True)
    lightweight_type = subprocess.check_output(
        ("git", "cat-file", "-t", "refs/tags/findb-v1.0.0"), cwd=repository, text=True
    ).strip()
    assert lightweight_type == "commit"

    subprocess.run(
        ("git", "tag", "-a", "findb-v1.0.1", "-m", "FinDB v1.0.1"),
        cwd=repository,
        check=True,
    )
    annotated_type = subprocess.check_output(
        ("git", "cat-file", "-t", "refs/tags/findb-v1.0.1"), cwd=repository, text=True
    ).strip()
    tag_oid = subprocess.check_output(
        ("git", "rev-parse", "refs/tags/findb-v1.0.1"), cwd=repository, text=True
    ).strip()
    peeled_sha = subprocess.check_output(
        ("git", "rev-parse", "refs/tags/findb-v1.0.1^{commit}"), cwd=repository, text=True
    ).strip()
    assert annotated_type == "tag"
    assert tag_oid != commit_sha
    assert peeled_sha == commit_sha


def _resolve_fixture_repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A main branch with an annotated tag, an unreachable commit, and an origin remote."""
    origin = tmp_path / "origin.git"
    subprocess.run(("git", "init", "-q", "--bare", str(origin)), check=True)
    work = tmp_path / "work"
    subprocess.run(("git", "clone", "-q", str(origin), str(work)), check=True)
    for command in (
        ("git", "config", "user.name", "FinDB Test"),
        ("git", "config", "user.email", "findb-test@example.invalid"),
        ("git", "checkout", "-q", "-b", "main"),
    ):
        subprocess.run(command, cwd=work, check=True)
    (work / "release.txt").write_text("one\n")
    subprocess.run(("git", "add", "release.txt"), cwd=work, check=True)
    subprocess.run(("git", "commit", "-qm", "one"), cwd=work, check=True)
    subprocess.run(("git", "tag", "-a", "findb-v1.0.0", "-m", "FinDB v1.0.0"), cwd=work, check=True)
    subprocess.run(
        ("git", "tag", "-a", "fetcher-v1.0.0", "-m", "Fetcher v1.0.0"), cwd=work, check=True
    )
    first = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=work, text=True).strip()
    (work / "release.txt").write_text("two\n")
    subprocess.run(("git", "commit", "-qam", "two"), cwd=work, check=True)
    head = subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=work, text=True).strip()
    subprocess.run(("git", "push", "-q", "origin", "main", "--tags"), cwd=work, check=True)
    subprocess.run(("git", "checkout", "-q", "-b", "unmerged"), cwd=work, check=True)
    (work / "release.txt").write_text("three\n")
    subprocess.run(("git", "commit", "-qam", "three"), cwd=work, check=True)
    subprocess.run(("git", "checkout", "-q", "main"), cwd=work, check=True)
    # The resolve step runs from a checkout that carries the policy script.
    (work / "infra" / "deploy").mkdir(parents=True)
    shutil.copy(ROOT / "infra/deploy/change_policy.py", work / "infra/deploy/change_policy.py")
    return work, first, head


def _run_resolve(work: Path, **environment: str) -> tuple[int, dict[str, str], str]:
    workflow = yaml.safe_load(_workflow_text("cd.yml"))
    script = _step(workflow, "resolve", "Resolve exact target, mode, units and revisions")["run"]
    output = work / "github-output"
    output.write_text("")
    base = {
        "EVENT": "workflow_dispatch",
        "BASE": "0" * 40,
        "TARGET": "staging",
        "MODE": "candidate",
        "UNITS": "both",
        "CONFIRMATION": "",
        "FINDB_RELEASE_TAG": "",
        "FETCHER_RELEASE_TAG": "",
        "FINDB_BUNDLE_KEY": "",
        "FETCHER_BUNDLE_KEY": "",
    }
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=work,
        env={
            **os.environ,
            **base,
            **environment,
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(work),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
    return completed.returncode, outputs, completed.stderr


def test_cd_resolve_derives_revisions_from_keys_tags_and_policy(tmp_path: Path) -> None:
    work, first, head = _resolve_fixture_repository(tmp_path)
    unmerged = subprocess.check_output(
        ("git", "rev-parse", "unmerged"), cwd=work, text=True
    ).strip()
    sha256 = "e" * 64

    code, outputs, stderr = _run_resolve(
        work, MODE="candidate", CONFIRMATION="deploy-new-candidate"
    )
    assert code == 0, stderr
    assert outputs["findb_revision"] == head and outputs["fetcher_revision"] == head
    assert outputs["findb_ci"] == "true" and outputs["fetcher_ci"] == "true"
    assert outputs["host_mode"] == "candidate" and outputs["namespace"] == "FinDB/Staging"

    code, outputs, _ = _run_resolve(
        work, MODE="candidate", UNITS="fetcher", CONFIRMATION="deploy-new-candidate"
    )
    assert code == 0
    assert outputs["deploy_findb"] == "false" and outputs["deploy_fetcher"] == "true"
    assert outputs["findb_ci"] == "false" and outputs["fetcher_ci"] == "true"

    code, _, _ = _run_resolve(work, MODE="candidate", CONFIRMATION="")
    assert code != 0

    code, outputs, stderr = _run_resolve(
        work,
        MODE="replay",
        FINDB_BUNDLE_KEY=f"findb/accepted/{first}/{sha256}.tar",
        FETCHER_BUNDLE_KEY=f"fetcher/accepted/{first}/{sha256}.tar",
    )
    assert code == 0, stderr
    assert outputs["findb_revision"] == first and outputs["fetcher_revision"] == first
    assert outputs["findb_ci"] == "false" and outputs["fetcher_ci"] == "false"
    assert outputs["host_mode"] == "replay"

    code, _, _ = _run_resolve(
        work,
        MODE="replay",
        FINDB_BUNDLE_KEY=f"findb/accepted/{unmerged}/{sha256}.tar",
        FETCHER_BUNDLE_KEY=f"fetcher/accepted/{first}/{sha256}.tar",
    )
    assert code != 0  # A key whose revision is not on main is rejected.

    code, _, _ = _run_resolve(
        work,
        MODE="replay",
        UNITS="findb",
        FINDB_BUNDLE_KEY=f"findb/accepted/{first}/{sha256}.tar",
        FETCHER_BUNDLE_KEY=f"fetcher/accepted/{first}/{sha256}.tar",
    )
    assert code != 0  # An unselected unit must not carry a key.

    code, outputs, stderr = _run_resolve(
        work,
        TARGET="production",
        MODE="promote",
        CONFIRMATION="PROMOTE_FINDB_FETCHER_PRODUCTION",
        FINDB_RELEASE_TAG="findb-v1.0.0",
        FETCHER_RELEASE_TAG="fetcher-v1.0.0",
        FINDB_BUNDLE_KEY=f"findb/accepted/{first}/{sha256}.tar",
        FETCHER_BUNDLE_KEY=f"fetcher/accepted/{first}/{sha256}.tar",
    )
    assert code == 0, stderr
    assert outputs["findb_revision"] == first and outputs["fetcher_revision"] == first
    assert outputs["findb_tag_oid"] != first and len(outputs["findb_tag_oid"]) == 40
    assert outputs["namespace"] == "FinDB/Production" and outputs["host_mode"] == "candidate"

    code, _, _ = _run_resolve(
        work,
        TARGET="production",
        MODE="promote",
        CONFIRMATION="PROMOTE_FINDB_PRODUCTION",
        FINDB_RELEASE_TAG="findb-v1.0.0",
        FETCHER_RELEASE_TAG="fetcher-v1.0.0",
        FINDB_BUNDLE_KEY=f"findb/accepted/{first}/{sha256}.tar",
        FETCHER_BUNDLE_KEY=f"fetcher/accepted/{first}/{sha256}.tar",
    )
    assert code != 0  # The confirmation names the selected units exactly.

    code, outputs, stderr = _run_resolve(
        work,
        TARGET="production",
        MODE="rollback",
        UNITS="findb",
        CONFIRMATION="PROMOTE_FINDB_PRODUCTION",
        FINDB_RELEASE_TAG="findb-v1.0.0",
        FINDB_BUNDLE_KEY=f"findb/production/accepted/{first}/{sha256}.tar",
    )
    assert code == 0, stderr
    assert outputs["host_mode"] == "replay" and outputs["deploy_fetcher"] == "false"

    code, _, _ = _run_resolve(
        work,
        TARGET="production",
        MODE="rollback",
        UNITS="findb",
        CONFIRMATION="PROMOTE_FINDB_PRODUCTION",
        FINDB_RELEASE_TAG="findb-v1.0.0",
        FINDB_BUNDLE_KEY=f"findb/accepted/{first}/{sha256}.tar",
    )
    assert code != 0  # Rollback replays only production accepted records.

    code, _, _ = _run_resolve(
        work, TARGET="production", MODE="candidate", CONFIRMATION="deploy-new-candidate"
    )
    assert code != 0
    code, _, _ = _run_resolve(work, TARGET="staging", MODE="promote")
    assert code != 0


def test_cd_resolve_push_uses_the_change_policy(tmp_path: Path) -> None:
    work, first, head = _resolve_fixture_repository(tmp_path)
    code, outputs, stderr = _run_resolve(work, EVENT="push", BASE=first)
    assert code == 0, stderr
    assert outputs["target"] == "staging" and outputs["mode"] == "candidate"
    assert outputs["findb_revision"] == head and outputs["fetcher_revision"] == head
    # release.txt touches no unit, so the push deploys nothing.
    assert outputs["deploy_findb"] == "false" and outputs["deploy_fetcher"] == "false"
    code, _, _ = _run_resolve(work, EVENT="push", BASE=first, CONFIRMATION="deploy-new-candidate")
    assert code != 0


def test_release_tag_binding_helper_is_conditional_kms_and_fail_closed() -> None:
    helper = ROOT / "infra" / "deploy" / "bind_release_tag.sh"
    completed = subprocess.run(
        ["bash", "-n", str(helper)], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 0
    text = helper.read_text()
    assert "--if-none-match '*'" in text
    assert "--server-side-encryption aws:kms" in text
    assert '--ssekms-key-id "$BUNDLE_KMS_KEY"' in text
    assert "binding_missing_or_unreadable" in text
    assert "binding_mismatch" in text


def test_reusable_deployments_persist_production_acceptance_and_replay_only_accepted() -> None:
    for unit in ("findb", "fetcher"):
        text = _workflow_text(f"{unit}-deploy.yml")
        assert "inputs.mode == 'candidate'" in text
        assert f"{unit}/production/accepted/" in text
        assert 'state:"accepted"' in text
        assert "ssm_command_marker_state" in text
        assert "PRODUCTION_DEPLOY_ENABLED" not in text
        assert "vars." not in text
        assert 'bundle_sha256="${bundle_filename##*-}"' in text
        assert 'infra/deploy/release_manifest.py > \\"\\$validator\\"' in text
        assert "VALIDATOR_SHA256" in text
        assert "aws sts get-caller-identity --query Account" in text
        assert "getent ahostsv4 $q_dns" in text
        assert f"/opt/{unit}/current/infra/deploy/release_manifest.py" not in text
        assert 'jq -e --arg target "$TARGET"' in text
        assert "runtime_profile" not in text.split("- name: Persist immutable", 1)[1]


def test_production_candidate_record_precedes_activation_and_bootstrap_has_target_contract() -> (
    None
):
    for unit in ("findb", "fetcher"):
        text = _workflow_text(f"{unit}-deploy.yml")
        assert (
            text.index("Run candidate acceptance or accepted replay")
            < text.index("Persist immutable target-aware acceptance record")
            < text.index("Activate only after immutable acceptance is durable")
        )
        assert 'host_mode=candidate; [ "$MODE" = replay ] && host_mode=activate' in text
    bootstrap = (ROOT / "infra/deploy/runtime-secrets/install_findb_bootstrap.sh").read_text()
    assert 'deployment_target="${4:?deployment target required}"' in bootstrap
    assert 'aws_account_id="${5:?AWS account id required}"' in bootstrap
    assert "$deployment_target $aws_account_id" in bootstrap


def test_control_plane_is_loaded_from_the_workflow_sha_in_every_aws_job() -> None:
    cd = yaml.safe_load(_workflow_text("cd.yml"))
    for job in (
        "publish-findb",
        "publish-fetcher",
        "release-findb",
        "release-fetcher",
        "report-findb-failure",
        "report-fetcher-failure",
    ):
        steps = cd["jobs"][job]["steps"]
        checkout = next(
            step
            for step in steps
            if step.get("name") == "Checkout trusted control-plane configuration"
        )
        assert checkout["with"]["ref"] == "${{ github.workflow_sha }}"
        assert checkout["with"]["persist-credentials"] is False
        load = next(
            step for step in steps if step.get("name") == "Load control-plane configuration"
        )
        assert "control_plane.py export" in load["run"]
        assert steps.index(load) < next(
            index
            for index, step in enumerate(steps)
            if "configure-aws-credentials" in step.get("uses", "")
        )
    for unit in ("findb", "fetcher"):
        reusable = yaml.safe_load(_workflow_text(f"{unit}-deploy.yml"))
        steps = reusable["jobs"]["deploy"]["steps"]
        load = next(
            step for step in steps if step.get("name") == "Load control-plane configuration"
        )
        assert steps.index(load) < next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Verify immutable target contract"
        )
        credentials = next(
            step for step in steps if "configure-aws-credentials" in step.get("uses", "")
        )
        assert credentials["with"]["role-to-assume"] == "${{ env.AWS_DEPLOY_ROLE_ARN }}"
        assert credentials["with"]["allowed-account-ids"] == "${{ env.AWS_ACCOUNT_ID }}"


def test_containerized_dashboard_ci_marks_only_the_fixed_workspace_safe() -> None:
    workflow = _workflow_text("findb-ci.yml")
    browser_job = workflow.split("  dashboard-browser:", 1)[1]
    verify = browser_job.split("- name: Verify exact requested revision", 1)[1].split(
        "- name: Set up pnpm", 1
    )[0]
    assert 'git config --global --add safe.directory "$GITHUB_WORKSPACE"' in verify
    assert 'git -C "$GITHUB_WORKSPACE" rev-parse HEAD' in verify
    assert "safe.directory '*'" not in verify


@pytest.mark.parametrize(
    ("environment", "expected_code"),
    [
        (
            {
                "POLICY": "success",
                "FINDB": "success",
                "FETCHER": "success",
                "WANT_FINDB": "true",
                "WANT_FETCHER": "true",
                "INFRA": "success",
                "RETIREMENT_INFRA": "skipped",
                "WANT_INFRA": "true",
                "PR_HEAD_REF": "refactor/deployment-flow-convergence",
            },
            0,
        ),
        (
            {
                "POLICY": "success",
                "FINDB": "skipped",
                "FETCHER": "skipped",
                "WANT_FINDB": "false",
                "WANT_FETCHER": "false",
                "INFRA": "skipped",
                "RETIREMENT_INFRA": "skipped",
                "WANT_INFRA": "false",
                "PR_HEAD_REF": "docs/deployment-notes",
            },
            0,
        ),
        (
            {
                "POLICY": "success",
                "FINDB": "skipped",
                "FETCHER": "skipped",
                "WANT_FINDB": "false",
                "WANT_FETCHER": "false",
                "INFRA": "skipped",
                "RETIREMENT_INFRA": "success",
                "WANT_INFRA": "true",
                "PR_HEAD_REF": "chore/staging-phase2-retirement",
            },
            0,
        ),
        (
            {
                "POLICY": "success",
                "FINDB": "success",
                "FETCHER": "skipped",
                "WANT_FINDB": "true",
                "WANT_FETCHER": "true",
                "INFRA": "success",
                "RETIREMENT_INFRA": "skipped",
                "WANT_INFRA": "true",
                "PR_HEAD_REF": "fix/failed-fetcher-ci",
            },
            1,
        ),
    ],
)
def test_required_ci_aggregator_truth_table(
    environment: dict[str, str], expected_code: int
) -> None:
    workflow = yaml.safe_load(_workflow_text("required-ci.yml"))
    assert workflow["jobs"]["required"]["name"] == "Required CI"
    script = workflow["jobs"]["required"]["steps"][0]["run"]
    completed = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, **environment},
        check=False,
    )
    assert completed.returncode == expected_code


def test_cd_jobs_delegate_host_mutation_to_the_reusable_deployments() -> None:
    """The caller may build and bundle bytes, but never owns the SSM deployment path."""
    text = _workflow_text("cd.yml")
    workflow = yaml.safe_load(text)
    jobs = workflow["jobs"]
    assert "environment" not in jobs["resolve"]
    assert "id-token" not in jobs["resolve"].get("permissions", {})
    for unit in ("findb", "fetcher"):
        assert "environment" not in jobs[f"publish-{unit}"]
        assert jobs[f"publish-{unit}"]["permissions"] == {"contents": "read", "id-token": "write"}
        assert (
            jobs[f"release-{unit}"]["environment"]
            == f"${{{{ needs.resolve.outputs.target }}}}-{unit}"
        )
        assert jobs[f"release-{unit}"]["permissions"] == {"contents": "read", "id-token": "write"}
        assert jobs[f"deploy-{unit}"]["permissions"] == {"contents": "read", "id-token": "write"}
        publish_block = _job_block(text, f"publish-{unit}")
        release_block = _job_block(text, f"release-{unit}")
        deploy_block = _job_block(text, f"deploy-{unit}")
        assert "role-to-assume: ${{ env.AWS_PUBLISHER_ROLE_ARN }}" in publish_block
        assert "role/" not in publish_block and "role/" not in release_block
        assert "role-to-assume: ${{ env.AWS_DEPLOY_ROLE_ARN }}" in release_block
        assert "aws s3api put-object" not in publish_block
        assert "aws s3api put-object" in release_block
        assert f"uses: ./.github/workflows/{unit}-deploy.yml" in deploy_block
        assert "runs-on:" not in deploy_block
        assert "aws ssm send-command" not in deploy_block
        assert "mode: ${{ needs.resolve.outputs.host_mode }}" in deploy_block
        assert (
            f"bundle_key: ${{{{ needs.release-{unit}.outputs.bundle_key || needs.resolve.outputs.{unit}_bundle_key }}}}"
            in deploy_block
        )
        assert f"{unit}/candidates/" in text
        assert f"{unit}/accepted/" in text
    assert "aws ssm send-command" not in text


def test_fetcher_deploys_only_after_findb_through_a_job_dependency() -> None:
    workflow = yaml.safe_load(_workflow_text("cd.yml"))
    fetcher = workflow["jobs"]["deploy-fetcher"]
    assert fetcher["needs"] == ["resolve", "release-fetcher", "deploy-findb"]
    assert (
        "needs.deploy-findb.result == 'success' || needs.deploy-findb.result == 'skipped'"
        in fetcher["if"]
    )
    findb = workflow["jobs"]["deploy-findb"]
    assert findb["needs"] == ["resolve", "release-findb"]
    text = _workflow_text("cd.yml")
    assert "actions/workflows/" not in text
    assert "gh run" not in text and "gh api" not in text


def test_reusable_deploy_concurrency_does_not_compete_with_its_caller() -> None:
    caller = yaml.safe_load(_workflow_text("cd.yml"))
    caller_group = caller["concurrency"]["group"]
    assert caller_group == "cd-${{ inputs.target || 'staging' }}"
    for unit in ("findb", "fetcher"):
        reusable = yaml.safe_load(_workflow_text(f"{unit}-deploy.yml"))
        reusable_group = reusable["jobs"]["deploy"]["concurrency"]["group"]
        assert reusable_group == f"${{{{ inputs.deployment_target }}}}-{unit}-deploy"
        for target in ("staging", "production"):
            assert (
                reusable_group.replace("${{ inputs.deployment_target }}", target) != f"cd-{target}"
            )


def test_reusable_workflows_accept_staging_candidates_before_activation() -> None:
    for unit in ("findb", "fetcher"):
        text = _workflow_text(f"{unit}-deploy.yml")
        assert f"^{unit}/candidates/${{EXPECTED_SHA}}/" in text
        assert "deployment_target:$target" in text
        assert 'release_tag == ""' not in text  # The record's tag is compared to the input.
        assert ".release_tag == $release_tag" in text
        assert "staging-v1-bundle-validator.py" not in text
        assert "jq -r .validator_sha256" not in text
        assert text.index("Persist immutable target-aware acceptance record") < text.index(
            "Activate only after immutable acceptance is durable"
        )


def test_staging_image_builds_use_step_outputs_and_buildx() -> None:
    text = _workflow_text("cd.yml")
    assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" in text
    assert text.count("build_ecr_image_if_missing.sh") == 5
    assert "outputs.image_ref" in text
    assert "sed -n 's/^image_ref=//p'" not in text
    for repository in ("twelve-data", "finlab", "shioaji"):
        assert f'"$ECR_REGISTRY/findb/staging/fetcher/{repository}"' in text
    for repository, dockerfile in (
        ("twelve-data", "Dockerfile"),
        ("finlab", "Dockerfile.finlab"),
        ("shioaji", "Dockerfile.shioaji"),
    ):
        assert (
            f'build_ecr_image_if_missing.sh "$ECR_REGISTRY/findb/staging/fetcher/{repository}" . ./fetcher/{dockerfile}'
            in text
        )
    assert (
        'build_ecr_image_if_missing.sh "$ECR_REGISTRY/findb/staging/backend" ./backend ./backend/Dockerfile'
        in text
    )
    assert (
        'build_ecr_image_if_missing.sh "$ECR_REGISTRY/findb/staging/dashboard" . ./dashboard/Dockerfile'
        in text
    )
    ci = _workflow_text("fetcher-ci.yml")
    for dockerfile in ("Dockerfile", "Dockerfile.finlab", "Dockerfile.shioaji"):
        assert f"docker build -f fetcher/{dockerfile}" in ci
    assert ci.count("docker build -f fetcher/") == 3


def test_reusable_preflight_is_before_deploy_and_checks_host_boundaries() -> None:
    for unit in ("findb", "fetcher"):
        text = _workflow_text(f"{unit}-deploy.yml")
        assert text.index("Run read-only SSM preflight") < text.index(
            "Run candidate acceptance or accepted replay"
        )
        assert "aws s3api get-object" in text
        assert "materialize-bundle" in text
        assert 'docker --config \\"\\$work/docker\\" pull' in text
        assert "load_runtime_secrets.py" in text
        assert (
            f"--output /run/findb-runtime-secrets/preflight-{unit}/runtime.env --check-only" in text
        )
        assert '--output \\"\\$work/runtime.env\\" --check-only' not in text
        preflight_block = re.search(
            r"- name: Run read-only SSM preflight(?P<body>.*?)(?=\n      - name:)",
            text,
            re.S,
        ).group("body")
        if unit == "fetcher":
            assert "check_fetcher_host.py" in preflight_block
            assert (
                "--target $q_target --registry $q_registry --installed-validator" in preflight_block
            )
            assert "--full-market-state-dir $q_state_dir" in preflight_block
            assert "q_host_guard_sha" in preflight_block
        else:
            assert "if ! docker ps --format '{{.Names}}' | grep -Eq" in preflight_block
            assert "$q_target = production" not in preflight_block
            assert 'then [ -z \\"\\$(docker ps -aq)\\" ]; fi' in preflight_block
            assert "--consumer nginx" in preflight_block
        assert "phase1-preflight-denial-probe" not in preflight_block
        cross_unit = "fetcher" if unit == "findb" else "findb"
        assert (
            f"--secret-id findb/$q_target/{cross_unit}/runtime/configuration >/dev/null 2>&1"
            in preflight_block
        )
        assert "DNS_CHECK_NAME: ${{ env.FINDB_PUBLIC_HOST }}" in preflight_block

    findb_text = _workflow_text("findb-deploy.yml")
    assert "docker compose" in findb_text
    assert "config --no-interpolate -q" in findb_text
    assert "FINDB_NGINX_CONFIG_DIR=/etc/findb/nginx" in findb_text
    assert "/home/ubuntu/etc/nginx" not in findb_text
    preflight_block = re.search(
        r"- name: Run read-only SSM preflight(?P<body>.*?)(?=\n      - name:)",
        findb_text,
        re.S,
    ).group("body")
    assert "MIGRATION: ${{ steps.release.outputs.migration }}" in preflight_block
    assert "printf -v q_migration '%q' \"$MIGRATION\"" in preflight_block
    assert "--migration-revision $q_migration" in preflight_block

    fetcher_text = _workflow_text("fetcher-deploy.yml")
    assert "docker compose" not in fetcher_text
    for helper in (
        "runtime_secret_command.sh",
        "deploy_fetcher_aws.sh",
        "release_fetcher_provider.sh",
    ):
        assert f'\\"\\$work/release/infra/deploy/runtime-secrets/{helper}\\"' in fetcher_text
    assert "bash -n" in fetcher_text


def test_reusable_deployments_forward_only_control_plane_values_to_the_host() -> None:
    contracts_by_unit = {
        "findb": ("FULL_MARKET_ENABLED",),
        "fetcher": ("FULL_MARKET_ENABLED", "FETCHER_FULL_MARKET_STATE_DIR"),
    }
    application_names = (
        "APP_NAME",
        "PORT",
        "SOURCE_ALLOWLIST_CIDRS",
        "FETCHER_SOURCE_API_URL",
        "SHIOAJI_SIMULATION",
        "CLOUDFLARE_R2_RAW_BUCKET",
        "CLOUDFLARE_R2_CANONICAL_BUCKET",
    )
    for unit, expected_names in contracts_by_unit.items():
        text = _workflow_text(f"{unit}-deploy.yml")
        workflow = yaml.safe_load(text)
        assert "inputs.deployment_target == 'staging'" not in text
        for name in application_names:
            assert f"{name}=" not in text and f"{name}:" not in text
        for step_name in (
            "Run candidate acceptance or accepted replay through bounded SSM",
            "Activate only after immutable acceptance is durable",
        ):
            step = _step(workflow, "deploy", step_name)
            match = re.search(r'^runtime_variable_names="([A-Z0-9_ ]+)"$', step["run"], re.M)
            assert match
            assert tuple(match.group(1).split()) == expected_names
            assert '[ -n "${!name:-}" ] || exit 1' in step["run"]
            assert 'runtime_exports+=" $name=$q_runtime_value"' in step["run"]
            assert "$runtime_exports" in step["run"]
            assert 'if [ "$TARGET" = staging ]' not in step["run"]
    findb_helper = (ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text()
    pull_script = findb_helper.split("<<'PULL_SCRIPT'\n", 1)[1].split("\nPULL_SCRIPT", 1)[0]
    assert 'if [ "${PORT:-}" != 8080 ]; then' in pull_script
    assert "port_contract_invalid" in pull_script


def test_every_ssm_run_shell_command_is_explicitly_wrapped_for_bash() -> None:
    wrapper = "python3 infra/deploy/ssm_bash_command.py"
    for unit in ("findb", "fetcher"):
        text = _workflow_text(f"{unit}-deploy.yml")
        assert text.count("--document-name AWS-RunShellScript") == 3
        assert text.count(wrapper) == 3
    smoke = _workflow_text("fetcher-finlab-smoke.yml")
    assert smoke.count("--document-name AWS-RunShellScript") == 1
    assert smoke.count(wrapper) == 1
    cd = _workflow_text("cd.yml")
    assert "--document-name AWS-RunShellScript" not in cd


def test_bundle_validation_never_passes_an_incomplete_exact_input_contract() -> None:
    for workflow_name in ("findb-deploy.yml", "fetcher-deploy.yml", "cd.yml"):
        text = _workflow_text(workflow_name)
        exact_calls = [
            line
            for line in text.splitlines()
            if ("validate-bundle" in line or "materialize-bundle" in line)
            and "--commit-sha" in line
        ]
        assert exact_calls
        for call in exact_calls:
            assert "--migration-revision" in call
            assert "--image" in call


def test_environment_contract_marker_is_required_in_every_mode() -> None:
    for unit in ("findb", "fetcher"):
        text = _workflow_text(f"{unit}-deploy.yml")
        assert "legacy_environment_bridge" not in text
        assert "DEPLOYMENT_TARGET" not in text
        assert text.count("# findb_environment_contract=app-environment-v1") == 2
        ssm = text.split("- name: Run candidate acceptance or accepted replay", 1)[1]
        assert "environment_contract_gate='grep -qx" in ssm
        assert '$environment_contract_gate \\"\\$root/infra/deploy/runtime-secrets/deploy_' in ssm


def test_cd_candidate_contracts_are_main_push_or_confirmed_manual_candidate_or_exact_replay() -> (
    None
):
    text = _workflow_text("cd.yml")
    resolve = _job_block(text, "resolve")
    assert 'if [ "$EVENT" = push ]; then' in resolve
    assert "infra/deploy/change_policy.py" in resolve
    assert '[ "$CONFIRMATION" = deploy-new-candidate ]' in resolve
    assert '[ -z "$CONFIRMATION" ]' in resolve
    assert "/accepted/([0-9a-f]{40})/[0-9a-f]{64}[.]tar$" in resolve
    for unit in ("findb", "fetcher"):
        release = _job_block(text, f"release-{unit}")
        assert f"{unit}/candidates/${{REVISION}}/" in release
        assert "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${sha}.tar" in release
        assert "--if-none-match '*'" in release
        assert "--server-side-encryption aws:kms" in release
        assert '--ssekms-key-id "$DEPLOY_BUNDLE_KMS_KEY_ARN"' in release


def test_reusable_records_and_ssm_gates_are_immutable_and_bounded() -> None:
    for unit in ("findb", "fetcher"):
        text = _workflow_text(f"{unit}-deploy.yml")
        assert "timeout-minutes: 90" in text
        assert "--if-none-match '*'" in text
        assert "ssm_command_marker_state" in text
        assert "CloudWatchOutputEnabled=true,CloudWatchLogGroupName=" in text
        assert "status=success" in text and "status=failed" in text
        assert (
            'ssm_command_id | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
            '[0-9a-f]{4}-[0-9a-f]{12}$")' in text
        )
        assert (
            "FINDB_DEPLOY_MODE=$q_host_mode" in text or "FETCHER_DEPLOY_MODE=$q_host_mode" in text
        )
        assert (
            text.index("Run candidate acceptance or accepted replay")
            < text.index("Persist immutable target-aware acceptance record")
            < text.index("Activate only after immutable acceptance is durable")
        )


def test_fetcher_smoke_is_exact_hardened_and_non_destructive() -> None:
    text = _workflow_text("fetcher-finlab-smoke.yml")
    assert "options: ['2330,2317']" in text
    assert "workflow_dispatch" in text and "push:" not in text
    assert "environment: staging-fetcher" in text
    assert "readlink -f /opt/fetcher/current" in text
    assert "jq -r .images.finlab" in text
    assert "/findb/staging/fetcher/finlab@sha256:[0-9a-f]{64}" in text
    assert "chown 10001:10001" in text
    assert "--user 10001:10001" in text
    assert "vars." not in text
    for prune_command in ("docker image prune", "docker system prune", "docker volume prune"):
        assert prune_command not in text


def test_promotion_helper_has_copy_collision_and_destination_digest_guards() -> None:
    helper = ROOT / "infra" / "deploy" / "promote_image.sh"
    completed = subprocess.run(
        ["bash", "-n", str(helper)], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 0
    text = helper.read_text()
    assert "imagetools create" in text and "tag_collision" in text
    assert "destination_digest_mismatch" in text and "state=idempotent" in text


@pytest.mark.parametrize(
    ("mode", "expected_code", "expected_marker"),
    [
        ("missing", 0, "state=copied"),
        ("same", 0, "state=idempotent"),
        ("collision", 1, "reason=tag_collision"),
        ("auth", 1, "reason=destination_inspect_failed"),
    ],
)
def test_promotion_helper_fails_closed_or_is_idempotent(
    tmp_path: Path, mode: str, expected_code: int, expected_marker: str
) -> None:
    """Exercise registry collision/partial-rerun behavior without Docker."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "created"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ $* == *'imagetools inspect'* ]]; then\n"
        "  case ${PROMOTION_TEST_MODE:?} in\n"
        '    same) printf \'"sha256:%s"\\n\' "${PROMOTION_DIGEST:?}";;\n'
        "    collision) printf '\"sha256:%064d\"\\n' 0;;\n"
        '    missing) if [ -f "${PROMOTION_STATE:?}" ]; then printf \'"sha256:%s"\\n\' "${PROMOTION_DIGEST:?}"; else echo \'manifest unknown\' >&2; exit 1; fi;;\n'
        "    auth) echo 'denied: authorization failed' >&2; exit 1;;\n"
        "  esac\n"
        "else\n"
        '  : > "${PROMOTION_STATE:?}"\n'
        "fi\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    digest = "a" * 64
    result = subprocess.run(
        ["bash", str(ROOT / "infra/deploy/promote_image.sh")],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PROMOTION_TEST_MODE": mode,
            "PROMOTION_DIGEST": digest,
            "PROMOTION_STATE": str(state),
            "SOURCE_IMAGE_REF": f"111111111111.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/backend@sha256:{digest}",
            "DESTINATION_REPOSITORY": "222222222222.dkr.ecr.ap-southeast-1.amazonaws.com/findb/production/backend",
            "RELEASE_TAG": "v1.2.3",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_code
    assert expected_marker in result.stdout + result.stderr


def _write_fake_binding_aws(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "binding-bin"
    fake_bin.mkdir()
    aws = fake_bin / "aws"
    aws.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ "$1" = sts ]; then\n'
        "  printf '%s\\n' \"${FAKE_AWS_ACCOUNT:?}\"\n"
        "  exit 0\n"
        "fi\n"
        '[ "$1" = s3api ] || exit 64\n'
        "operation=$2\n"
        "shift 2\n"
        "bucket= key= body= output=\n"
        "while [ $# -gt 0 ]; do\n"
        "  case $1 in\n"
        "    --bucket) bucket=$2; shift 2;;\n"
        "    --key) key=$2; shift 2;;\n"
        "    --body) body=$2; shift 2;;\n"
        "    --if-none-match|--expected-bucket-owner|--server-side-encryption|--ssekms-key-id) shift 2;;\n"
        "    *) output=$1; shift;;\n"
        "  esac\n"
        "done\n"
        'target="${FAKE_S3_ROOT:?}/$bucket/$key"\n'
        "case $operation in\n"
        "  put-object)\n"
        '    if [ "${FAKE_RACE_MODE:-}" = same ] || [ "${FAKE_RACE_MODE:-}" = different ]; then\n'
        '      mkdir -p "$(dirname "$target")"\n'
        '      cp "${FAKE_RACE_FILE:?}" "$target"\n'
        "      exit 255\n"
        "    fi\n"
        '    [ ! -e "$target" ] || exit 255\n'
        '    mkdir -p "$(dirname "$target")"\n'
        '    cp "$body" "$target"\n'
        "    ;;\n"
        "  get-object)\n"
        '    [ -f "$target" ] || exit 255\n'
        '    cp "$target" "$output"\n'
        "    ;;\n"
        "  *) exit 64;;\n"
        "esac\n",
        encoding="utf-8",
    )
    aws.chmod(0o755)
    return fake_bin


def _binding_environment(
    tmp_path: Path,
    fake_bin: Path,
    *,
    unit: str = "findb",
    mode: str = "promote",
    release_tag: str = "findb-v1.2.3",
    tag_ref_oid: str = "a" * 40,
    commit_sha: str = "b" * 40,
    source_bundle_key: str | None = None,
    **extra: str,
) -> dict[str, str]:
    source_bundle_key = source_bundle_key or f"{unit}/accepted/{commit_sha}/{'c' * 64}.tar"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "FAKE_AWS_ACCOUNT": "123456789012",
        "FAKE_S3_ROOT": str(tmp_path / "fake-s3"),
        "UNIT": unit,
        "MODE": mode,
        "RELEASE_TAG": release_tag,
        "TAG_REF_OID": tag_ref_oid,
        "COMMIT_SHA": commit_sha,
        "AWS_REGION": "ap-southeast-1",
        "ACCOUNT_ID": "123456789012",
        "BUNDLE_BUCKET": "findb-production-deploy-bundle",
        "BUNDLE_KMS_KEY": "arn:aws:kms:ap-southeast-1:123456789012:key/01234567-89ab-cdef-0123-456789abcdef",
        **extra,
    }
    if mode == "promote":
        environment["SOURCE_BUNDLE_KEY"] = source_bundle_key
    return environment


def _run_binding(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "infra/deploy/bind_release_tag.sh")],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _binding_object_path(tmp_path: Path, unit: str, release_tag: str) -> Path:
    return (
        tmp_path
        / "fake-s3"
        / "findb-production-deploy-bundle"
        / unit
        / "production"
        / "release-tags"
        / f"{release_tag}.binding.json"
    )


def test_release_tag_binding_creates_once_and_rejects_identity_drift(tmp_path: Path) -> None:
    fake_bin = _write_fake_binding_aws(tmp_path)
    environment = _binding_environment(tmp_path, fake_bin)

    first = _run_binding(environment)
    assert first.returncode == 0
    assert "release_tag_binding=created" in first.stdout
    binding = _binding_object_path(tmp_path, "findb", "findb-v1.2.3")
    assert json.loads(binding.read_text()) == {
        "schema_version": 1,
        "unit": "findb",
        "release_tag": "findb-v1.2.3",
        "tag_ref_oid": "a" * 40,
        "commit_sha": "b" * 40,
        "staging_accepted_bundle_key": f"findb/accepted/{'b' * 40}/{'c' * 64}.tar",
    }

    same = _run_binding(environment)
    assert same.returncode == 0
    assert "release_tag_binding=validated" in same.stdout

    moved_tag = _run_binding(_binding_environment(tmp_path, fake_bin, tag_ref_oid="d" * 40))
    assert moved_tag.returncode != 0
    assert "reason=binding_mismatch" in moved_tag.stderr

    changed_commit = _run_binding(_binding_environment(tmp_path, fake_bin, commit_sha="e" * 40))
    assert changed_commit.returncode != 0
    assert "reason=binding_mismatch" in changed_commit.stderr

    changed_source = _run_binding(
        _binding_environment(
            tmp_path,
            fake_bin,
            source_bundle_key=f"findb/accepted/{'b' * 40}/{'f' * 64}.tar",
        )
    )
    assert changed_source.returncode != 0
    assert "reason=binding_mismatch" in changed_source.stderr


def test_release_tag_binding_rejects_cross_unit_tag_or_source_key(tmp_path: Path) -> None:
    fake_bin = _write_fake_binding_aws(tmp_path)
    cases = (
        _binding_environment(tmp_path, fake_bin, unit="unknown"),
        _binding_environment(tmp_path, fake_bin, unit="fetcher"),
        _binding_environment(tmp_path, fake_bin, release_tag="fetcher-v1.2.3"),
        _binding_environment(
            tmp_path,
            fake_bin,
            source_bundle_key=f"fetcher/accepted/{'b' * 40}/{'c' * 64}.tar",
        ),
    )
    for environment in cases:
        result = _run_binding(environment)
        assert result.returncode != 0
        assert "release_tag_binding=failed" in result.stderr


def test_release_tag_binding_rollback_requires_existing_unmoved_binding(tmp_path: Path) -> None:
    fake_bin = _write_fake_binding_aws(tmp_path)
    missing = _run_binding(_binding_environment(tmp_path, fake_bin, mode="rollback"))
    assert missing.returncode != 0
    assert "reason=binding_missing_or_unreadable" in missing.stderr

    assert _run_binding(_binding_environment(tmp_path, fake_bin)).returncode == 0
    moved_tag = _run_binding(
        _binding_environment(tmp_path, fake_bin, mode="rollback", tag_ref_oid="d" * 40)
    )
    assert moved_tag.returncode != 0
    assert "reason=binding_mismatch" in moved_tag.stderr


@pytest.mark.parametrize(
    ("race_mode", "expected_code", "expected_marker"),
    [("same", 0, "release_tag_binding=validated"), ("different", 1, "reason=binding_mismatch")],
)
def test_release_tag_binding_validates_conditional_put_race_winner(
    tmp_path: Path, race_mode: str, expected_code: int, expected_marker: str
) -> None:
    fake_bin = _write_fake_binding_aws(tmp_path)
    environment = _binding_environment(tmp_path, fake_bin)
    race_payload = {
        "schema_version": 1,
        "unit": "findb",
        "release_tag": "findb-v1.2.3",
        "tag_ref_oid": "a" * 40,
        "commit_sha": "b" * 40,
        "staging_accepted_bundle_key": f"findb/accepted/{'b' * 40}/{'c' * 64}.tar",
    }
    if race_mode == "different":
        race_payload["tag_ref_oid"] = "d" * 40
    race_file = tmp_path / f"race-{race_mode}.json"
    race_file.write_text(json.dumps(race_payload), encoding="utf-8")
    environment.update({"FAKE_RACE_MODE": race_mode, "FAKE_RACE_FILE": str(race_file)})

    result = _run_binding(environment)
    assert result.returncode == expected_code
    assert expected_marker in result.stdout + result.stderr
