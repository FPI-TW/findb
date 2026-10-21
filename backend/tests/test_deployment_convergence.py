"""Focused contracts for converged CI/CD policy and promotion primitives."""

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = _module("change_policy", "infra/deploy/change_policy.py")
manifest = _module("release_manifest_v2", "infra/deploy/release_manifest.py")
contracts = manifest.load_contract_manifest(ROOT / "contracts" / "manifest.json")


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["contracts/market_eod/v1.schema.json"], (True, True, False, False, False)),
        (["backend/app/main.py"], (True, False, True, False, False)),
        (["fetcher/src/main.py"], (True, True, False, True, False)),
        (["infra/deploy/release_manifest.py"], (True, True, True, True, False)),
        (["infra/tofu/staging/main.tf"], (True, False, False, False, True)),
        (["infra/env/production/findb/remote.env.example"], (True, False, False, False, True)),
        (["infra/env/aws_configuration.py"], (True, False, False, False, True)),
        (["infra/monitoring/publish_staging_metrics.py"], (True, False, False, False, False)),
        (["infra/acceptance/native_feed_evidence.py"], (True, False, False, False, False)),
        (
            ["infra/acceptance/export_native_fetcher_evidence.py"],
            (True, True, False, False, False),
        ),
        ([".github/actions/aws-configuration/action.yml"], (True, True, True, True, False)),
        ([".github/workflows/findb-ci.yml"], (True, True, False, False, False)),
        ([".github/workflows/findb-cd.yml"], (True, True, True, False, False)),
        ([".github/workflows/findb-deploy.yml"], (True, True, True, False, False)),
        ([".github/workflows/fetcher-cd.yml"], (True, True, False, True, False)),
        ([".github/workflows/fetcher-deploy.yml"], (True, True, False, True, False)),
        ([".github/workflows/fetcher-production-cd.yml"], (True, True, False, False, False)),
        ([".github/workflows/production-promote.yml"], (True, True, False, False, False)),
        ([".github/workflows/required-ci.yml"], (True, True, False, False, False)),
        (["docs/operations/deployment.md"], (False, False, False, False, False)),
        ([""], (False, False, False, False, False)),
    ],
)
def test_change_policy_truth_table(paths: list[str], expected: tuple[bool, ...]) -> None:
    result = policy.classify(paths)
    assert tuple(result[key] for key in policy.OUTPUTS) == expected


def test_change_policy_full_selection_without_trusted_base(tmp_path: Path) -> None:
    output = tmp_path / "output"
    assert policy.main(["--full", "--github-output", str(output)]) == 0
    assert output.read_text().splitlines() == [f"{key}=true" for key in policy.OUTPUTS]


