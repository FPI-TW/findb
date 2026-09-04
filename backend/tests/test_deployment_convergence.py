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
        (["fetcher/src/main.py"], (False, True, False, True, False)),
        (["infra/deploy/release_manifest.py"], (True, True, True, True, False)),
        (["infra/tofu/staging/main.tf"], (False, False, False, False, True)),
        (["infra/env/production/findb/remote.env.example"], (False, False, False, False, True)),
        ([".github/workflows/findb-ci.yml"], (True, True, False, False, False)),
        ([".github/workflows/fetcher-production-cd.yml"], (True, True, False, False, False)),
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


def test_v1_staging_replay_is_accepted_but_v1_production_is_rejected() -> None:
    legacy = {
        "schema_version": 1,
        "deployment_target": "staging",
        "unit": "fetcher",
        "commit_sha": "a" * 40,
        "images": {
            name: f"{repo}@sha256:{'b' * 64}"
            for name, repo in manifest.IMAGE_REPOSITORIES["fetcher"].items()
        },
        "migration_revision": "none",
        "contract_versions": list(contracts.versions),
        "contract_manifest_sha256": contracts.canonical_sha256,
        "deployment_source_bundle_sha256": "d" * 64,
        "created_by_run_id": "1",
    }
    manifest.validate_manifest(legacy, contracts)
    legacy["deployment_target"] = "production"
    with pytest.raises(manifest.ManifestError, match="v1 manifests are staging-only"):
        manifest.validate_manifest(legacy, contracts)


def test_production_workflows_are_manual_and_do_not_contain_legacy_transport() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-production-cd.yml").read_text()
        assert "workflow_dispatch" in text and "push:" not in text
        assert "PRODUCTION_DEPLOY_ENABLED" in text
        assert f"PROMOTE_{unit.upper()}_PRODUCTION" in text
        assert "promote_image.sh" in text and "docker build" not in text
        assert "STAGING_PROMOTION_READ_ROLE_ARN" in text
        assert "AWS_PROMOTION_ROLE_ARN" in text
        assert '.registry "$RUNNER_TEMP/staging-manifest.json"' in text
        assert '.account_id "$RUNNER_TEMP/staging-manifest.json"' in text
        assert '[ "$SOURCE_ACCOUNT" != "$DESTINATION_ACCOUNT" ]' in text
        assert "secrets: inherit" not in text
        assert f"{unit}/production/candidates/" in text
        assert not {"ghcr.io", "appleboy", "FINDB_EC2_", "FETCHER_EC2_", "scp"} & set(text.split())


def test_production_release_tag_binding_precedes_ci_and_all_deploy_paths() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-production-cd.yml").read_text()
        assert 'git rev-parse "refs/tags/$RELEASE_TAG"' in text
        assert 'git rev-parse "refs/tags/$RELEASE_TAG^{commit}"' in text
        assert '[ "$(git cat-file -t "refs/tags/$RELEASE_TAG")" = tag ]' in text
        assert 'echo "tag_ref_oid=$tag_ref_oid" >> "$GITHUB_OUTPUT"' in text
        assert "outputs: {revision:" in text and "tag_ref_oid:" in text
        binding = re.search(
            r"^  bind-release-tag:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)",
            text,
            re.M | re.S,
        )
        assert binding
        binding_body = binding.group("body")
        assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in binding_body
        assert "ref: '${{ needs.resolve.outputs.revision }}'" in binding_body
        assert "Verify exact release revision for binding" in binding_body
        assert "Verify exact accepted staging binding source" in binding_body
        assert '.deployment_target == "staging"' in binding_body
        assert '.release_tag == ""' in binding_body
        assert (
            binding_body.index("Verify exact accepted staging binding source")
            < binding_body.index("Configure production release-binding credentials")
            < binding_body.index("Bind or validate immutable release tag")
        )
        assert "Configure production release-binding credentials" in binding_body
        assert "infra/deploy/bind_release_tag.sh" in binding_body
        assert "TAG_REF_OID: ${{ needs.resolve.outputs.tag_ref_oid }}" in binding_body
        assert "COMMIT_SHA: ${{ needs.resolve.outputs.revision }}" in binding_body
        assert "BUNDLE_KMS_KEY: ${{ vars.DEPLOY_BUNDLE_KMS_KEY_ARN }}" in binding_body
        assert "needs: [resolve, bind-release-tag]" in text
        assert "needs: [resolve, bind-release-tag, verify, promote]" in text
        assert "needs: [resolve, bind-release-tag, verify]" in text
        for action in re.findall(r"uses:\s*([^\s#}]+)", text):
            if not action.startswith("./"):
                assert re.search(r"@[0-9a-f]{40}$", action), action


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
        assert "ssm_command_marker_state" in text
        assert "PRODUCTION_DEPLOY_ENABLED" in text
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
        assert not {"ghcr.io", "appleboy", "FINDB_EC2_", "FETCHER_EC2_"} & set(text.split())


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
        assert jobs["select"]["environment"] == f"staging-{unit}"
        assert jobs["select"]["permissions"].get("id-token") is None
        assert "environment" not in jobs["publish"]
        assert jobs["publish"]["permissions"] == {
            "contents": "read",
            "id-token": "write",
        }
        assert jobs["prepare"]["environment"] == f"staging-{unit}"
        assert jobs["prepare"]["permissions"] == {
            "contents": "read",
            "id-token": "write",
        }
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
        assert "role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}" in prepare_block
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
        assert "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c" in text
        assert "build_ecr_image_if_missing.sh" in text
        assert "outputs.image_ref" in text
        assert "sed -n 's/^image_ref=//p'" not in text
        if unit == "fetcher":
            assert text.count("build_ecr_image_if_missing.sh") == 3
            for repository in ("twelve-data", "finlab", "shioaji"):
                assert f"/findb/staging/fetcher/{repository}" in text