@pytest.mark.parametrize(
    "argv",
    [[], ["--base", "a" * 40], ["--full", "--base", "a" * 40, "--head", "b" * 40]],
)
def test_change_policy_requires_exactly_one_selection_mode(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        policy.main(argv)
    assert exc.value.code == 2


def test_staging_cd_diffs_from_last_successful_push_rollout() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        job = text.split("  policy:", 1)[1].split("  verify:", 1)[0]
        assert "${{ github.event.before }}" not in job
        assert "actions: read" in job
        assert (
            f"actions/workflows/{unit}-cd.yml/runs?branch=main&event=push&status=success"
        ) in job
        assert 'git merge-base --is-ancestor "$base" "$HEAD"' in job
        assert "change_policy.py --full" in job


def test_staging_accepted_replay_skips_ci_but_candidates_require_it() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        policy_job = text.split("  policy:", 1)[1].split("  verify:", 1)[0]
        assert 'ci=true; [ "$DEPLOYMENT_MODE" = replay ] && ci=false' in policy_job
        select = text.split("  select:", 1)[1].split("    runs-on:", 1)[0]
        assert "!cancelled()" in select
        assert "needs.policy.result == 'success'" in select
        assert (
            "needs.verify.result == 'skipped' && github.event_name == 'workflow_dispatch'"
            " && inputs.deployment_mode == 'replay'"
        ) in select


def test_staging_cd_reuses_identical_tree_pull_request_ci_only_on_push() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        jobs = yaml.safe_load(text)["jobs"]
        policy_job = jobs["policy"]
        assert policy_job["permissions"] == {
            "contents": "read",
            "actions": "read",
            "pull-requests": "read",
        }
        assert policy_job["outputs"][f"{unit}_ci_evidence"] == (
            f"${{{{ steps.policy.outputs.{unit}_ci_evidence }}}}"
        )
        script = next(step for step in policy_job["steps"] if step.get("id") == "policy")["run"]
        dispatch = script.split("exit 0", 1)[0]
        assert "ci_evidence.py" not in dispatch
        evidence = script.split("change_policy.py --full", 1)[1]
        assert f"grep -qx '{unit}_ci=true' \"$GITHUB_OUTPUT\"" in evidence
        assert (
            f'infra/deploy/ci_evidence.py --repository "$GITHUB_REPOSITORY" --revision "$HEAD" '
            f"--unit {unit} --ci-workflow .github/workflows/{unit}-ci.yml"
        ) in evidence
        assert jobs["verify"]["if"] == (
            f"${{{{ needs.policy.outputs.{unit}_ci == 'true' && "
            f"needs.policy.outputs.{unit}_ci_evidence == '' }}}}"
        )
        assert (
            "needs.verify.result == 'skipped' && github.event_name == 'push' && "
            f"needs.policy.outputs.{unit}_ci_evidence != ''"
        ) in jobs["select"]["if"]


def test_staging_cd_jobs_after_an_optional_verify_override_skip_propagation() -> None:
    # Without a status function, `if` implies success(), which is false once any
    # transitive dependency was skipped; reused CI evidence skips verify on push.
    for unit in ("findb", "fetcher"):
        jobs = yaml.safe_load((ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text())[
            "jobs"
        ]

        def needs(name: str) -> set[str]:
            direct = jobs[name].get("needs", [])
            direct = [direct] if isinstance(direct, str) else direct
            return set(direct).union(*(needs(parent) for parent in direct))

        downstream = [name for name in jobs if "verify" in needs(name)]
        assert {"select", "publish", "prepare", "deploy"} <= set(downstream)
        for name in downstream:
            condition = str(jobs[name].get("if", ""))
            assert "!cancelled()" in condition or "always()" in condition, (unit, name)


def _v2_manifest(target: str) -> dict[str, object]:
    registry = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com"
    unit = "findb"
    images = {
        name: f"{repo}@sha256:{'b' * 64}"
        for name, repo in manifest.image_repositories(target, unit, registry).items()
    }
    result: dict[str, object] = {
        "schema_version": 3,
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
            "staging_registry": manifest.REGISTRY,
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


def test_production_workflows_are_manual_and_do_not_contain_legacy_transport() -> None:
    workflows = ROOT / ".github" / "workflows"
    release = (workflows / "production-release.yml").read_text()
    promote = (workflows / "production-promote.yml").read_text()
    for shared in (release, promote):
        assert set(yaml.safe_load(shared)["on"]) == {"workflow_call"}
    assert 'test "$GITHUB_REF" = refs/heads/main' in release
    assert '[ "$CONFIRMATION" = "PROMOTE_${UNIT^^}_PRODUCTION" ]' in release
    assert "STAGING_PROMOTION_READ_ROLE_ARN" in release
    assert "AWS_PROMOTION_ROLE_ARN" in release
    assert "promote_image.sh" in promote and "docker build" not in promote
    assert "STAGING_PROMOTION_READ_ROLE_ARN" in promote
    assert "AWS_PROMOTION_ROLE_ARN" in promote
    assert '.registry "$RUNNER_TEMP/staging-manifest.json"' in promote
    assert '.account_id "$RUNNER_TEMP/staging-manifest.json"' in promote
    assert '[ "$SOURCE_ACCOUNT" != "$DESTINATION_ACCOUNT" ]' in promote
    assert 'candidate_key="${UNIT}/production/candidates/' in promote
    for unit in ("findb", "fetcher"):
        text = (workflows / f"{unit}-production-cd.yml").read_text()
        assert set(yaml.safe_load(text)["on"]) == {"workflow_dispatch"}
        assert "PRODUCTION_DEPLOY_ENABLED" not in text
        assert f"PROMOTE_{unit.upper()}_PRODUCTION" in text
        jobs = yaml.safe_load(text)["jobs"]
        assert jobs["release"]["with"]["unit"] == unit
        assert jobs["promote"]["with"]["unit"] == unit
        for combined in (text, release, promote):
            assert "secrets: inherit" not in combined


def test_production_promotion_maps_each_unit_to_its_exact_image_set() -> None:
    promote = yaml.safe_load((ROOT / ".github/workflows/production-promote.yml").read_text())
    steps = promote["jobs"]["promote"]["steps"]
    unit_step = next(step for step in steps if step.get("id") == "unit")["run"]
    assert "findb) echo 'images=backend:backend dashboard:dashboard'" in unit_step
    assert (
        "fetcher) echo 'images=twelve_data:fetcher/twelve-data finlab:fetcher/finlab "
        "shioaji:fetcher/shioaji factset:fetcher/factset'"
    ) in unit_step
    assert "*) exit 1 ;;" in unit_step
    validate = next(
        step
        for step in steps
        if step.get("name") == "Download and validate exact accepted staging bundle"
    )["run"]
    assert '[ "${image_ref%@*}" = "$SOURCE_REGISTRY/findb/staging/${entry#*:}" ]' in validate
    assert '[ "$UNIT" != fetcher ] || [ "$migration" = none ]' in validate
    bundle = next(step for step in steps if step.get("id") == "bundle")["run"]
    assert 'destination_repository="$REGISTRY/findb/production/${entry#*:}"' in bundle
    assert 'short_tag="${RELEASE_TAG#"$UNIT"-}"' in bundle
    assert "profile_args=(--runtime-profile unified)" in bundle


def test_production_release_binding_precedes_all_deploy_paths():
    workflows = ROOT / ".github/workflows"
    release_text = (workflows / "production-release.yml").read_text()
    release = yaml.safe_load(release_text)
    assert set(release["on"]["workflow_call"]["outputs"]) == {"revision", "tag_ref_oid"}
    jobs = release["jobs"]
    assert "tag_ref_oid" in jobs["resolve"]["outputs"]
    binding = jobs["bind-release-tag"]
    names = [step.get("name") for step in binding["steps"]]
    assert names.index("Verify exact accepted staging binding source") < names.index(
        "Bind or validate immutable release tag"
    )
    assert binding["needs"] == "resolve"
    assert 'git rev-parse "refs/tags/$RELEASE_TAG^{commit}"' in release_text
    assert '[ "$(git cat-file -t "refs/tags/$RELEASE_TAG")" = tag ]' in release_text
    for unit in ("findb", "fetcher"):
        caller = yaml.safe_load((workflows / f"{unit}-production-cd.yml").read_text())["jobs"]
        assert caller["release"]["uses"] == "./.github/workflows/production-release.yml"
        for name, job in caller.items():
            if "uses" in job and name != "release":
                assert "release" in job["needs"]


def test_production_release_tags_require_annotated_object_identity(tmp_path: Path) -> None:
    repository = tmp_path / "tag-repository"
    repository.mkdir()
    # Ignore developer/system git config (signing, hooks, templates) so the
    # temporary repository behaves identically on every machine and in CI.
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def git(*args: str) -> str:
        return subprocess.check_output(
            (
                "git",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "tag.gpgSign=false",
                "-c",
                "user.name=FinDB Test",
                "-c",
                "user.email=findb-test@example.invalid",
                *args,
            ),
            cwd=repository,
            env=environment,
            text=True,
        ).strip()

    git("init", "-q")
    (repository / "release.txt").write_text("release\n", encoding="utf-8")
    git("add", "release.txt")
    git("commit", "-qm", "release")
    commit_sha = git("rev-parse", "HEAD")

    git("tag", "findb-v1.0.0")
    assert git("cat-file", "-t", "refs/tags/findb-v1.0.0") == "commit"

    git("tag", "-a", "findb-v1.0.1", "-m", "FinDB v1.0.1")
    assert git("cat-file", "-t", "refs/tags/findb-v1.0.1") == "tag"
    assert git("rev-parse", "refs/tags/findb-v1.0.1") != commit_sha
    assert git("rev-parse", "refs/tags/findb-v1.0.1^{commit}") == commit_sha


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
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
        assert "inputs.mode == 'candidate'" in text
        assert f"{unit}/production/accepted/" in text
        assert 'state:"accepted"' in text
        assert "ssm_wait_command" in text
        assert "PRODUCTION_DEPLOY_ENABLED" not in text
        assert 'test "$GITHUB_REF" = refs/heads/main' in text
        assert 'bundle_sha256="${bundle_filename##*-}"' in text
        assert 'infra/deploy/release_manifest.py > \\"\\$validator\\"' in text
        assert "VALIDATOR_SHA256" in text
        assert "aws sts get-caller-identity --query Account" in text
        assert "getent ahostsv4 $q_dns" in text
        assert f"/opt/{unit}/current/infra/deploy/release_manifest.py" not in text


def test_production_candidate_record_precedes_activation_and_bootstrap_has_target_contract() -> (
    None
):
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
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


def test_staging_cd_has_exact_revision_policy_and_no_legacy_transport() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        assert "infra/deploy/change_policy.py" in text
        assert "revision: ${{ needs.prepare.outputs.revision }}" in text
        assert "Verify exact requested revision" in text


def test_containerized_dashboard_ci_marks_only_the_fixed_workspace_safe() -> None:
    workflow = (ROOT / ".github" / "workflows" / "findb-ci.yml").read_text()
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
                "WANT_INFRA": "true",
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
                "WANT_INFRA": "false",
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
                "WANT_INFRA": "true",
            },
            1,
        ),
        (
            {
                "POLICY": "success",
                "FINDB": "success",
                "FETCHER": "skipped",
                "WANT_FINDB": "true",
                "WANT_FETCHER": "true",
                "INFRA": "success",
                "WANT_INFRA": "true",
            },
            1,
        ),
    ],
)
def test_required_ci_aggregator_truth_table(
    environment: dict[str, str], expected_code: int
) -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "required-ci.yml").read_text())
    assert workflow["jobs"]["required"]["name"] == "Required CI"
    script = workflow["jobs"]["required"]["steps"][0]["run"]
    completed = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, **environment},
        check=False,
    )
    assert completed.returncode == expected_code


def test_staging_callers_delegate_deployment_to_reusable_workflow() -> None:
    """The staging callers may prepare bytes, but may not own the SSM deployment path."""
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        workflow = yaml.safe_load(text)
        jobs = workflow["jobs"]
        assert "environment" not in jobs["select"]
        assert jobs["select"]["permissions"].get("id-token") is None
        assert "environment" not in jobs["publish"]
        assert jobs["publish"]["permissions"] == {
            "contents": "read",
            "id-token": "write",
        }
        assert "environment" not in jobs["prepare"]
        prepare_permissions = {
            "contents": "read",
            "id-token": "write",
        }
        if unit == "fetcher":
            prepare_permissions["actions"] = "read"
        assert jobs["prepare"]["permissions"] == prepare_permissions
        assert workflow["jobs"]["deploy"]["permissions"] == {
            "contents": "read",
            "id-token": "write",
        }
        publish_block = re.search(
            r"^  publish:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text, re.M | re.S
        ).group("body")
        prepare_block = re.search(
            r"^  prepare:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text, re.M | re.S
        ).group("body")
        assert f"role/{unit}-staging-ecr-publisher" in publish_block
        assert f"role/{unit}-staging-ecr-publisher" not in prepare_block
        assert "aws-configuration" in prepare_block
        assert "aws s3api put-object" not in publish_block
        assert "aws s3api put-object" in prepare_block
        deploy_block = re.search(
            r"^  deploy:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text, re.M | re.S
        ).group("body")
        assert f"uses: ./.github/workflows/{unit}-deploy.yml" in deploy_block
        assert "runs-on:" not in deploy_block
        assert "aws ssm send-command" not in deploy_block
        assert "mode: ${{ needs.prepare.outputs.mode }}" in deploy_block
        assert "bundle_key: ${{ needs.prepare.outputs.bundle_key }}" in deploy_block
        assert f"{unit}/candidates/" in text
        assert f"{unit}/accepted/" in text