def test_fetcher_waits_for_same_sha_findb_before_aws_credentials() -> None:
    text = (ROOT / ".github" / "workflows" / "fetcher-cd.yml").read_text()
    assert "Wait for same-revision FinDB staging rollout" in text
    assert "actions/workflows/findb-cd.yml/runs?head_sha=${HEAD_SHA}" in text
    assert "timed out waiting for same-revision FinDB rollout" in text
    select_block = re.search(
        r"^  select:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:|\Z)", text, re.M | re.S
    ).group("body")
    assert "Wait for same-revision FinDB staging rollout" in select_block
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
        assert "AccessDenied" in text
        assert "load_runtime_secrets.py" in text
        assert "docker compose" in text

    findb_text = (ROOT / ".github" / "workflows" / "findb-deploy.yml").read_text()
    assert "FINDB_NGINX_CONFIG_DIR=/etc/findb/nginx" in findb_text
    assert "/home/ubuntu/etc/nginx" not in findb_text


def test_v1_staging_replay_uses_the_bundle_local_validator_without_a_bare_sha_fallback() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
        assert "staging-v1-bundle-validator.py" in text
        assert 'tar -xOf "$RUNNER_TEMP/release-bundle.tar" infra/deploy/release_manifest.py' in text
        assert "jq -r .validator_sha256" in text
        assert f"--unit {unit}" in text
        assert "manifest-missing" not in text


def test_prepare_contracts_are_main_push_candidate_or_exact_manual_replay() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-cd.yml").read_text()
        assert 'github.event_name }}" = workflow_dispatch' in text
        assert '[ -n "$ACCEPTED" ] && [ -n "$IMAGE_TAG" ] || exit 1' in text
        assert '[ "${{ github.event_name }}" = push ]' in text
        assert f"{unit}/candidates/${{REVISION}}/" in text
        assert "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${sha}.tar" in text
        assert "--if-none-match '*'" in text
        assert "--server-side-encryption aws:kms" in text
        assert '--ssekms-key-id "$KMS_KEY"' in text


def test_reusable_records_and_ssm_gates_are_immutable_and_bounded() -> None:
    for unit in ("findb", "fetcher"):
        text = (ROOT / ".github" / "workflows" / f"{unit}-deploy.yml").read_text()
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
    text = (ROOT / ".github" / "workflows" / "fetcher-cd.yml").read_text()
    assert "options: ['2330,2317']" in text
    assert "needs.deploy.outputs.finlab_image_ref" in text
    assert '[[ "$FINLAB_IMAGE" =~ /findb/staging/fetcher/finlab@sha256:[0-9a-f]{64}$ ]]' in text
    assert "chown 10001:10001" in text
    assert "--user 10001:10001" in text
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