def test_reusable_deploy_concurrency_does_not_compete_with_its_caller() -> None:
    for unit in ("findb", "fetcher"):
        reusable = yaml.safe_load(
            (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
        )
        reusable_group = reusable["jobs"]["deploy"]["concurrency"]["group"]
        assert reusable_group == f"${{{{ inputs.deployment_target }}}}-{unit}-deploy"
        for target, caller_name in (
            ("staging", f"{unit}-cd.yml"),
            ("production", f"{unit}-production-cd.yml"),
        ):
            caller = yaml.safe_load((ROOT / ".github" / "workflows" / caller_name).read_text())
            caller_group = caller["concurrency"]["group"]
            assert caller_group == f"{target}-{unit}"
            assert reusable_group.replace("${{ inputs.deployment_target }}", target) != caller_group


def test_reusable_workflows_accept_staging_candidates_before_activation() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
        assert f"^{unit}/candidates/${{EXPECTED_SHA}}/" in text
        assert "deployment_target:$target" in text
        assert 'release_tag == ""' in text
        assert "v1" in text and "staging" in text
        assert text.index("Persist immutable target-aware acceptance record") < text.index(
            "Activate only after immutable acceptance is durable"
        )


def test_staging_image_builds_use_step_outputs_and_buildx() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        assert "docker/setup-buildx-action@" in text
        assert "build_ecr_image_if_missing.sh" in text
        assert "outputs.image_ref" in text
        publish = text.split("  publish:", 1)[1].split("  prepare:", 1)[0]
        assert "      ECR_BUILD_CACHE: gha\n" in publish
        runtime = publish.split("Expose BuildKit cache runtime without logging it", 1)[1]
        assert "actions/github-script@" in runtime
        assert "core.setSecret(process.env.ACTIONS_RUNTIME_TOKEN" in runtime
        assert "core.info" not in runtime
        assert publish.index("setup-buildx-action") < publish.index("github-script")
        assert publish.index("github-script") < publish.index("build_ecr_image_if_missing.sh")
        if unit == "fetcher":
            assert text.count("build_ecr_image_if_missing.sh") == 4
            for repository in ("twelve-data", "finlab", "shioaji", "factset"):
                assert f"/findb/staging/fetcher/{repository}" in text
            for repository_var, dockerfile in (
                ("TWELVE", "Dockerfile"),
                ("FINLAB", "Dockerfile.finlab"),
                ("SHIOAJI", "Dockerfile.shioaji"),
                ("FACTSET", "Dockerfile.factset"),
            ):
                assert (
                    f'build_ecr_image_if_missing.sh "${repository_var}" . ./fetcher/{dockerfile}'
                ) in text
                assert (
                    "build_ecr_image_if_missing.sh "
                    f'"${repository_var}" ./fetcher ./fetcher/{dockerfile}'
                ) not in text

            ci = (ROOT / ".github" / "workflows" / "fetcher-ci.yml").read_text()
            for dockerfile in (
                "Dockerfile",
                "Dockerfile.finlab",
                "Dockerfile.shioaji",
                "Dockerfile.factset",
            ):
                assert f"docker build -f fetcher/{dockerfile}" in ci
            assert ci.count("docker build -f fetcher/") == 4


def test_fetcher_builds_before_waiting_for_same_sha_findb_deployment() -> None:
    text = (ROOT / ".github" / "workflows" / "fetcher-cd.yml").read_text()
    assert "Wait for same-revision FinDB staging rollout" in text
    assert "actions/workflows/findb-cd.yml/runs?head_sha=${HEAD_SHA}" in text
    assert "timed out waiting for same-revision FinDB rollout" in text
    select_block = re.search(
        r"^  select:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text, re.M | re.S
    ).group("body")
    assert "Wait for same-revision FinDB staging rollout" not in select_block
    workflow = yaml.safe_load(text)
    prepare = workflow["jobs"]["prepare"]
    names = [step.get("name") for step in prepare["steps"]]
    assert (
        names.index("Build and upload exact v2 staging candidate")
        < names.index("Wait for same-revision FinDB staging rollout")
        < names.index("Publish exact reusable deployment contract")
    )
    assert workflow["jobs"]["deploy"]["needs"] == "prepare"
    wait = next(
        step
        for step in prepare["steps"]
        if step.get("name") == "Wait for same-revision FinDB staging rollout"
    )
    assert wait["env"]["HEAD_SHA"] == "${{ needs.select.outputs.revision }}"
    assert "Configure staging publisher credentials" not in select_block


def test_reusable_preflight_is_before_deploy_and_checks_host_boundaries() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
        assert text.index("Run target-aware read-only SSM preflight") < text.index(
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
            r"- name: Run target-aware read-only SSM preflight(?P<body>.*?)(?=\n    - name:)",
            text,
            re.S,
        ).group("body")
        if unit == "fetcher":
            assert "check_fetcher_host.py" in preflight_block
            assert (
                "--target $q_target --registry $q_registry --installed-validator" in preflight_block
            )
            assert "q_host_guard_sha" in preflight_block
        else:
            assert "if ! docker ps --format '{{.Names}}' | grep -Eq" in preflight_block
            assert "then [ $q_target = production ];" not in preflight_block
            assert '[ -z \\"\\$(docker ps -aq)\\" ]; fi' in preflight_block
        assert "phase1-preflight-denial-probe" not in preflight_block
        cross_unit = "fetcher" if unit == "findb" else "findb"
        assert (
            f"--secret-id findb/$q_target/{cross_unit}/runtime/configuration >/dev/null 2>&1"
            in preflight_block
        )

    findb_text = (ROOT / ".github" / "workflows" / "findb-deploy.yml").read_text()
    assert "docker compose" in findb_text
    assert "config --no-interpolate -q" in findb_text
    assert "FINDB_NGINX_CONFIG_DIR=/etc/findb/nginx" in findb_text
    assert "/home/ubuntu/etc/nginx" not in findb_text
    preflight_block = re.search(
        r"- name: Run target-aware read-only SSM preflight(?P<body>.*?)(?=\n    - name:)",
        findb_text,
        re.S,
    ).group("body")
    assert "MIGRATION: ${{ steps.release.outputs.migration }}" in preflight_block
    assert "printf -v q_migration '%q' \"$MIGRATION\"" in preflight_block
    assert "--migration-revision $q_migration" in preflight_block

    fetcher_text = (ROOT / ".github" / "workflows" / "fetcher-deploy.yml").read_text()
    assert "docker compose" not in fetcher_text
    for helper in (
        "runtime_secret_command.sh",
        "deploy_fetcher_aws.sh",
        "release_fetcher_provider.sh",
    ):
        assert f'\\"\\$work/release/infra/deploy/runtime-secrets/{helper}\\"' in fetcher_text
    assert "bash -n" in fetcher_text


def test_reusable_deployments_only_forward_deployment_controls():
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github/workflows" / f"{unit}-deploy.yml").read_text()
        jobs = yaml.safe_load(text)["jobs"]
        assert all("environment" not in job for job in jobs.values())
        assert "${{ vars." not in text and "${{ secrets." not in text
        assert 'runtime_variable_names="FULL_MARKET_ENABLED PROVIDER_WORK_REQUIRED"' in text
        assert "APP_NAME: ${{" not in text
        assert "FETCHER_SOURCE_API_URL: ${{" not in text
        assert "aws-configuration" in text
        assert "load_runtime_secrets.py" in text


def test_every_ssm_run_shell_command_is_explicitly_wrapped_for_bash() -> None:
    wrapper = "python3 .deployment-driver/infra/deploy/ssm_bash_command.py"
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
        assert text.count("--document-name AWS-RunShellScript") == 3
        assert text.count(wrapper) == 3

    fetcher_cd = (ROOT / ".github" / "workflows" / "fetcher-cd.yml").read_text()
    assert fetcher_cd.count("--document-name AWS-RunShellScript") == 0
    assert fetcher_cd.count(wrapper) == 0


def test_bundle_validation_never_passes_an_incomplete_exact_input_contract() -> None:
    for workflow_name in (
        "findb-deploy.yml",
        "fetcher-deploy.yml",
        "production-promote.yml",
    ):
        text = (ROOT / ".github" / "workflows" / workflow_name).read_text()
        exact_calls = [
            line
            for line in text.splitlines()
            if ("validate-bundle" in line or "materialize-bundle" in line)
            and "--commit-sha" in line
        ]
        assert exact_calls
        for call in exact_calls:
            assert "--migration-revision" in call
            # The shared promotion builds its unit's exact image list first.
            assert "--image" in call or (
                '"${image_args[@]}"' in call and 'image_args+=(--image "$name=' in text
            )


def test_reusable_deployment_uses_current_validator_and_unified_acceptance():
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github/workflows" / f"{unit}-deploy.yml").read_text()
        assert 'validator=".deployment-driver/infra/deploy/release_manifest.py"' in text
        assert ".schema_version == 3" in text
        assert "schema_version:3" in text


def test_prepare_contracts_are_main_push_or_confirmed_manual_candidate_or_exact_replay() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        assert 'github.event_name }}" = workflow_dispatch' in text
        assert yaml.safe_load(text)["on"]["workflow_dispatch"]["inputs"]["deployment_mode"][
            "options"
        ] == ["replay", "candidate"]
        assert '[ "$CANDIDATE_CONFIRMATION" = deploy-new-candidate ]' in text
        assert '[ -z "$ACCEPTED" ] && [ -z "$IMAGE_TAG" ]' in text
        assert '[ -z "$CANDIDATE_CONFIRMATION" ]' in text
        assert '[ -n "$ACCEPTED" ] && [ -n "$IMAGE_TAG" ]' in text
        assert '[ "${{ github.event_name }}" = push ]' in text
        assert f"{unit}/candidates/${{REVISION}}/" in text
        assert "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${sha}.tar" in text
        assert "--if-none-match '*'" in text
        assert "--server-side-encryption aws:kms" in text
        assert '--ssekms-key-id "$KMS_KEY"' in text


def test_reusable_records_and_ssm_gates_are_immutable_and_bounded() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
        assert "timeout-minutes: 55" in text
        assert "--if-none-match '*'" in text
        assert "ssm_wait_command" in text
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


def test_native_provider_smoke_is_not_a_daily_deployment_gate():
    text = (ROOT / ".github/workflows/fetcher-cd.yml").read_text()
    assert "findb-fetch-finlab-smoke" not in text


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


@pytest.mark.parametrize(
    "filename",
    (
        "findb-cd.yml",
        "fetcher-cd.yml",
        "findb-deploy.yml",
        "fetcher-deploy.yml",
        "production-release.yml",
        "production-promote.yml",
    ),
)
def test_checkout_cannot_remove_configuration_action_before_its_post_step(filename):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    checked = 0
    for job in workflow["jobs"].values():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            if step.get("uses") != "./.deployment-driver/.github/actions/aws-configuration":
                continue
            driver_index = next(
                i
                for i, item in enumerate(steps[:index])
                if item.get("with", {}).get("path") == ".deployment-driver"
            )
            assert steps[driver_index]["with"]["ref"] == "${{ github.workflow_sha }}"
            root_checkouts = [
                i
                for i, item in enumerate(steps)
                if item.get("uses", "").startswith("actions/checkout@")
                and item.get("with", {}).get("path", ".") == "."
            ]
            # Initializing a root checkout can recreate its workspace even with
            # clean:false. The local action must survive through job post steps.
            assert all(i < driver_index for i in root_checkouts)
            checked += 1
    assert checked


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
def test_staging_replay_deploy_survives_skipped_publisher(unit: str) -> None:
    workflow = yaml.safe_load((ROOT / f".github/workflows/{unit}-cd.yml").read_text())
    jobs = workflow["jobs"]
    # Replay intentionally skips publish. A status function is required so that
    # GitHub does not inject success() and suppress deploy through that ancestor.
    assert jobs["deploy"]["needs"] == "prepare"
    assert jobs["deploy"]["if"] == "${{ !cancelled() && needs.prepare.result == 'success' }}"
    assert "needs.select.outputs.mode == 'replay'" in jobs["prepare"]["if"]
    assert "needs.publish.result == 'skipped'" in jobs["prepare"]["if"]
