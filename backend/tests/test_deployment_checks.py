"""Tests for deployment gates."""

import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

import scripts.predeploy_db_check as predeploy_db_check
from scripts.check_queue_health import (
    fetch_dlq_health,
    fetch_readiness_payload,
    validate_queue_health,
)
from scripts.predeploy_db_check import (
    DEFAULT_MINIMUM_CONNECTION_HEADROOM,
    calculate_connection_headroom,
    database_revision_matches_expected,
    database_url_matches_expected_host,
    database_url_requires_tls,
    is_schema_compatible_with_target,
    validate_predeploy_state,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
WORKFLOWS_ROOT = REPO_ROOT / ".github" / "workflows"
REQUIRED_CI_WORKFLOW = WORKFLOWS_ROOT / "required-ci.yml"
FINDB_CI_WORKFLOW = WORKFLOWS_ROOT / "findb-ci.yml"
FINDB_CD_WORKFLOW = WORKFLOWS_ROOT / "findb-cd.yml"
FETCHER_CI_WORKFLOW = WORKFLOWS_ROOT / "fetcher-ci.yml"
FETCHER_CD_WORKFLOW = WORKFLOWS_ROOT / "fetcher-cd.yml"
DEPLOY_WORKFLOW = FINDB_CD_WORKFLOW
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
ENV_CONFIG_ROOT = REPO_ROOT / "infra" / "env"
ENV_SYNC_SCRIPT = ENV_CONFIG_ROOT / "sync_github_environment.py"
PLAN_JSON_GUARD = REPO_ROOT / "infra" / "tofu" / "plan_json_guard.py"
SSM_COMMAND_MARKER_GATE = REPO_ROOT / "infra" / "deploy" / "ssm_command_marker_gate.sh"
SSM_BASH_COMMAND = REPO_ROOT / "infra" / "deploy" / "ssm_bash_command.py"


class UniqueKeyLoader(yaml.BaseLoader):
    """Parse workflow YAML without YAML 1.1 booleans and reject duplicate keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def test_production_calendar_seed_runs_after_migration_only_for_production() -> None:
    helper = (
        REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "deploy_findb_aws.sh"
    ).read_text()
    migration = helper.index("uv run alembic upgrade head")
    production_gate = helper.index('if [ "$DEPLOYMENT_TARGET" = production ]; then', migration)
    seed = helper.index("python /app/scripts/seed_production_calendars.py", production_gate)

    assert migration < production_gate < seed
    assert "--deployment-target production" in helper[seed : seed + 240]
    assert "--apply" in helper[seed : seed + 240]
    assert "COPY configs ./configs" in (REPO_ROOT / "backend" / "Dockerfile").read_text()


def _run_ssm_marker_gate(
    tmp_path: Path, response: dict[str, object] | None, *, aws_error: str = ""
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$AWS_CALLS"
case "${AWS_ERROR:-}" in
  InvocationDoesNotExist)
    echo 'An error occurred (InvocationDoesNotExist)' >&2
    exit 255
    ;;
  AccessDenied)
    echo 'An error occurred (AccessDeniedException)' >&2
    exit 254
    ;;
esac
printf '%s' "$SSM_RESPONSE"
""",
        encoding="utf-8",
    )
    fake_aws.chmod(0o755)
    calls = tmp_path / "calls"
    command = (
        f"source {SSM_COMMAND_MARKER_GATE!s}; "
        'ssm_command_marker_state region command instance "marker status=success" '
        '"marker status=failed"'
    )
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AWS_CALLS": str(calls),
        "AWS_ERROR": aws_error,
        "SSM_RESPONSE": json.dumps(response) if response is not None else "",
    }
    completed = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        check=False,
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        timeout=10,
    )
    invocation = calls.read_text(encoding="utf-8")
    assert invocation.count("ssm get-command-invocation") == 1
    for argument in (
        "--region region",
        "--command-id command",
        "--instance-id instance",
        "--output json",
        "command_id:CommandId",
        "instance_id:InstanceId",
        "document_name:DocumentName",
        "plugin_name:PluginName",
        "status:Status",
        "status_details:StatusDetails",
        "response_code:ResponseCode",
        "stdout:StandardOutputContent",
    ):
        assert argument in invocation
    return completed


def test_ssm_marker_gate_validates_single_invocation_response_and_exact_protocol(
    tmp_path: Path,
) -> None:
    base: dict[str, object] = {
        "command_id": "command",
        "instance_id": "instance",
        "document_name": "AWS-RunShellScript",
        "plugin_name": "aws:runShellScript",
        "status": "Success",
        "status_details": "Success",
        "response_code": 0,
        "stdout": "marker status=success\r\n",
    }
    valid = _run_ssm_marker_gate(tmp_path, base)
    assert valid.returncode == 0, valid.stderr
    assert valid.stdout == "success\n"

    for update in (
        {"stdout": "prefix marker status=success\n"},
        {"stdout": "marker status=success\nmarker status=success\n"},
        {"stdout": "marker status=success\nmarker status=failed\n"},
        {"response_code": 1},
        {"status": "Failed", "status_details": "Failed"},
        {"status": "Cancelled", "status_details": "Cancelled"},
        {"status": "TimedOut", "status_details": "Timed Out"},
        {"document_name": "WrongDocument"},
        {"response_code": 0.5},
    ):
        case_dir = tmp_path / str(len(list(tmp_path.iterdir())))
        case_dir.mkdir()
        rejected = _run_ssm_marker_gate(case_dir, base | update)
        assert rejected.returncode != 0
        assert rejected.stdout == ""

    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    pending = _run_ssm_marker_gate(
        pending_dir, base | {"status": "InProgress", "status_details": "In Progress"}
    )
    assert pending.returncode == 0, pending.stderr
    assert pending.stdout == "pending\n"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("command_id", "other-command"),
        ("instance_id", "other-instance"),
        ("plugin_name", "other-plugin"),
        ("status_details", "Failed"),
        ("status", "Unknown"),
        ("stdout", 123),
        ("status", 123),
    ),
)
def test_ssm_marker_gate_rejects_identity_status_and_type_mismatches(
    tmp_path: Path, field: str, value: object
) -> None:
    response: dict[str, object] = {
        "command_id": "command",
        "instance_id": "instance",
        "document_name": "AWS-RunShellScript",
        "plugin_name": "aws:runShellScript",
        "status": "Success",
        "status_details": "Success",
        "response_code": 0,
        "stdout": "marker status=success\n",
    }
    rejected = _run_ssm_marker_gate(tmp_path, response | {field: value})

    assert rejected.returncode != 0
    assert rejected.stdout == ""
    if field in {"status_details", "status"} and value != 123:
        assert "SSM terminal invocation failure" in rejected.stderr
        assert "status=" in rejected.stderr
        assert "response_code=" in rejected.stderr
    else:
        assert "SSM invocation marker protocol failure" in rejected.stderr


@pytest.mark.parametrize("missing_key", ("command_id", "instance_id", "plugin_name"))
def test_ssm_marker_gate_rejects_missing_required_keys(tmp_path: Path, missing_key: str) -> None:
    response: dict[str, object] = {
        "command_id": "command",
        "instance_id": "instance",
        "document_name": "AWS-RunShellScript",
        "plugin_name": "aws:runShellScript",
        "status": "Success",
        "status_details": "Success",
        "response_code": 0,
        "stdout": "marker status=success\n",
    }
    response.pop(missing_key)
    rejected = _run_ssm_marker_gate(tmp_path, response)

    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert "SSM invocation marker protocol failure" in rejected.stderr


def test_ssm_marker_gate_terminal_diagnostics_do_not_leak_stdout_or_markers(
    tmp_path: Path,
) -> None:
    response = {
        "command_id": "command",
        "instance_id": "instance",
        "document_name": "AWS-RunShellScript",
        "plugin_name": "aws:runShellScript",
        "status": "Failed",
        "status_details": "Failed",
        "response_code": 42,
        "stdout": "secret marker status=success\n",
    }
    rejected = _run_ssm_marker_gate(tmp_path, response)

    assert rejected.returncode != 0
    assert rejected.stdout == ""
    assert "SSM terminal invocation failure" in rejected.stderr
    assert "status=Failed" in rejected.stderr
    assert "status_details=Failed" in rejected.stderr
    assert "response_code=42" in rejected.stderr
    assert "secret" not in rejected.stderr
    assert "marker status=success" not in rejected.stderr


def test_ssm_marker_gate_treats_only_missing_invocation_as_pending(tmp_path: Path) -> None:
    missing = _run_ssm_marker_gate(tmp_path / "missing", None, aws_error="InvocationDoesNotExist")
    assert missing.returncode == 0
    assert missing.stdout == "pending\n"
    assert "InvocationDoesNotExist" in missing.stderr

    denied = _run_ssm_marker_gate(tmp_path / "denied", None, aws_error="AccessDenied")
    assert denied.returncode != 0
    assert denied.stdout == ""
    assert "AccessDeniedException" in denied.stderr

    malformed = _run_ssm_marker_gate(tmp_path / "malformed", None)
    assert malformed.returncode != 0
    assert malformed.stdout == ""


def test_ssm_bash_command_runs_pipefail_scripts_through_bash_from_posix_sh() -> None:
    script = (
        "set -euo pipefail\n[[ -n \"${BASH_VERSION:-}\" ]]\nprintf 'ssm_bash_wrapper=success\\n'\n"
    )
    encoded = subprocess.run(
        [sys.executable, str(SSM_BASH_COMMAND)],
        input=script,
        check=True,
        capture_output=True,
        text=True,
    )
    assert encoded.stdout.startswith("exec bash -c ")
    completed = subprocess.run(
        ["/bin/sh", "-c", encoded.stdout],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == "ssm_bash_wrapper=success\n"

    failing = subprocess.run(
        [sys.executable, str(SSM_BASH_COMMAND)],
        input="set -euo pipefail\nfalse | true\nprintf 'unreachable\\n'\n",
        check=True,
        capture_output=True,
        text=True,
    )
    rejected = subprocess.run(
        ["/bin/sh", "-c", failing.stdout],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "unreachable" not in rejected.stdout


def _load_workflow(path: Path) -> dict[str, object]:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    assert isinstance(loaded, dict)
    return loaded


def _named_step(workflow: dict[str, Any], job_name: str, step_name: str) -> dict[str, Any]:
    steps = workflow["jobs"][job_name]["steps"]
    return next(step for step in steps if step.get("name") == step_name)


def _legacy_rollback_runtime_paths(workflow_path: Path) -> set[str]:
    workflow = _load_workflow(workflow_path)
    script = _named_step(
        workflow,
        "build-push-staging-ecr",
        "Validate staging ECR release selection",
    )["run"]
    marker = 'git diff --quiet "$ECR_IMAGE_TAG" HEAD -- \\\n'
    assert marker in script
    diff_paths = script.split(marker, 1)[1].split("; then", 1)[0]
    return {
        line.strip().removesuffix("\\").strip() for line in diff_paths.splitlines() if line.strip()
    }


def _host_runtime_contract_paths(workflow_path: Path) -> set[str]:
    # The explicit pre-foundation rollback gate remains bound to the old host
    # runtime contract.  Post-foundation staging instead uses the validated
    # bundle, so this set must not be inferred from removed SCP steps.
    if workflow_path == FINDB_CD_WORKFLOW:
        return {
            "docker-compose.prod.yml",
            "infra/nginx/nginx.conf",
            "infra/nginx/source-allowlist.conf",
            "infra/nginx/cloudflare-real-ip.conf",
            "backend/scripts/render_nginx_public_host.py",
            "backend/scripts/render_nginx_source_allowlist.py",
            "backend/scripts/render_nginx_cloudflare_real_ip.py",
            "infra/deploy/runtime-secrets/load_runtime_secrets.py",
            "infra/deploy/runtime-secrets/runtime_secret_command.sh",
            "infra/deploy/runtime-secrets/render_nginx_runtime.sh",
            "infra/deploy/runtime-secrets/render_serve_key.py",
            "infra/deploy/runtime-secrets/install_findb_bootstrap.sh",
            "infra/deploy/runtime-secrets/deploy_findb_aws.sh",
            "infra/deploy/runtime-secrets/findb.json",
        }
    return {
        "infra/deploy/runtime-secrets/load_runtime_secrets.py",
        "infra/deploy/runtime-secrets/runtime_secret_command.sh",
        "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh",
        "infra/deploy/runtime-secrets/release_fetcher_provider.sh",
        "infra/deploy/runtime-secrets/fetcher.json",
    }


def _aggregate_ci_paths(unit: str) -> set[str]:
    workflow = _load_workflow(REQUIRED_CI_WORKFLOW)
    script = _named_step(workflow, "changes", "Classify changed paths")["run"]
    match = re.search(
        rf'case "\$path" in\s+(?P<patterns>[^)]+)\)\s+{unit}=true',
        script,
    )
    assert match is not None
    return {pattern.strip() for pattern in match.group("patterns").split("|")}


def _secret_reference_paths(value: object, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    references: list[tuple[str, ...]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            references.extend(_secret_reference_paths(child, (*path, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            references.extend(_secret_reference_paths(child, (*path, str(index))))
    elif isinstance(value, str) and "${{ secrets." in value:
        references.append(path)
    return references


def _uses_references(value: object) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses" and isinstance(child, str):
                references.append(child)
            else:
                references.extend(_uses_references(child))
    elif isinstance(value, list):
        for child in value:
            references.extend(_uses_references(child))
    return references


def test_service_workflows_are_split_and_have_unique_yaml_keys() -> None:
    assert not (WORKFLOWS_ROOT / "deploy.yml").exists()

    expected_names = {
        "required-ci.yml",
        "findb-ci.yml",
        "findb-cd.yml",
        "findb-deploy.yml",
        "findb-production-cd.yml",
        "fetcher-ci.yml",
        "fetcher-cd.yml",
        "fetcher-deploy.yml",
        "fetcher-production-cd.yml",
        "staging-infra-plan.yml",
    }
    actual_paths = tuple(
        sorted(
            (
                *WORKFLOWS_ROOT.glob("*.yml"),
                *WORKFLOWS_ROOT.glob("*.yaml"),
            )
        )
    )

    assert {path.name for path in actual_paths} == expected_names
    for path in actual_paths:
        _load_workflow(path)


def test_staging_infra_plan_is_reusable_exact_commit_and_bounded() -> None:
    workflow_path = WORKFLOWS_ROOT / "staging-infra-plan.yml"
    workflow = _load_workflow(workflow_path)
    triggers = workflow["on"]

    assert set(triggers) == {"workflow_call"}
    assert triggers["workflow_call"]["inputs"] == {
        "allow_ghcr_metadata_retirement": {
            "description": (
                "Allow only the separately approved legacy GHCR metadata retirement pair."
            ),
            "required": "false",
            "type": "boolean",
            "default": "false",
        }
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"plan"}
    assert workflow["env"]["TOFU_VERSION"] == "1.12.6"

    plan_job = workflow["jobs"]["plan"]
    assert plan_job["permissions"] == {"contents": "read", "id-token": "write"}
    assert "environment" not in plan_job
    steps = plan_job["steps"]
    fork_guard = steps[0]
    assert fork_guard["name"] == "Reject external fork pull requests"
    assert fork_guard["env"] == {
        "PR_HEAD_REPOSITORY": "${{ github.event.pull_request.head.repo.full_name }}",
        "BASE_REPOSITORY": "${{ github.repository }}",
    }
    fork_guard_script = fork_guard["run"]
    assert '-z "$PR_HEAD_REPOSITORY"' in fork_guard_script
    assert '"$PR_HEAD_REPOSITORY" != "$BASE_REPOSITORY"' in fork_guard_script
    assert "External fork pull requests are not eligible" in fork_guard_script
    assert "exit 1" in fork_guard_script
    checkout = next(
        step for step in steps if step.get("name") == "Checkout exact pull request commit"
    )
    assert checkout["uses"] == ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
    assert checkout["with"] == {
        "ref": "${{ github.event.pull_request.head.sha }}",
        "fetch-depth": "1",
        "persist-credentials": "false",
    }

    retirement_authorization = next(
        step for step in steps if step.get("name") == "Authorize bounded GHCR metadata retirement"
    )
    assert retirement_authorization["id"] == "retirement_authorization"
    assert retirement_authorization["env"] == {
        "RETIREMENT_REQUESTED": "${{ inputs.allow_ghcr_metadata_retirement }}",
        "CALLER_REPOSITORY": "${{ github.repository }}",
        "PR_HEAD_REPOSITORY": "${{ github.event.pull_request.head.repo.full_name }}",
        "PR_HEAD_REF": "${{ github.event.pull_request.head.ref }}",
        "EVENT_NAME": "${{ github.event_name }}",
        "PR_BASE_REF": "${{ github.event.pull_request.base.ref }}",
        "PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "TRUSTED_WORKFLOW_REPOSITORY": "${{ job.workflow_repository }}",
        "TRUSTED_WORKFLOW_SHA": "${{ job.workflow_sha }}",
        "TRUSTED_WORKFLOW_REF": "${{ job.workflow_ref }}",
        "TRUSTED_WORKFLOW_FILE_PATH": "${{ job.workflow_file_path }}",
    }
    retirement_script = retirement_authorization["run"]
    assert '"$CALLER_REPOSITORY" != "FPI-TW/findb"' in retirement_script
    assert '"$PR_HEAD_REPOSITORY" != "FPI-TW/findb"' in retirement_script
    assert '"$PR_HEAD_REF" != "chore/staging-phase2-retirement"' in retirement_script
    assert '"$EVENT_NAME" != "pull_request"' in retirement_script
    assert '"$PR_BASE_REF" != "main"' in retirement_script
    assert re.search(r'! "\$PR_BASE_SHA" =~ \^\[0-9a-fA-F\]\{40\}\$', retirement_script)
    assert '"$TRUSTED_WORKFLOW_REPOSITORY" != "FPI-TW/findb"' in retirement_script
    assert re.search(r'! "\$TRUSTED_WORKFLOW_SHA" =~ \^\[0-9a-fA-F\]\{40\}\$', retirement_script)
    assert (
        'expected_workflow_ref="FPI-TW/findb/.github/workflows/staging-infra-plan.yml@$TRUSTED_WORKFLOW_SHA"'
        in retirement_script
    )
    assert "A PR-local reusable workflow is reported as @refs/pull/<n>/merge." in retirement_script
    assert "to an immutable SHA, which is the only accepted workflow_ref." in retirement_script
    assert '"$TRUSTED_WORKFLOW_REF" != "$expected_workflow_ref"' in retirement_script
    assert '"$TRUSTED_WORKFLOW_FILE_PATH" != ".github/workflows/staging-infra-plan.yml"' in (
        retirement_script
    )
    assert 'echo "retirement_mode=true" >> "$GITHUB_OUTPUT"' in retirement_script
    assert 'echo "retirement_mode=false" >> "$GITHUB_OUTPUT"' in retirement_script

    trusted_checkout = next(
        step for step in steps if step.get("name") == "Checkout trusted guard source"
    )
    assert trusted_checkout["uses"] == ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1")
    assert trusted_checkout["with"] == {
        "repository": "${{ job.workflow_repository }}",
        "ref": "${{ job.workflow_sha }}",
        "path": ".trusted-staging-infra-plan",
        "sparse-checkout": "infra/tofu/plan_json_guard.py\ninfra/tofu/staging/.terraform.lock.hcl\n",
        "sparse-checkout-cone-mode": "false",
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    trusted_source_verification = next(
        step for step in steps if step.get("name") == "Verify trusted guard source"
    )
    assert trusted_source_verification["env"] == {
        "RETIREMENT_MODE": "${{ steps.retirement_authorization.outputs.retirement_mode }}",
        "PR_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
        "TRUSTED_WORKFLOW_SHA": "${{ job.workflow_sha }}",
    }
    trusted_source_script = trusted_source_verification["run"]
    assert 'trusted_root="$GITHUB_WORKSPACE/.trusted-staging-infra-plan"' in trusted_source_script
    assert 'git -C "$trusted_root" rev-parse HEAD' in trusted_source_script
    assert '"$trusted_sha" != "$TRUSTED_WORKFLOW_SHA"' in trusted_source_script
    assert re.search(
        r'\[\[ ! "\$PR_BASE_SHA" =~ \^\[0-9a-fA-F\]\{40\}\$ \]\]', trusted_source_script
    )
    assert 'git -C "$trusted_root" cat-file -e "$PR_BASE_SHA^{commit}"' in trusted_source_script
    assert (
        'git -C "$trusted_root" merge-base --is-ancestor "$TRUSTED_WORKFLOW_SHA" "$PR_BASE_SHA"'
        in trusted_source_script
    )
    assert 'lockfile_path="infra/tofu/staging/.terraform.lock.hcl"' in trusted_source_script
    assert 'git -C "$trusted_root" cat-file -e "$TRUSTED_WORKFLOW_SHA:$lockfile_path"' in (
        trusted_source_script
    )
    assert (
        'git -C "$trusted_root" show "$TRUSTED_WORKFLOW_SHA:$lockfile_path" | sha256sum'
        in trusted_source_script
    )
    assert 'sha256sum "$GITHUB_WORKSPACE/$lockfile_path"' in trusted_source_script
    assert '"$trusted_lockfile_sha256" != "$pr_lockfile_sha256"' in trusted_source_script

    trusted_source_capture = next(
        step for step in steps if step.get("name") == "Capture verified plan guard source"
    )
    assert trusted_source_capture["id"] == "trusted_guard_source"
    assert trusted_source_capture["env"] == {
        "TRUSTED_WORKFLOW_SHA": "${{ job.workflow_sha }}",
    }
    trusted_capture_script = trusted_source_capture["run"]
    assert 'git -C "$trusted_root" show "$TRUSTED_WORKFLOW_SHA:infra/tofu/plan_json_guard.py"' in (
        trusted_capture_script
    )
    assert "base64 --wrap=0" in trusted_capture_script
    assert "base64 --decode" in trusted_capture_script
    assert "sha256sum" in trusted_capture_script
    assert 'echo "source_base64=$guard_source_base64" >> "$GITHUB_OUTPUT"' in (
        trusted_capture_script
    )
    assert 'echo "source_sha256=$guard_source_sha256" >> "$GITHUB_OUTPUT"' in (
        trusted_capture_script
    )

    tofu_setup = next(step for step in steps if step.get("name") == "Set up pinned OpenTofu")
    assert tofu_setup["uses"] == (
        "opentofu/setup-opentofu@a1320f892987e89d278cc92dc5adc984fb93aca4"
    )
    assert tofu_setup["with"] == {
        "tofu_version": "${{ env.TOFU_VERSION }}",
        "tofu_wrapper": "false",
    }

    verify_commit = next(step for step in steps if step.get("name") == "Verify checked out commit")
    assert verify_commit["env"] == {
        "PR_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
    }
    verify_script = verify_commit["run"]
    assert re.search(r"\[\[ ! \"\$PR_HEAD_SHA\" =~ \^\[0-9a-fA-F\]\{40\}\$ \]\]", verify_script)
    assert 'checked_out_sha="$(git rev-parse HEAD)"' in verify_script
    assert '"$checked_out_sha" != "$PR_HEAD_SHA"' in verify_script
    assert "exit 1" in verify_script

    credential_step = next(
        step for step in steps if step.get("name") == "Configure staging plan AWS credentials"
    )
    assert (
        steps.index(fork_guard)
        < steps.index(checkout)
        < steps.index(verify_commit)
        < steps.index(retirement_authorization)
        < steps.index(trusted_checkout)
        < steps.index(trusted_source_verification)
        < steps.index(trusted_source_capture)
        < steps.index(tofu_setup)
        < steps.index(credential_step)
    )
    assert credential_step["uses"] == (
        "aws-actions/configure-aws-credentials@e6de054238d6b7531b4efff3b6587d9aade6a06c"
    )
    assert credential_step["with"] == {
        "role-to-assume": "${{ vars.STAGING_INFRA_PLAN_ROLE_ARN }}",
        "aws-region": "${{ env.AWS_REGION }}",
        "allowed-account-ids": "${{ env.AWS_ACCOUNT_ID }}",
        "role-session-name": "staging-infra-plan-${{ github.run_id }}",
    }

    formatting_step = next(
        step for step in steps if step.get("name") == "Check OpenTofu formatting recursively"
    )
    init_step = next(
        step for step in steps if step.get("name") == "Initialize exact staging backend"
    )
    init_script = init_step["run"]
    for setting in (
        "-reconfigure",
        "-lockfile=readonly",
        "bucket=findb-staging-tofu-state-439622209937",
        "key=staging/control-plane.tfstate",
        "region=ap-southeast-1",
        "encrypt=true",
        "kms_key_id=arn:aws:kms:ap-southeast-1:439622209937:key/776159fc-3251-4cd0-98b0-24dfa9e9701d",
        "use_lockfile=true",
    ):
        assert setting in init_script

    validate_step = next(
        step for step in steps if step.get("name") == "Validate staging configuration"
    )

    plan_step = next(
        step
        for step in steps
        if step.get("name") == "Refresh plan and enforce bounded delete policy"
    )
    plan_script = plan_step["run"]
    assert "-refresh=true" in plan_script
    assert "-var-file=terraform.tfvars.example" in plan_script
    assert '-out="$plan_file"' in plan_script
    assert 'tofu -chdir=infra/tofu/staging show -json "$plan_file"' in plan_script
    assert 'python3 -c "$trusted_guard_source"' in plan_script
    assert ".trusted-staging-infra-plan/infra/tofu/plan_json_guard.py" not in plan_script
    assert "TRUSTED_GUARD_SOURCE_BASE64" in plan_script
    assert "TRUSTED_GUARD_SOURCE_SHA256" in plan_script
    assert "base64 --decode" in plan_script
    assert "sha256sum" in plan_script
    assert "unset trusted_guard_source" in plan_script
    assert "RETIREMENT_MODE: ${{ steps.retirement_authorization.outputs.retirement_mode }}" in (
        workflow_path.read_text(encoding="utf-8")
    )
    assert "--allow-delete-address" in plan_script
    assert 'aws_secretsmanager_secret.runtime["findb/registry/ghcr-pull"]' in plan_script
    assert 'aws_secretsmanager_secret.runtime["fetcher/registry/ghcr-pull"]' in plan_script
    assert '"$delete_count" != "0" && "$delete_count" != "2"' in plan_script
    assert "jq" not in plan_script
    assert "fallback" not in plan_script.lower()
    assert "authorized metadata retirements" in plan_script
    assert "delete/replace actions" in plan_script
    assert "trap 'rm -rf \"$scratch\"' EXIT" in plan_script
    assert "GITHUB_STEP_SUMMARY" in plan_script
    assert "config_checksum" in plan_script
    assert "lock_checksum" in plan_script
    workflow_text = workflow_path.read_text(encoding="utf-8")
    assert not re.search(r'echo\s+"[^"\n]*`', plan_script)
    assert "printf -- '- commit: `%s`\\n' \"$PR_HEAD_SHA\"" in plan_script
    assert (
        'printf -- \'- configuration files: %s (sha256: `%s`)\\n\' "$config_count" "$config_checksum"'
        in plan_script
    )
    assert "upload-artifact" not in workflow_text
    assert "tofu apply" not in workflow_text
    assert "${{ secrets." not in workflow_text
    assert (
        steps.index(trusted_checkout)
        < steps.index(trusted_source_verification)
        < steps.index(trusted_source_capture)
        < steps.index(tofu_setup)
        < steps.index(credential_step)
        < steps.index(formatting_step)
        < steps.index(init_step)
        < steps.index(validate_step)
        < steps.index(plan_step)
    )


def _plan_json_fixture() -> dict[str, object]:
    return {
        "format_version": "1.0",
        "terraform_version": "1.12.6",
        "planned_values": {"root_module": {"resources": [], "child_modules": []}},
        "configuration": {
            "root_module": {
                "resources": [
                    {
                        "expressions": {
                            "statement": [
                                {
                                    "actions": {"constant_value": ["iam:GetRole"]},
                                    "resources": {"references": ["aws_iam_role.example.arn"]},
                                }
                            ]
                        }
                    }
                ],
                "module_calls": {},
            }
        },
        "resource_changes": [],
        "resource_drift": [],
        "output_changes": {},
        "deferred_changes": [],
    }


def _run_plan_json_guard(
    tmp_path: Path,
    payload: object,
    allowed_delete_addresses: tuple[str, ...] = (),
    guard_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(PLAN_JSON_GUARD),
            *(
                argument
                for address in allowed_delete_addresses
                for argument in ("--allow-delete-address", address)
            ),
            *guard_args,
            str(plan_path),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


def test_plan_json_guard_accepts_safe_plan_and_emits_only_bounded_counts(
    tmp_path: Path,
) -> None:
    completed = _run_plan_json_guard(tmp_path, _plan_json_fixture())

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.startswith("plan_guard=pass ")
    assert "action_lists=0" in completed.stdout
    assert "delete_actions=0" in completed.stdout
    assert "resource_changes=0" in completed.stdout
    assert "resource_drift=0" in completed.stdout
    assert "output_changes=0" in completed.stdout
    assert "deferred_changes=0" in completed.stdout


def test_plan_json_guard_accepts_missing_zero_drift_collection(tmp_path: Path) -> None:
    plan = _plan_json_fixture()
    del plan["resource_drift"]

    completed = _run_plan_json_guard(tmp_path, plan)

    assert completed.returncode == 0, completed.stderr
    assert "resource_drift=0" in completed.stdout


RETIREMENT_DELETE_ADDRESSES = (
    'aws_secretsmanager_secret.runtime["findb/registry/ghcr-pull"]',
    'aws_secretsmanager_secret.runtime["fetcher/registry/ghcr-pull"]',
)


def test_plan_json_guard_allows_only_the_exact_pure_retirement_delete_pair(
    tmp_path: Path,
) -> None:
    plan = _plan_json_fixture()
    plan["resource_changes"] = [
        {"address": address, "change": {"actions": ["delete"]}}
        for address in RETIREMENT_DELETE_ADDRESSES
    ]

    completed = _run_plan_json_guard(tmp_path, plan, RETIREMENT_DELETE_ADDRESSES)

    assert completed.returncode == 0, completed.stderr
    assert "delete_actions=2" in completed.stdout
    assert "ghcr-pull" not in completed.stdout
    assert "authorized_delete_actions" not in completed.stdout


def test_plan_json_guard_allows_a_zero_delete_retirement_rerun_with_default_summary(
    tmp_path: Path,
) -> None:
    default_completed = _run_plan_json_guard(tmp_path, _plan_json_fixture())
    retirement_completed = _run_plan_json_guard(
        tmp_path,
        _plan_json_fixture(),
        RETIREMENT_DELETE_ADDRESSES,
    )

    assert retirement_completed.returncode == 0, retirement_completed.stderr
    assert retirement_completed.stdout == default_completed.stdout


def test_plan_json_guard_allows_moved_noop_with_previous_address_in_retirement_mode(
    tmp_path: Path,
) -> None:
    plan = _plan_json_fixture()
    plan["resource_changes"] = [
        {
            "address": 'aws_secretsmanager_secret.active_runtime["findb/database/application"]',
            "previous_address": 'aws_secretsmanager_secret.runtime["findb/database/application"]',
            "change": {"actions": ["no-op"]},
        }
    ]

    completed = _run_plan_json_guard(tmp_path, plan, RETIREMENT_DELETE_ADDRESSES)

    assert completed.returncode == 0, completed.stderr
    assert "delete_actions=0" in completed.stdout


@pytest.mark.parametrize(
    ("label", "mutate", "allow_deletes"),
    (
        (
            "default exact resource-change delete",
            lambda plan: plan["resource_changes"].append(
                {
                    "address": RETIREMENT_DELETE_ADDRESSES[0],
                    "change": {"actions": ["delete"]},
                }
            ),
            (),
        ),
        (
            "replacement",
            lambda plan: plan["resource_changes"].append(
                {
                    "address": RETIREMENT_DELETE_ADDRESSES[0],
                    "change": {"actions": ["delete", "create"]},
                }
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "incomplete allowed delete pair",
            lambda plan: plan["resource_changes"].append(
                {
                    "address": RETIREMENT_DELETE_ADDRESSES[0],
                    "change": {"actions": ["delete"]},
                }
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "unauthorized delete",
            lambda plan: plan["resource_changes"].append(
                {
                    "address": 'aws_secretsmanager_secret.runtime["other"]',
                    "change": {"actions": ["delete"]},
                }
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "duplicate delete address",
            lambda plan: plan["resource_changes"].extend(
                [
                    {
                        "address": RETIREMENT_DELETE_ADDRESSES[0],
                        "change": {"actions": ["delete"]},
                    },
                    {
                        "address": RETIREMENT_DELETE_ADDRESSES[0],
                        "change": {"actions": ["delete"]},
                    },
                ]
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "previous address",
            lambda plan: plan["resource_changes"].append(
                {
                    "address": RETIREMENT_DELETE_ADDRESSES[0],
                    "previous_address": "legacy",
                    "change": {"actions": ["delete"]},
                }
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "deposed instance",
            lambda plan: plan["resource_changes"].append(
                {
                    "address": RETIREMENT_DELETE_ADDRESSES[0],
                    "deposed": "deposed-instance",
                    "change": {"actions": ["delete"]},
                }
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "null deposed instance",
            lambda plan: plan["resource_changes"].append(
                {
                    "address": RETIREMENT_DELETE_ADDRESSES[0],
                    "deposed": None,
                    "change": {"actions": ["delete"]},
                }
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "resource-drift delete",
            lambda plan: plan["resource_drift"].append({"change": {"actions": ["delete"]}}),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "output delete",
            lambda plan: plan["output_changes"].update({"example": {"actions": ["delete"]}}),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "nested deferred delete",
            lambda plan: plan["deferred_changes"].append(
                {
                    "change": {"actions": ["no-op"]},
                    "future": {"nested": {"actions": ["delete"]}},
                }
            ),
            RETIREMENT_DELETE_ADDRESSES,
        ),
        (
            "unknown future delete",
            lambda plan: plan.update({"future": {"nested": {"actions": ["delete"]}}}),
            RETIREMENT_DELETE_ADDRESSES,
        ),
    ),
)
def test_plan_json_guard_rejects_delete_outside_the_bounded_exception(
    tmp_path: Path,
    label: str,
    mutate: Any,
    allow_deletes: tuple[str, ...],
) -> None:
    del label
    plan = _plan_json_fixture()
    mutate(plan)

    completed = _run_plan_json_guard(tmp_path, plan, allow_deletes)

    assert completed.returncode != 0
    assert "plan_guard=reject reason=delete" in completed.stderr
    assert completed.stdout == ""
    assert "example" not in completed.stderr


@pytest.mark.parametrize(
    "guard_args",
    (
        ("--unknown",),
        ("--allow-delete-address", RETIREMENT_DELETE_ADDRESSES[0]),
        (
            "--allow-delete-address",
            RETIREMENT_DELETE_ADDRESSES[0],
            "--allow-delete-address",
            RETIREMENT_DELETE_ADDRESSES[0],
        ),
        ("--allow-delete-address", 'aws_secretsmanager_secret.runtime["other"]'),
    ),
)
def test_plan_json_guard_rejects_invalid_retirement_allowlist_arguments(
    tmp_path: Path,
    guard_args: tuple[str, ...],
) -> None:
    completed = _run_plan_json_guard(tmp_path, _plan_json_fixture(), guard_args=guard_args)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.strip() == "plan_guard=reject reason=usage"


@pytest.mark.parametrize(
    ("label", "payload"),
    (
        ("non-object", []),
        ("missing core configuration", {"format_version": "1.0"}),
        (
            "missing resource change collection",
            {
                key: value
                for key, value in _plan_json_fixture().items()
                if key != "resource_changes"
            },
        ),
        (
            "resource changes wrong collection type",
            {**_plan_json_fixture(), "resource_changes": {}},
        ),
        (
            "action list has non-string member",
            {
                **_plan_json_fixture(),
                "resource_changes": [{"change": {"actions": ["create", 7]}}],
            },
        ),
        (
            "resource change missing action list",
            {**_plan_json_fixture(), "resource_changes": [{"change": {}}]},
        ),
        (
            "output change missing action list",
            {**_plan_json_fixture(), "output_changes": {"example": {}}},
        ),
    ),
)
def test_plan_json_guard_rejects_malformed_or_incomplete_plan_json(
    tmp_path: Path,
    label: str,
    payload: object,
) -> None:
    del label
    completed = _run_plan_json_guard(tmp_path, payload)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr.strip() == "plan_guard=reject reason=malformed"


@pytest.mark.parametrize(
    "resource_drift",
    (None, {}, "not-a-list", 7, [{}], [{"change": {}}]),
)
def test_plan_json_guard_rejects_present_invalid_resource_drift_collection(
    tmp_path: Path,
    resource_drift: object,
) -> None:
    completed = _run_plan_json_guard(
        tmp_path,
        {**_plan_json_fixture(), "resource_drift": resource_drift},
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr.strip() == "plan_guard=reject reason=malformed"


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    for path in (*WORKFLOWS_ROOT.glob("*.yml"), *WORKFLOWS_ROOT.glob("*.yaml")):
        for reference in _uses_references(_load_workflow(path)):
            if reference.startswith("./"):
                continue
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference), (
                f"{path.name} contains an unpinned action reference: {reference}"
            )


def test_aws_host_helpers_reject_untrusted_ecr_images_before_docker_or_credentials(
    tmp_path: Path,
) -> None:
    findb_helper = REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "deploy_findb_aws.sh"
    fetcher_helper = (
        REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "release_fetcher_provider.sh"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    (fake_bin / "docker").write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {docker_log}\nexit 99\n", encoding="utf-8"
    )
    (fake_bin / "docker").chmod(0o755)
    environment = dict(
        os.environ,
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        AWS_REGION="ap-southeast-1",
        AWS_ACCOUNT_ID="439622209937",
        DEPLOYMENT_TARGET="staging",
        ECR_REGISTRY="439622209937.dkr.ecr.ap-southeast-1.amazonaws.com",
    )

    invalid_findb = subprocess.run(
        ["bash", str(findb_helper)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **environment,
            "AWS_REGION": "ap-southeast-1",
            "ECR_REGISTRY": "evil.example",
            "FINDB_IMAGE_REF": "evil.example/backend@sha256:" + "a" * 64,
            "DASHBOARD_IMAGE_REF": "evil.example/dashboard@sha256:" + "a" * 64,
            "FINDB_PUBLIC_HOST": "findb.example.test",
        },
    )
    assert invalid_findb.returncode != 0
    assert "reason=ecr_image_contract" in invalid_findb.stderr

    valid_findb = subprocess.run(
        ["bash", str(findb_helper)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **environment,
            "AWS_REGION": "ap-southeast-1",
            "ECR_REGISTRY": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com",
            "FINDB_IMAGE_REF": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/backend@sha256:"
            + "a" * 64,
            "DASHBOARD_IMAGE_REF": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/dashboard@sha256:"
            + "b" * 64,
            "FINDB_PUBLIC_HOST": "findb.example.test",
        },
    )
    assert valid_findb.returncode != 0
    assert "reason=compose_missing" in valid_findb.stderr

    rejected_fetcher = subprocess.run(
        [
            "bash",
            str(fetcher_helper),
            "finlab",
            "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/fetcher/shioaji:"
            + "a" * 40,
            "/tmp/state",
            "/tmp/state/state.sqlite3",
            "stable",
            "candidate",
            "previous",
            "preflight",
            "-",
            "scheduler",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert rejected_fetcher.returncode != 0
    assert "reason=ecr_image_contract" in rejected_fetcher.stderr

    accepted_fetcher = subprocess.run(
        [
            "bash",
            str(fetcher_helper),
            "finlab",
            "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/fetcher/finlab@sha256:"
            + "a" * 64,
            "/tmp/state",
            "/tmp/state/state.sqlite3",
            "stable",
            "candidate",
            "previous",
            "preflight",
            "-",
            "scheduler",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert accepted_fetcher.returncode != 0
    assert "reason=required_value_missing" in accepted_fetcher.stderr

    unsafe_shioaji = subprocess.run(
        [
            "bash",
            str(fetcher_helper),
            "shioaji",
            "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/fetcher/shioaji:"
            + "a" * 40,
            "/tmp/state",
            "/tmp/state/state.sqlite3",
            "stable",
            "candidate",
            "previous",
            "preflight",
            "-",
            "scheduler",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**environment, "SHIOAJI_SIMULATION": "false"},
    )
    assert unsafe_shioaji.returncode != 0
    assert "reason=shioaji_simulation_required" in unsafe_shioaji.stderr
    assert not docker_log.exists()

    runtime_command = (
        REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "runtime_secret_command.sh"
    )
    rejected_runtime_region = subprocess.run(
        [
            "bash",
            str(runtime_command),
            "--catalog",
            "/opt/findb/runtime-secrets/findb.json",
            "--region",
            "us-east-1",
            "--deployment-target",
            "staging",
            "--aws-account-id",
            "439622209937",
            "--consumer",
            "compose",
            "--",
            "true",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert rejected_runtime_region.returncode != 0
    assert "runtime_secret_command=failed reason=region_invalid" in rejected_runtime_region.stderr

    for helper, arguments, expected_reason in (
        (
            findb_helper,
            [],
            "findb_aws_deploy=failed reason=region_invalid",
        ),
        (
            fetcher_helper,
            [
                "finlab",
                "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/fetcher/finlab:"
                + "a" * 40,
                "/tmp/state",
                "/tmp/state/state.sqlite3",
                "stable",
                "candidate",
                "previous",
                "preflight",
                "-",
                "scheduler",
            ],
            "release_fetcher_provider=failed reason=region_invalid",
        ),
    ):
        rejected_region = subprocess.run(
            ["bash", str(helper), *arguments],
            capture_output=True,
            text=True,
            check=False,
            env={
                **environment,
                "AWS_REGION": "us-east-1",
                "IMAGE_TAG": "a" * 40,
                "ECR_REGISTRY": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com",
                "FINDB_IMAGE": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/backend",
                "DASHBOARD_IMAGE": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/dashboard",
                "FINDB_PUBLIC_HOST": "findb.example.test",
            },
        )
        assert rejected_region.returncode != 0
        assert expected_reason in rejected_region.stderr
    assert not docker_log.exists()


def test_immutable_ecr_build_helper_reuses_or_builds_only_after_exact_tag_inspection(
    tmp_path: Path,
) -> None:
    helper = REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "build_ecr_image_if_missing.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    aws_args_log = tmp_path / "aws-args.log"
    github_output = tmp_path / "github-output"
    (fake_bin / "aws").write_text(
        f"""#!/bin/sh
test_tag="${{ECR_IMAGE_TAG:-${{GITHUB_SHA:?}}}}"
printf '%s\\n' "$*" >> {aws_args_log}
case "${{ECR_TEST_MODE:?}}" in
  present) printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  digest_no_newline) printf '%s' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  digest_empty) : ;;
  digest_malformed) printf '%s\\n' 'sha256:UPPERCASE' ;;
  digest_multiple) printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' ;;
  digest_two_lf) printf '%s\\n\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  digest_crlf) printf '%s\\r\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  digest_leading_space) printf ' %s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  digest_trailing_space) printf '%s \\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  digest_nul) printf '%s\\0\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  absent)
    if [ -f "${{ECR_TEST_STATE:?}}" ]; then
      printf '%s\\n' 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    else
      printf '%s\\n' '{{"Code":"ImageNotFoundException","Message":"tag absent"}}' >&2; exit 255
    fi
    ;;
  absent_digest_two_lf)
    if [ -f "${{ECR_TEST_STATE:?}}" ]; then
      printf '%s\\n\\n' 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    else
      printf '%s\\n' '{{"Code":"ImageNotFoundException","Message":"tag absent"}}' >&2; exit 255
    fi
    ;;
  never_visible) printf '%s\n' '{{"Code":"ImageNotFoundException","Message":"tag absent"}}' >&2; exit 255 ;;
  malformed) printf '%s\\n' '{{"Code":"ImageNotFoundException","Message":"tag absent"' >&2; exit 255 ;;
  other_code) printf '%s\\n' '{{"Code":"ThrottlingException","Message":"retry later"}}' >&2; exit 255 ;;
  access_denied) printf '%s\\n' '{{"Code":"AccessDeniedException","Message":"denied"}}' >&2; exit 255 ;;
  repository_not_found) printf '%s\\n' '{{"Code":"RepositoryNotFoundException","Message":"not found"}}' >&2; exit 255 ;;
  multiple) printf '%s\\n' '{{"Code":"ImageNotFoundException","Message":"tag absent"}}' '{{"Code":"ThrottlingException","Message":"retry later"}}' >&2; exit 255 ;;
  array) printf '%s\\n' '[{{"Code":"ImageNotFoundException","Message":"tag absent"}}]' >&2; exit 255 ;;
  scalar) printf '%s\\n' '"ImageNotFoundException"' >&2; exit 255 ;;
  nul) printf '%s\\0' '{{"Code":"ImageNotFoundException","Message":"tag absent"}}' >&2; exit 255 ;;
  empty_message) printf '%s\\n' '{{"Code":"ImageNotFoundException","Message":""}}' >&2; exit 255 ;;
esac
""",
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> {docker_log}\n: > "${{ECR_TEST_STATE:?}}"\n',
        encoding="utf-8",
    )
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for command in ("aws", "docker", "sleep"):
        (fake_bin / command).chmod(0o755)
    base_environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AWS_REGION": "ap-southeast-1",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "ECR_TEST_STATE": str(tmp_path / "ecr-present"),
    }
    image = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/backend"

    present = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **base_environment,
            "ECR_TEST_MODE": "present",
            "GITHUB_OUTPUT": str(github_output),
        },
    )
    assert present.returncode == 0, present.stderr
    assert "staging_ecr_build=reused" in present.stdout
    assert not docker_log.exists()
    assert github_output.read_text(encoding="utf-8") == (
        "image_digest=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "image_ref=439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/backend@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    assert "--cli-error-format json" in aws_args_log.read_text(encoding="utf-8")

    github_output.unlink()
    no_newline = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **base_environment,
            "ECR_TEST_MODE": "digest_no_newline",
            "GITHUB_OUTPUT": str(github_output),
        },
    )
    assert no_newline.returncode == 0, no_newline.stderr
    assert "staging_ecr_build=reused" in no_newline.stdout

    absent = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={**base_environment, "ECR_TEST_MODE": "absent"},
    )
    assert absent.returncode == 0, absent.stderr
    assert "staging_ecr_build=pushed" in absent.stdout
    assert "buildx build" in docker_log.read_text(encoding="utf-8")

    docker_log.unlink()
    (tmp_path / "ecr-present").unlink()
    describe_before = aws_args_log.read_text(encoding="utf-8").count("describe-images")
    never_visible = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={**base_environment, "ECR_TEST_MODE": "never_visible"},
    )
    assert never_visible.returncode != 0
    assert "reason=ecr_digest_resolution_failed" in never_visible.stderr
    describe_after = aws_args_log.read_text(encoding="utf-8").count("describe-images")
    assert describe_after - describe_before == 6
    docker_log.unlink()
    (tmp_path / "ecr-present").unlink()
    for mode in (
        "digest_empty",
        "digest_malformed",
        "digest_multiple",
        "digest_two_lf",
        "digest_crlf",
        "digest_leading_space",
        "digest_trailing_space",
        "digest_nul",
        "malformed",
        "other_code",
        "access_denied",
        "repository_not_found",
        "multiple",
        "array",
        "scalar",
        "nul",
        "empty_message",
    ):
        rejected = subprocess.run(
            ["bash", str(helper), image, ".", "./backend/Dockerfile"],
            capture_output=True,
            text=True,
            check=False,
            env={**base_environment, "ECR_TEST_MODE": mode},
        )
        assert rejected.returncode != 0
        assert (
            "reason=ecr_digest_invalid" in rejected.stderr
            or "reason=ecr_tag_inspection_failed" in rejected.stderr
        )
        assert not docker_log.exists()

    post_push_invalid = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={**base_environment, "ECR_TEST_MODE": "absent_digest_two_lf"},
    )
    assert post_push_invalid.returncode != 0
    assert "reason=ecr_digest_invalid" in post_push_invalid.stderr
    assert "buildx build" in docker_log.read_text(encoding="utf-8")
    docker_log.unlink()
    (tmp_path / "ecr-present").unlink()

    rollback_absent = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **base_environment,
            "ECR_TEST_MODE": "absent",
            "ECR_IMAGE_TAG": "b" * 40,
            "ECR_REUSE_ONLY": "true",
        },
    )
    assert rollback_absent.returncode != 0
    assert "reason=rollback_tag_not_found" in rollback_absent.stderr
    assert not docker_log.exists()

    (fake_bin / "jq").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    (fake_bin / "jq").chmod(0o755)
    jq_failure = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={**base_environment, "ECR_TEST_MODE": "absent"},
    )
    assert jq_failure.returncode != 0
    assert "reason=ecr_tag_inspection_failed" in jq_failure.stderr
    assert not docker_log.exists()

    unexpected_aws_log = tmp_path / "unexpected-aws.log"
    (fake_bin / "aws").write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {unexpected_aws_log}\nexit 99\n",
        encoding="utf-8",
    )
    (fake_bin / "aws").chmod(0o755)
    forward_tag_mismatch = subprocess.run(
        ["bash", str(helper), image, ".", "./backend/Dockerfile"],
        capture_output=True,
        text=True,
        check=False,
        env={
            **base_environment,
            "ECR_IMAGE_TAG": "b" * 40,
            "ECR_REUSE_ONLY": "false",
        },
    )
    assert forward_tag_mismatch.returncode != 0
    assert "reason=publisher_identity_or_sha_contract" in forward_tag_mismatch.stderr
    assert not unexpected_aws_log.exists()
    assert not docker_log.exists()


def test_runtime_secret_helpers_enforce_tmpfs_cleanup_and_registry_isolation() -> None:
    command = (REPO_ROOT / "infra/deploy/runtime-secrets/runtime_secret_command.sh").read_text(
        encoding="utf-8"
    )
    assert "set +x" in command
    assert 'python3 "$loader" --ensure-runtime-root' in command
    assert "runtime_root_bootstrap_failed" in command
    assert 'findmnt -n -o FSTYPE -T "$output"' in command
    assert 'loaded_files+=("$output")' in command
    assert 'rm -f -- "$output"' in command
    assert 'rm -f -- "$file"' in command
    assert 'unset "$key"' in command
    assert "unset DOCKER_CONFIG" in command
    assert 'mktemp -d "$runtime_root/docker-config.XXXXXX"' in command
    assert 'docker logout "$ecr_registry"' in command
    assert "docker inspect Config.Env" not in command
    assert " docker compose" not in command
    assert " env " not in command

    findb = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    migration_start = findb.index("run_runtime --consumer migration")
    migration_end = findb.index("writer_services=", migration_start)
    migration_block = findb[migration_start:migration_end]
    assert "MIGRATION_DATABASE_URL=DATABASE_URL" in migration_block
    assert "run --rm --no-deps" in migration_block
    assert " up " not in migration_block
    assert findb.count("--map MIGRATION_DATABASE_URL=DATABASE_URL") == 2
    second_migration_start = findb.rindex("run_runtime --consumer migration")
    long_lived_start = findb.index("run_runtime --consumer compose", second_migration_start)
    assert "MIGRATION_DATABASE_URL" not in findb[long_lived_start:]
    up_script = findb.split("<<'UP_SCRIPT'\n", 1)[1].split("\nUP_SCRIPT", 1)[0]
    assert "exec -T -e CELERY_BROKER_URL ingest" in up_script


def test_lookup_secret_is_rendered_only_to_tmpfs_and_compose_never_mounts_persistent_key() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    nginx_volumes = compose["services"]["nginx"]["volumes"]
    assert (
        "/run/findb-runtime-secrets/nginx/serve-key.conf:/etc/nginx/serve-key.conf:ro"
        in nginx_volumes
    )
    assert not any("/home/ubuntu/etc/nginx/serve-key.conf:" in volume for volume in nginx_volumes)
    for tls_file in ("server.crt", "server.key"):
        variable = (
            "FINDB_TLS_CERT_PATH" if tls_file == "server.crt" else "FINDB_TLS_PRIVATE_KEY_PATH"
        )
        assert any(
            volume.startswith(f"${{{variable}:-/home/ubuntu/etc/nginx/ssl/{tls_file}}}:")
            for volume in nginx_volumes
        )
    for config in ("nginx.conf", "cloudflare-real-ip.conf", "source-allowlist.conf"):
        assert any(
            f"${{FINDB_NGINX_CONFIG_DIR:-/home/ubuntu/etc/nginx}}/{config}:" in volume
            for volume in nginx_volumes
        )

    renderer = (REPO_ROOT / "infra/deploy/runtime-secrets/render_serve_key.py").read_text(
        encoding="utf-8"
    )
    assert "/run/findb-runtime-secrets" in renderer
    assert "os.O_NOFOLLOW" in renderer
    assert "0o600" in renderer
    findb = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    assert "render_nginx_runtime.sh" in findb
    assert "/home/ubuntu/etc/nginx/serve-key.conf" not in findb
    assert '"$AWS_ACCOUNT_ID" validate' in findb
    assert "candidate_nginx_runtime_validated" in findb
    assert "tls_file_missing_or_unsafe" in findb
    assert 'nginx_config_dir="${FINDB_NGINX_CONFIG_DIR:-/home/ubuntu/etc/nginx}"' in findb
    assert '[ "$nginx_config_dir" != /etc/findb/nginx ]' in findb
    assert "require_root_owned_nginx_directory /etc/findb" in findb
    assert "nginx_config_target_unsafe" in findb
    assert 'chown root:root -- "$nginx_config_dir/$conf"' in findb
    assert 'chmod 0644 -- "$nginx_config_dir/$conf"' in findb
    assert "nginx_config_metadata_update_failed" in findb
    assert "nginx_config_metadata_invalid" in findb
    nginx_helper = (REPO_ROOT / "infra/deploy/runtime-secrets/render_nginx_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "runtime_output_dir=/run/findb-runtime-secrets/nginx" in nginx_helper
    assert 'serve_key_output="$runtime_output_dir/serve-key.conf"' in nginx_helper
    assert 'certificate_output="$runtime_output_dir/server.crt"' in nginx_helper
    assert 'private_key_output="$runtime_output_dir/server.key"' in nginx_helper
    assert 'openssl x509 -in "$certificate_tmp" -noout -checkhost "$public_host"' in nginx_helper
    assert 'openssl x509 -in "$certificate_tmp" -noout -checkend 2592000' in nginx_helper
    assert "tls_keypair_mismatch" in nginx_helper
    assert "tls_output_unsafe" in nginx_helper
    assert 'render_mode="${6:-materialize}"' in nginx_helper
    assert "validate|materialize" in nginx_helper
    assert (
        "/opt/findb/releases/[0-9a-f]{64}-[0-9]+-[0-9]+/infra/deploy/runtime-secrets/findb"
        in nginx_helper
    )
    assert (
        "/opt/findb/releases/[0-9a-f]{64}-[0-9]+-[0-9]+-findb/infra/deploy/runtime-secrets/findb"
        in nginx_helper
    )
    assert '[ "$deployment_target" = staging ]' in nginx_helper
    assert '[ "$catalog" = /opt/findb/runtime-secrets/findb.json ]' in nginx_helper
    assert 'runtime_dir="${catalog%/findb.json}"' in nginx_helper
    assert 'renderer="$runtime_dir/render_serve_key.py"' in nginx_helper
    assert "renderer_metadata_invalid" in nginx_helper
    assert 'python3 "$renderer"' in nginx_helper
    assert "/opt/findb/runtime-secrets/render_serve_key.py" not in nginx_helper


def test_nginx_runtime_catalog_path_gate_accepts_v2_and_staging_v1_only() -> None:
    helper = REPO_ROOT / "infra/deploy/runtime-secrets/render_nginx_runtime.sh"
    identity = f"{'a' * 64}-34025994515-1"
    current_catalog = f"/opt/findb/releases/{identity}/infra/deploy/runtime-secrets/findb.json"
    legacy_catalog = f"/opt/findb/releases/{identity}-findb/infra/deploy/runtime-secrets/findb.json"

    for target, catalog in (
        ("staging", current_catalog),
        ("production", current_catalog),
        ("staging", legacy_catalog),
        ("production", "/opt/findb/runtime-secrets/findb.json"),
    ):
        result = subprocess.run(
            [
                "bash",
                str(helper),
                catalog,
                "ap-southeast-1",
                "findb.example.com",
                target,
                "439622209937",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "renderer_metadata_invalid" in result.stderr
        assert "catalog_path_invalid" not in result.stderr

    for target, catalog in (
        ("production", legacy_catalog),
        (
            "staging",
            f"/opt/findb/releases/{identity}-fetcher/infra/deploy/runtime-secrets/findb.json",
        ),
        (
            "staging",
            f"/opt/findb/releases/{'A' * 64}-34025994515-1/infra/deploy/runtime-secrets/findb.json",
        ),
    ):
        result = subprocess.run(
            [
                "bash",
                str(helper),
                catalog,
                "ap-southeast-1",
                "findb.example.com",
                target,
                "439622209937",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "catalog_path_invalid" in result.stderr


def test_findb_deploy_helper_selects_explicit_release_or_legacy_runtime_paths() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    setup = helper.split('catalog="$runtime_dir/findb.json"', 1)[0]
    preserve_start = helper.index("preserve_env=")
    preserve_end = helper.index("\n\nrun_runtime()", preserve_start)
    preserve_setup = helper[preserve_start:preserve_end]
    for environment, expected in (
        (
            {
                "FINDB_RELEASE_ROOT": "/opt/findb/releases/demo",
                "COMPOSE_PROJECT_NAME": "unexpected",
            },
            "/opt/findb/releases/demo/infra/deploy/runtime-secrets|/opt/findb/releases/demo/docker-compose.prod.yml|findb",
        ),
        (
            {"COMPOSE_PROJECT_NAME": "unexpected"},
            "/opt/findb/runtime-secrets|/opt/findb/docker-compose.prod.yml|unexpected",
        ),
    ):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'''{setup}
{preserve_setup}
case ",$preserve_env," in
  *,COMPOSE_PROJECT_NAME,*)
    effective_project="$(env -i "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME" /bin/bash -c 'printf "%s" "${{COMPOSE_PROJECT_NAME-unset}}"')"
    ;;
  *) effective_project="$(env -i /bin/bash -c 'printf "%s" "${{COMPOSE_PROJECT_NAME-unset}}"')" ;;
esac
printf "%s|%s|%s|%s\\n" "$runtime_dir" "$compose_file" "${{COMPOSE_PROJECT_NAME-unset}}" "$effective_project"''',
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, **environment},
        )
        assert result.returncode == 0
        expected_effective = "findb" if "FINDB_RELEASE_ROOT" in environment else "unset"
        assert result.stdout == f"{expected}|{expected_effective}\n"

    assert "COMPOSE_PROJECT_NAME=findb" in setup
    assert "export COMPOSE_PROJECT_NAME" in setup
    assert "COMPOSE_PROJECT_NAME" not in preserve_setup.split("\n", 1)[0].split(",")
    assert (
        'preserve_env="${preserve_env},COMPOSE_PROJECT_NAME,FINDB_RELEASE_ROOT,FINDB_DEPLOY_MODE,PREDEPLOY_EXPECTED_ALEMBIC_REVISION,PREDEPLOY_EXPECTED_RDS_ENDPOINT"'
        in preserve_setup
    )
    assert 'sudo --preserve-env="$preserve_env" "$runtime_command"' in helper


def test_findb_runtime_secret_deploy_replaces_verified_nginx_after_rendering_lookup_key() -> None:
    deploy = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )

    render_at = deploy.index('"$nginx_runtime" "$catalog" "$AWS_REGION" "$FINDB_PUBLIC_HOST"')
    main_up_at = deploy.index('docker compose -f "$compose_file" up -d --remove-orphans </dev/null')
    old_id_at = deploy.index(
        'nginx_old_id="$(docker compose -f "$compose_file" ps -q nginx </dev/null)"'
    )
    stop_at = deploy.index('docker stop --time 30 "$nginx_old_id"')
    remove_at = deploy.index('docker rm "$nginx_old_id"')
    old_absent_at = deploy.index('docker inspect "$nginx_old_id" </dev/null >/dev/null 2>&1')
    create_at = deploy.index('docker compose -f "$compose_file" up -d --no-deps nginx </dev/null')
    new_id_at = deploy.index(
        'nginx_new_id="$(docker compose -f "$compose_file" ps -q nginx </dev/null)"'
    )
    nginx_health_at = deploy.index('health_status="$(docker inspect', create_at)
    public_acceptance_at = deploy.index("for dashboard_path in /dashboard/ /dashboard/lookup")
    lookup_probe_at = deploy.index('lookup_referer="https://$FINDB_PUBLIC_HOST/dashboard/lookup"')

    assert (
        render_at
        < main_up_at
        < old_id_at
        < stop_at
        < remove_at
        < old_absent_at
        < create_at
        < new_id_at
    )
    assert new_id_at < nginx_health_at < public_acceptance_at < lookup_probe_at
    assert 'docker compose -f "$compose_file" restart nginx' not in deploy
    assert "--force-recreate nginx" not in deploy
    assert "nginx_expected_project=findb" in deploy
    assert "nginx_expected_service=nginx" in deploy
    assert "reason=nginx_container_identity_invalid" in deploy
    assert "reason=nginx_old_container_still_exists" in deploy
    assert "reason=nginx_container_not_replaced" in deploy
    assert "reason=nginx_started_at_unchanged" in deploy
    assert "reason=nginx_replacement_contract_invalid" in deploy
    assert "nginx_serve_key_source=/run/findb-runtime-secrets/nginx/serve-key.conf" in deploy
    assert "{{.Source}} {{.RW}}" in deploy
    assert '--header="Referer: $lookup_referer"' in deploy
    assert '"https://127.0.0.1/api/v1/serve/instruments?include_count=false&page_size=1"' in deploy


def test_findb_staging_candidate_acceptance_checks_durable_topology_and_worker_ping() -> None:
    deploy = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    up_script = deploy.split("<<'UP_SCRIPT'\n", 1)[1].split("\nUP_SCRIPT", 1)[0]
    topology = up_script.index("rabbitmqctl list_queues -p /findb name durable --formatter json")
    durable_check = up_script.index('"findb.normalize.v1", "findb.normalize.dlq.v1"', topology)
    worker_ping = up_script.index("celery -A app.task_queue inspect ping --timeout=10")
    queue_health = up_script.index("python /app/scripts/check_queue_health.py")
    candidate_success = deploy.index("findb_aws_deploy=candidate_ready_for_acceptance")

    assert "rabbitmq_topology_invalid vhost=/findb" in up_script
    assert 'item.get("durable") is True' in up_script
    assert "celery_worker_ping_failed" in up_script
    assert topology < durable_check < worker_ping < queue_health
    assert up_script.index("findb_aws_deploy=candidate_checks_passed") < candidate_success
    assert "cleanup_unaccepted_candidate()" in deploy
    assert "unaccepted_candidate_stopped" in deploy
    for container in (
        "findb-nginx",
        "findb-dashboard",
        "findb-serve",
        "findb-ingest",
        "findb-dispatcher",
        "findb-worker",
        "findb-raw-cleanup",
    ):
        assert container in deploy
    assert "RabbitMQ and its durable volume" in deploy
    assert "docker ps -a --format '{{.Names}}'" in deploy
    assert 'docker stop --time 30 "$container"' in deploy
    assert "release_services_may_have_started=1" in deploy
    assert deploy.index("findb_aws_deploy=candidate_services_fail_stopped") < candidate_success
    assert candidate_success < deploy.index("install_findb_bootstrap.sh")


def test_findb_candidate_cleanup_surfaces_a_docker_stop_failure(tmp_path: Path) -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    cleanup_start = helper.index("fixed_candidate_containers=(")
    cleanup_end = helper.index("\ncleanup_unaccepted_candidate()", cleanup_start)
    cleanup = helper[cleanup_start:cleanup_end]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "$1" in\n'
        "  ps) printf '%s\\n' findb-nginx findb-dashboard findb-serve findb-ingest findb-dispatcher findb-worker findb-raw-cleanup ;;\n"
        "  inspect) printf '%s\\n' 'running true false' ;;\n"
        "  stop) exit 71 ;;\n"
        "  *) exit 72 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{cleanup}\nstop_fixed_candidate_containers"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode != 0
    assert "candidate_cleanup_docker_stop_failed container=findb-nginx" in result.stderr
    assert "candidate_cleanup_failed" in result.stderr


def test_findb_nginx_replacement_shell_contract_is_verified_and_fail_closed(tmp_path: Path) -> None:
    deploy = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    replacement_start = deploy.index("nginx_expected_project=findb")
    replacement_end = deploy.index("ready=0", replacement_start)
    replacement = deploy[replacement_start:replacement_end]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    state_file = tmp_path / "nginx-state"
    state_file.write_text("old-container", encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
state="$(cat "$FAKE_NGINX_STATE")"
case "$1" in
  compose)
    case "$*" in
      *" ps -q nginx")
        [ "$state" = "removed" ] && exit 0
        printf '%s\\n' "$state"
        ;;
      *" up -d --no-deps nginx")
        if [ "${FAKE_FAIL_COMMAND:-}" = "compose-create" ]; then exit 96; fi
        if [ "${FAKE_SAME_ID:-}" = "1" ]; then
          printf '%s' old-container > "$FAKE_NGINX_STATE"
        else
          printf '%s' new-container > "$FAKE_NGINX_STATE"
        fi
        ;;
      *) exit 90 ;;
    esac
    ;;
  stop)
    [ "$2" = "--time" ] && [ "$3" = "30" ] && [ "$4" = "old-container" ] || exit 91
    if [ "${FAKE_FAIL_COMMAND:-}" = "stop" ]; then exit 97; fi
    ;;
  rm)
    [ "$2" = "old-container" ] || exit 92
    if [ "${FAKE_FAIL_COMMAND:-}" = "rm" ]; then exit 98; fi
    printf '%s' removed > "$FAKE_NGINX_STATE"
    ;;
  inspect)
    if [ "$2" = "--format" ]; then
      format="$3"
      container="$4"
      case "$format" in
        *"com.docker.compose.project"*) printf '%s\\n' "${FAKE_PROJECT:-findb}" ;;
        *"com.docker.compose.service"*) printf '%s\\n' "${FAKE_SERVICE:-nginx}" ;;
        *".State.StartedAt"*)
          [ "$container" = "old-container" ] && printf '%s\\n' old-start || printf '%s\\n' new-start
          ;;
        *"serve-key.conf"*)
          printf '%s\\n' "${FAKE_NGINX_MOUNT:-/run/findb-runtime-secrets/nginx/serve-key.conf false}"
          ;;
        *) exit 93 ;;
      esac
    elif [ "$2" = "old-container" ] && [ "$state" = "removed" ]; then
      exit 1
    else
      exit 94
    fi
    ;;
  *) exit 95 ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(docker_log),
        "FAKE_NGINX_STATE": str(state_file),
    }
    after_marker = tmp_path / "after-replacement"
    command = (
        f"set -euo pipefail\ncompose_file=/tmp/compose.yml\n{replacement}"
        'touch "$FAKE_AFTER_MARKER"\n'
    )
    environment["FAKE_AFTER_MARKER"] = str(after_marker)

    completed = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, check=False, env=environment
    )
    assert completed.returncode == 0, completed.stderr
    assert state_file.read_text(encoding="utf-8") == "new-container"
    assert after_marker.exists()
    assert docker_log.read_text(encoding="utf-8").splitlines() == [
        "compose -f /tmp/compose.yml ps -q nginx",
        'inspect --format {{index .Config.Labels "com.docker.compose.project"}} old-container',
        'inspect --format {{index .Config.Labels "com.docker.compose.service"}} old-container',
        "inspect --format {{.State.StartedAt}} old-container",
        "stop --time 30 old-container",
        "rm old-container",
        "inspect old-container",
        "compose -f /tmp/compose.yml up -d --no-deps nginx",
        "compose -f /tmp/compose.yml ps -q nginx",
        "inspect --format {{.State.StartedAt}} new-container",
        'inspect --format {{index .Config.Labels "com.docker.compose.project"}} new-container',
        'inspect --format {{index .Config.Labels "com.docker.compose.service"}} new-container',
        'inspect --format {{range .Mounts}}{{if eq .Destination "/etc/nginx/serve-key.conf"}}{{.Source}} {{.RW}}{{end}}{{end}} new-container',
    ]

    for overrides, expected_error in (
        ({"FAKE_PROJECT": "unexpected"}, "reason=nginx_container_identity_invalid"),
        ({"FAKE_FAIL_COMMAND": "stop"}, ""),
        ({"FAKE_FAIL_COMMAND": "rm"}, ""),
        ({"FAKE_FAIL_COMMAND": "compose-create"}, ""),
        ({"FAKE_SAME_ID": "1"}, "reason=nginx_container_not_replaced"),
        (
            {"FAKE_NGINX_MOUNT": "/tmp/unexpected true"},
            "reason=nginx_replacement_contract_invalid",
        ),
    ):
        state_file.write_text("old-container", encoding="utf-8")
        docker_log.write_text("", encoding="utf-8")
        after_marker.unlink(missing_ok=True)
        rejected = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            check=False,
            env={**environment, **overrides},
        )
        assert rejected.returncode != 0
        assert expected_error in rejected.stderr
        assert not after_marker.exists()


def test_findb_up_script_isolates_docker_stdin_before_nginx_replacement(tmp_path: Path) -> None:
    deploy = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    up_script = deploy.split("<<'UP_SCRIPT'\n", 1)[1].split("\nUP_SCRIPT", 1)[0]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    state_file = tmp_path / "nginx-state"
    state_file.write_text("old-container", encoding="utf-8")
    marker = tmp_path / "after-up-script"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
state="$(cat "$FAKE_NGINX_STATE")"
case "$1" in
  compose)
    case "$*" in
      *" up -d --remove-orphans") cat >/dev/null ;;
      *" up -d --no-deps nginx") cat >/dev/null; printf '%s' new-container > "$FAKE_NGINX_STATE" ;;
      *" ps -q nginx")
        [ "$state" = "removed" ] && exit 0
        printf '%s\\n' "$state"
        ;;
      *" ps --status running --services") printf '%s\\n' dispatcher worker ;;
      *"rabbitmqctl list_queues -p /findb name durable --formatter json"*)
        printf '%s' '[{"name":"findb.normalize.v1","durable":true},{"name":"findb.normalize.dlq.v1","durable":true}]'
        ;;
      *"celery -A app.task_queue inspect ping --timeout=10"*) : ;;
      *" exec "*) : ;;
      *) exit 90 ;;
    esac
    ;;
  stop) [ "$2" = "--time" ] && [ "$3" = "30" ] && [ "$4" = "old-container" ] || exit 91 ;;
  rm) [ "$2" = "old-container" ] || exit 92; printf '%s' removed > "$FAKE_NGINX_STATE" ;;
  inspect)
    if [ "$2" = "--format" ]; then
      format="$3"
      container="$4"
      case "$format" in
        *"com.docker.compose.project"*) printf '%s\\n' findb ;;
        *"com.docker.compose.service"*) printf '%s\\n' nginx ;;
        *".State.StartedAt"*)
          [ "$container" = "old-container" ] && printf '%s\\n' old-start || printf '%s\\n' new-start
          ;;
        *"serve-key.conf"*) printf '%s\\n' '/run/findb-runtime-secrets/nginx/serve-key.conf false' ;;
        *".State.Health"*) printf '%s\\n' healthy ;;
        *) exit 93 ;;
      esac
    elif [ "$2" = "old-container" ] && [ "$state" = "removed" ]; then
      exit 1
    else
      exit 94
    fi
    ;;
  image) : ;;
  *) exit 95 ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    completed = subprocess.run(
        ["bash", "-s", "--", "/tmp/compose.yml"],
        input=f'{up_script}\ntouch "$FAKE_AFTER_UP_SCRIPT"\n',
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_NGINX_STATE": str(state_file),
            "FAKE_AFTER_UP_SCRIPT": str(marker),
            "FINDB_PUBLIC_HOST": "findb.example.test",
            "IMAGE_TAG": "a" * 40,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert marker.exists()
    commands = docker_log.read_text(encoding="utf-8").splitlines()
    main_up_at = commands.index("compose -f /tmp/compose.yml up -d --remove-orphans")
    replacement_up_at = commands.index("compose -f /tmp/compose.yml up -d --no-deps nginx")
    lookup_probe_at = commands.index(
        "compose -f /tmp/compose.yml exec -T nginx wget -q --no-check-certificate --spider "
        "--header=Referer: https://findb.example.test/dashboard/lookup "
        "https://127.0.0.1/api/v1/serve/instruments?include_count=false&page_size=1"
    )
    assert main_up_at < replacement_up_at < lookup_probe_at


def test_aws_fetcher_release_preserves_provider_specific_nonsecret_runtime_inputs() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text(
        encoding="utf-8"
    )
    for name in (
        "TWELVE_DATA_BASE_URL",
        "TWELVE_DATA_TIMEOUT_SECONDS",
        "TWELVE_DATA_MAX_RESPONSE_BYTES",
    ):
        assert f"--env {name}" in helper

    deploy_helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text(
        encoding="utf-8"
    )
    assert "/var/lib/findb-shioaji-fetcher/cache" in deploy_helper


def test_aws_fetcher_release_preserves_staging_raw_marker_identities() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text(
        encoding="utf-8"
    )
    expected_markers = {
        "twelve-data": (
            "twelve",
            "92b76aa7bc713b08fe36ba8abe76aa17d67add8ac5f8dae868c147d78a5db45f",
        ),
        "finlab": (
            "finlab",
            "99b6f6dde7c65ac33573f3d766ba6fd0686e4303641c0cf23e44d7819931b812",
        ),
        "shioaji": (
            "shioaji",
            "d5d296ea62e87b971916f6e0ec7025c172aba9cbe0a0060dc8ac3675970bffcc",
        ),
    }
    account_id = "ef6190725fbf3a4331203b901a2d2961"
    raw_bucket = "findb-staging-raw"

    for selector, (identity, expected_marker) in expected_markers.items():
        assert re.search(
            rf"{re.escape(selector)}\)\n    marker_identity=\"{re.escape(identity)}\"",
            helper,
        )
        marker = hashlib.sha256(f"{account_id}\n{raw_bucket}\n{identity}".encode()).hexdigest()
        assert marker == expected_marker

    assert '"$CLOUDFLARE_R2_RAW_BUCKET" "$marker_identity"' in helper


def _run_twelve_fetcher_release_with_marker(
    tmp_path: Path,
    *,
    marker_identity: str,
    residual_candidate: bool = False,
    residual_previous: bool = False,
    unaccepted_stable: bool = False,
    transactional_candidate: bool = False,
    preflight_fails: bool = False,
) -> subprocess.CompletedProcess[str]:
    helper = REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh"
    state_dir = tmp_path / "state"
    cache_dir = tmp_path / "cache"
    fake_bin = tmp_path / "bin"
    state_dir.mkdir(parents=True)
    fake_bin.mkdir()
    state_path = state_dir / "state.sqlite3"
    state_path.write_text("durable-state\n", encoding="utf-8")
    if residual_candidate:
        (state_dir / "candidate").write_text("running\n", encoding="utf-8")
    if residual_previous:
        (state_dir / "previous").write_text("stopped\n", encoding="utf-8")
    if unaccepted_stable:
        (state_dir / "stable").write_text("running\n", encoding="utf-8")
    marker = hashlib.sha256(
        f"ef6190725fbf3a4331203b901a2d2961\nfindb-staging-raw\n{marker_identity}".encode()
    ).hexdigest()
    (state_dir / "raw-bucket.sha256").write_text(marker + "\n", encoding="utf-8")

    fake_docker = fake_bin / "docker"
    fake_docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(_FAKE_SUDO, encoding="utf-8")
    fake_sudo.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)

    image = (
        "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/fetcher/twelve-data@sha256:"
        + "a" * 64
    )
    environment = dict(os.environ)
    environment.update(
        AWS_REGION="ap-southeast-1",
        AWS_ACCOUNT_ID="439622209937",
        DEPLOYMENT_TARGET="staging",
        ECR_REGISTRY="439622209937.dkr.ecr.ap-southeast-1.amazonaws.com",
        FETCHER_SOURCE_API_URL="https://findb.example.test",
        FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY="source-key",
        TWELVE_DATA_API_KEY="provider-key",
        FINDB_SERVE_BASE_URL="https://findb.example.test",
        FETCHER_CALENDAR_SERVE_API_KEY="calendar-key",
        CLOUDFLARE_R2_ACCOUNT_ID="ef6190725fbf3a4331203b901a2d2961",
        CLOUDFLARE_R2_RAW_BUCKET="findb-staging-raw",
        CLOUDFLARE_R2_RAW_ACCESS_KEY_ID="r2-access-key",
        CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY="r2-secret-key",
        PATH=f"{fake_bin}:{environment['PATH']}",
        FAKE_DOCKER_STATE=str(state_dir),
        FAKE_DOCKER_LOG=str(tmp_path / "docker.log"),
        FAKE_IMAGE=image,
        FAKE_PREFLIGHT_FAIL="1" if preflight_fails else "0",
        FAKE_UNACCEPTED_NAMES="stable" if unaccepted_stable else "",
        FAKE_SIGNAL_SENT=str(tmp_path / "signal-sent"),
        FETCHER_PROVIDER_RELEASE_MODE="transactional" if transactional_candidate else "legacy",
        FETCHER_DEPLOY_MODE="candidate" if transactional_candidate else "activate",
    )
    return subprocess.run(
        [
            "bash",
            str(helper),
            "twelve-data",
            image,
            str(state_dir),
            str(state_path),
            "stable",
            "candidate",
            "previous",
            "preflight",
            str(cache_dir),
            "scheduler",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_aws_fetcher_release_accepts_legacy_twelve_raw_marker_and_rejects_selector_marker(
    tmp_path: Path,
) -> None:
    accepted = _run_twelve_fetcher_release_with_marker(
        tmp_path / "accepted", marker_identity="twelve"
    )

    assert accepted.returncode == 0, accepted.stderr
    assert "raw_bucket_mismatch" not in accepted.stderr

    rejected = _run_twelve_fetcher_release_with_marker(
        tmp_path / "rejected", marker_identity="twelve-data"
    )

    assert rejected.returncode != 0
    assert "reason=raw_bucket_mismatch" in rejected.stderr


def test_aws_fetcher_release_removes_residual_candidate_before_failed_preflight(
    tmp_path: Path,
) -> None:
    completed = _run_twelve_fetcher_release_with_marker(
        tmp_path,
        marker_identity="twelve",
        residual_candidate=True,
        preflight_fails=True,
    )

    assert completed.returncode != 0
    state_dir = tmp_path / "state"
    assert not (state_dir / "candidate").exists()
    assert not (state_dir / "stable").exists()
    operations = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert operations.index("rm") < operations.index("pull") < operations.index("run")


def test_aws_fetcher_release_restores_residual_previous_before_failed_preflight(
    tmp_path: Path,
) -> None:
    completed = _run_twelve_fetcher_release_with_marker(
        tmp_path,
        marker_identity="twelve",
        residual_previous=True,
        preflight_fails=True,
    )

    assert completed.returncode != 0
    state_dir = tmp_path / "state"
    assert (state_dir / "stable").read_text(encoding="utf-8").strip() == "running"
    assert not (state_dir / "previous").exists()
    operations = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert operations.index("rename") < operations.index("start") < operations.index("pull")


def test_aws_fetcher_release_rejects_unaccepted_stable_after_hard_interruption(
    tmp_path: Path,
) -> None:
    completed = _run_twelve_fetcher_release_with_marker(
        tmp_path,
        marker_identity="twelve",
        unaccepted_stable=True,
        preflight_fails=True,
    )

    assert completed.returncode != 0
    state_dir = tmp_path / "state"
    assert not (state_dir / "stable").exists()
    operations = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert operations.index("rm") < operations.index("pull")


def test_aws_fetcher_candidate_validates_created_container_without_starting_it(
    tmp_path: Path,
) -> None:
    completed = _run_twelve_fetcher_release_with_marker(
        tmp_path,
        marker_identity="twelve",
        transactional_candidate=True,
    )

    assert completed.returncode == 0, completed.stderr
    state_dir = tmp_path / "state"
    assert not (state_dir / "candidate").exists()
    assert not (state_dir / "stable").exists()
    operations = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert "create" in operations
    assert operations.index("create") < operations.index("rm")


@pytest.mark.parametrize("had_old", (False, True))
def test_fetcher_transaction_tracks_provider_before_interruption(
    tmp_path: Path,
    had_old: bool,
) -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text(
        encoding="utf-8"
    )
    transaction = helper.split("processed=()", 1)[1].split(
        "register_provider findb-fetcher-scheduler ", 1
    )[0]
    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    state_dir.mkdir()
    fake_bin.mkdir()
    if had_old:
        (state_dir / "stable").write_text("running\n", encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)
    harness = f"""set -euo pipefail
FETCHER_DEPLOY_MODE=candidate
processed=(){transaction}
register_provider stable previous
if [ {1 if had_old else 0} -eq 1 ]; then mv {state_dir}/stable {state_dir}/previous; fi
printf 'running\\n' > {state_dir}/stable
kill -TERM "$$"
"""
    environment = dict(os.environ)
    environment.update(
        PATH=f"{fake_bin}:{environment['PATH']}",
        FAKE_DOCKER_STATE=str(state_dir),
        FAKE_DOCKER_LOG=str(tmp_path / "docker.log"),
        FAKE_IMAGE="image:test",
    )

    completed = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 143
    if had_old:
        assert (state_dir / "stable").read_text(encoding="utf-8").strip() == "running"
    else:
        assert not (state_dir / "stable").exists()
    assert not (state_dir / "previous").exists()


def test_fetcher_release_root_mode_and_bounded_candidate_contract_are_exact() -> None:
    coordinator = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text(
        encoding="utf-8"
    )
    provider = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text(
        encoding="utf-8"
    )
    manifest = (REPO_ROOT / "infra/deploy/release_manifest.py").read_text(encoding="utf-8")

    assert "os.mkdir(output.name, 0o700" in manifest
    assert "root:root:700" in coordinator
    assert "root:root:755" not in coordinator
    assert "candidate) accepted_release=false" in provider
    assert 'docker create --name "$candidate"' in provider
    assert "--restart no" in provider
    candidate_branch = provider.split('if [ "$accepted_release" = false ]; then', 1)[1].split(
        "docker run -d", 1
    )[0]
    assert "docker start" not in candidate_branch
    assert "docker run" not in candidate_branch
    assert "candidate_restart" in candidate_branch
    assert "com.findb.fetcher.accepted=$accepted_release" in provider
    assert 'stable_accepted" = false' in provider


def test_fetcher_activation_publishes_atomic_durable_pointer_before_provider_switch() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text(
        encoding="utf-8"
    )
    activation = helper.index('if [ "$FETCHER_DEPLOY_MODE" = activate ]; then')
    committed = helper.index("pointer_committed=1", activation)
    atomic_replace = helper.index("mv -Tf --", committed)
    first_provider = helper.index(
        "register_provider findb-fetcher-scheduler findb-fetcher-scheduler-previous"
    )

    assert activation < committed < atomic_replace < first_provider
    assert "if [ -e /opt/fetcher/current ] && [ ! -L /opt/fetcher/current ]" in helper
    assert "ln -sfn" not in helper
    abort = helper.split("abort_transaction()", 1)[1].split("trap 'abort_transaction", 1)[0]
    assert "previous_pointer" in abort
    assert "mv -Tf --" in abort


@pytest.mark.parametrize("signal_operation", ("rm", "rename", "start"))
def test_fetcher_candidate_cleanup_remains_recoverable_during_each_rollback_operation(
    tmp_path: Path,
    signal_operation: str,
) -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_fetcher_aws.sh").read_text(
        encoding="utf-8"
    )
    transaction = helper.split("processed=()", 1)[1].split(
        "register_provider findb-fetcher-scheduler ", 1
    )[0]
    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    state_dir.mkdir()
    fake_bin.mkdir()
    (state_dir / "stable").write_text("running\n", encoding="utf-8")
    (state_dir / "previous").write_text("stopped\n", encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)
    harness = f"""set -euo pipefail
FETCHER_DEPLOY_MODE=candidate
processed=(){transaction}
processed=(stable:previous:1)
rollback_processed
trap - ERR INT TERM HUP
"""
    environment = dict(os.environ)
    environment.update(
        PATH=f"{fake_bin}:{environment['PATH']}",
        FAKE_DOCKER_STATE=str(state_dir),
        FAKE_DOCKER_LOG=str(tmp_path / "docker.log"),
        FAKE_IMAGE="image:test",
        FAKE_SIGNAL_ON=signal_operation,
        FAKE_SIGNAL_SENT=str(tmp_path / "signal-sent"),
    )

    completed = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 143
    assert (state_dir / "stable").read_text(encoding="utf-8").strip() == "running"
    assert not (state_dir / "previous").exists()


def test_aws_fetcher_release_recovers_on_errors_and_signals_before_deleting_previous() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh").read_text(
        encoding="utf-8"
    )
    assert "trap 'recover \"$?\"' ERR" in helper
    assert "trap 'recover 130' INT" in helper
    assert "trap 'recover 143' TERM" in helper
    assert "trap 'recover 129' HUP" in helper
    assert 'docker rm -f "$stable"' in helper
    final_running_check = helper.rindex("stable_not_running")
    previous_removal = helper.rindex('docker rm "$previous"')
    assert final_running_check < previous_removal


def test_findb_bootstrap_recreates_tmpfs_key_before_docker_and_fails_closed() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/install_findb_bootstrap.sh").read_text(
        encoding="utf-8"
    )
    assert "Before=docker.service" in helper
    assert "Requires=findb-runtime-nginx.service" in helper
    assert "After=findb-runtime-nginx.service" in helper
    assert "network-online.target" in helper
    assert "/run/findb-runtime-secrets/nginx" in helper
    assert "render_nginx_runtime.sh" in helper
    assert "ConditionPathExists" not in helper
    assert "RemainAfterExit" not in helper
    assert "systemd-analyze verify" in helper
    assert "systemctl enable findb-runtime-nginx.service" in helper
    assert "FINDB_LOOKUP_SERVE_API_KEY" not in helper


def test_remote_env_examples_cover_the_sync_contract() -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    service_configs = namespace["SERVICE_CONFIGS"]
    deployment_targets = namespace["DEPLOYMENT_TARGETS"]

    for target in deployment_targets:
        for service, config in service_configs.items():
            example = ENV_CONFIG_ROOT / target / service / "remote.env.example"
            configured_names = {
                match.group(1)
                for line in example.read_text(encoding="utf-8").splitlines()
                if (
                    match := re.fullmatch(
                        r"(?:# )?([A-Z][A-Z0-9_]*)=.*",
                        line,
                    )
                )
            }
            documented_names = set(
                (
                    *config.variables_for(target),
                    *config.secrets_for(target),
                    *(config.optional_secrets if target == "staging" else ()),
                )
            )
            assert configured_names == documented_names


def test_aws_control_plane_variables_are_isolated_and_match_examples() -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    phase1_names = set(namespace["STAGING_AWS_VARIABLES"])
    expected_values = {
        "findb": {
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": "439622209937",
            "AWS_DEPLOY_ROLE_ARN": "arn:aws:iam::439622209937:role/findb-staging-deploy",
            "AWS_INSTANCE_PROFILE_NAME": "findb-staging-instance",
            "AWS_SSM_LOG_GROUP": "/findb/staging/findb/ssm",
            "AWS_DNS_CHECK_NAME": "findb-staging.tingfong.com",
            "ECR_REGISTRY": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com",
        },
        "fetcher": {
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": "439622209937",
            "AWS_DEPLOY_ROLE_ARN": "arn:aws:iam::439622209937:role/fetcher-staging-deploy",
            "AWS_INSTANCE_PROFILE_NAME": "fetcher-staging-instance",
            "AWS_SSM_LOG_GROUP": "/findb/staging/fetcher/ssm",
            "AWS_DNS_CHECK_NAME": "findb-staging.tingfong.com",
            "ECR_REGISTRY": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com",
        },
    }

    for service, config in namespace["SERVICE_CONFIGS"].items():
        assert phase1_names <= set(config.variables_for("staging"))
        assert phase1_names <= set(config.variables_for("production"))

        staging_example = ENV_CONFIG_ROOT / "staging" / service / "remote.env.example"
        staging_values = {
            key: value
            for key, value in (
                line.split("=", 1)
                for line in staging_example.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#") and "=" in line
            )
        }
        assert {name: staging_values[name] for name in phase1_names} == expected_values[service]
        if service == "findb":
            assert staging_values["RDS_DB_INSTANCE_IDENTIFIER"] == (
                "replace-with-staging-rds-instance-identifier"
            )
        else:
            assert "RDS_DB_INSTANCE_IDENTIFIER" not in staging_values

        production_example = ENV_CONFIG_ROOT / "production" / service / "remote.env.example"
        production_text = production_example.read_text(encoding="utf-8")
        assert all(f"{name}=" in production_text for name in phase1_names)
        assert "PRODUCTION_DEPLOY_ENABLED=" not in production_text


def test_fetcher_r2_sync_contract_is_raw_only() -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    fetcher = namespace["SERVICE_CONFIGS"]["fetcher"]

    variables = set(fetcher.variables)

    assert "CLOUDFLARE_R2_RAW_BUCKET" in variables
    assert "CLOUDFLARE_R2_CANONICAL_BUCKET" not in variables


@pytest.mark.parametrize("target", ("staging", "production"))
def test_fetcher_sync_forces_shioaji_simulation(target: str) -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    validate = namespace["_validate_fetcher_simulation_contract"]

    assert validate(target, {"SHIOAJI_SIMULATION": "true"}) is None
    for invalid in ("false", "TRUE", "true ", "1"):
        assert validate(target, {"SHIOAJI_SIMULATION": invalid}) == (
            f"{target}-fetcher must set SHIOAJI_SIMULATION=true"
        )
    assert validate(target, {}) == f"{target}-fetcher must set SHIOAJI_SIMULATION=true"


@pytest.mark.parametrize("target", ("staging", "production"))
def test_findb_sync_rejects_reused_canonical_credentials(target: str) -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    validate = namespace["_validate_findb_canonical_contract"]

    error = validate(
        target,
        {
            "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID": "same-key",
            "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID": "same-key",
        },
    )

    assert error == f"{target}-findb canonical publisher and reader access key IDs must differ"


@pytest.mark.parametrize("target", ("staging", "production"))
def test_fetcher_sync_rejects_canonical_bucket(target: str) -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    validate = namespace["_validate_fetcher_bucket_contract"]

    error = validate(target, {"CLOUDFLARE_R2_CANONICAL_BUCKET": "canonical-bucket"})

    assert error == f"{target}-fetcher must not configure CLOUDFLARE_R2_CANONICAL_BUCKET"


def test_sync_rejects_unallowlisted_r2_names_even_when_empty() -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    unexpected = namespace["_unexpected_r2_names"]

    assert unexpected("fetcher", {"CLOUDFLARE_R2_UNRELATED": "present"}) == (
        "CLOUDFLARE_R2_UNRELATED",
    )
    assert unexpected("findb", {"CLOUDFLARE_R2_RAW_BUCKET": "present"}) == (
        "CLOUDFLARE_R2_RAW_BUCKET",
    )
    assert unexpected("fetcher", {"CLOUDFLARE_R2_RAW_BUCKET": ""}) == ()
    assert unexpected("fetcher", {"CLOUDFLARE_R2_UNRELATED": ""}) == ("CLOUDFLARE_R2_UNRELATED",)


def test_remote_r2_name_audit_reads_names_without_values(monkeypatch: pytest.MonkeyPatch) -> None:
    namespace = runpy.run_path(str(ENV_SYNC_SCRIPT))
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[-1].endswith("/variables?per_page=100"):
            output = '{"variables":[{"name":"CLOUDFLARE_R2_RAW_BUCKET"}]}'
        else:
            output = '{"secrets":[{"name":"CLOUDFLARE_R2_UNRELATED"}]}'
        return subprocess.CompletedProcess(arguments, 0, stdout=output, stderr="")

    monkeypatch.setattr(namespace["subprocess"], "run", fake_run)

    assert namespace["_unexpected_remote_r2_names"]("fetcher", "staging-fetcher") == (
        "CLOUDFLARE_R2_UNRELATED",
    )
    assert len(calls) == 2
    assert all("CLOUDFLARE_R2_" not in " ".join(call) for call in calls)


def test_remote_env_examples_are_explicitly_isolated_by_target() -> None:
    for target in ("staging", "production"):
        for service in ("findb", "fetcher"):
            example = ENV_CONFIG_ROOT / target / service / "remote.env.example"
            first_line = example.read_text(encoding="utf-8").splitlines()[0]
            assert first_line == f"# GitHub Environment: {target}-{service}"

    staging_findb = (ENV_CONFIG_ROOT / "staging" / "findb" / "remote.env.example").read_text(
        encoding="utf-8"
    )
    production_findb = (ENV_CONFIG_ROOT / "production" / "findb" / "remote.env.example").read_text(
        encoding="utf-8"
    )
    assert "\nSERVE_REQUIRE_AUTH=false\n" in staging_findb
    assert "\nSERVE_REQUIRE_AUTH=" not in production_findb
    assert "PRODUCTION_DEPLOY_ENABLED=" not in production_findb


def test_fetcher_ci_retries_transient_container_build_failures() -> None:
    fetcher_ci = _load_workflow(FETCHER_CI_WORKFLOW)
    build_step = _named_step(fetcher_ci, "test", "Build container")
    script = build_step["run"]

    assert "for attempt in 1 2 3; do" in script
    assert 'if docker build -f fetcher/Dockerfile -t "$image" .; then' in script
    assert 'if [ "$attempt" -eq 3 ]; then' in script
    assert 'sleep "$delay"' in script
    assert "Docker build failed after 3 attempts" in script


def test_retired_shared_credential_identifiers_are_absent_from_runtime_contracts() -> None:
    retired = ("SOURCE" + "_API_KEY", "ADMIN" + "_API_KEY")
    paths = (
        REPO_ROOT / "docker-compose.yml",
        PROD_COMPOSE,
        FINDB_CI_WORKFLOW,
        FINDB_CD_WORKFLOW,
        REPO_ROOT / "infra/env/sync_github_environment.py",
        REPO_ROOT / ".env.example",
        REPO_ROOT / "infra/env/staging/findb/remote.env.example",
        REPO_ROOT / "infra/env/production/findb/remote.env.example",
    )
    content = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for identifier in retired:
        assert re.search(rf"(?<![A-Za-z0-9_]){identifier}(?![A-Za-z0-9_])", content) is None


def test_cd_workflows_do_not_reference_cross_service_credentials() -> None:
    findb_cd = FINDB_CD_WORKFLOW.read_text(encoding="utf-8")
    fetcher_cd = FETCHER_CD_WORKFLOW.read_text(encoding="utf-8")

    combined = findb_cd + fetcher_cd
    for forbidden in (
        "FINDB_EC2_",
        "FETCHER_EC2_",
        "appleboy/",
        "ghcr.io",
        "DATABASE_URL",
        "CELERY_BROKER_URL",
        "RABBITMQ_",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_SESSION_SECRET",
    ):
        assert forbidden not in combined

    assert "secrets: inherit" not in findb_cd
    assert "secrets: inherit" not in fetcher_cd


def test_finlab_smoke_checks_out_marker_gate_before_sourcing_helper() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    steps = workflow["jobs"]["finlab-acquisition-smoke"]["steps"]
    smoke_step = _named_step(
        workflow, "finlab-acquisition-smoke", "Run bounded FinLab acquisition smoke over SSM"
    )

    checkout_indices = [
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
    ]
    smoke_index = steps.index(smoke_step)

    assert checkout_indices
    assert min(checkout_indices) < smoke_index
    assert "source infra/deploy/ssm_command_marker_gate.sh" in smoke_step["run"]


def test_findb_candidate_exits_before_live_writer_or_compose_mutation() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    candidate_gate = helper.index('if [ "$deploy_mode" = candidate ]; then')
    candidate_exit = helper.index("exit 0", candidate_gate)
    writer_stop = helper.index("writer_services=(ingest dispatcher worker raw-cleanup)")
    compose_up = helper.index('docker compose -f "$compose_file" up -d --remove-orphans')
    assert candidate_gate < candidate_exit < writer_stop < compose_up
    candidate_render = helper.index('nginx_config_dir="$release_root/rendered-nginx"')
    live_render_guard = helper.index('elif [ "$nginx_config_dir" != /etc/findb/nginx ]')
    assert candidate_render < live_render_guard < candidate_exit

    secret_dispatch = helper.split(
        "# A candidate must validate that its target-scoped nginx secret can load, but", 1
    )[1].split("\n\nrun_runtime --consumer migration", 1)[0]
    candidate_secret_branch, live_render_branch = secret_dispatch.split("else", 1)
    assert '"$nginx_runtime" "$catalog" "$AWS_REGION"' in candidate_secret_branch
    assert '"$AWS_ACCOUNT_ID" validate' in candidate_secret_branch
    assert "candidate_nginx_runtime_validated" in candidate_secret_branch
    assert "/run/findb-runtime-secrets/nginx/serve-key.conf" not in candidate_secret_branch
    assert '"$nginx_runtime" "$catalog" "$AWS_REGION" "$FINDB_PUBLIC_HOST"' in live_render_branch


def test_fetcher_ci_builds_and_inspects_three_isolated_provider_images() -> None:
    workflow = _load_workflow(FETCHER_CI_WORKFLOW)
    test_job = workflow["jobs"]["test"]
    build = _named_step(workflow, "test", "Build container")["run"]
    finlab = _named_step(workflow, "test", "Build and inspect isolated FinLab scheduler container")[
        "run"
    ]
    shioaji = _named_step(
        workflow, "test", "Build and inspect isolated Shioaji scheduler container"
    )["run"]

    assert 'docker build -f fetcher/Dockerfile -t "$image" .' in build
    assert "findb-fetch-scheduler --help" in build
    assert "exit 0" not in build
    assert 'docker build -f fetcher/Dockerfile.finlab -t "$finlab_image" .' in finlab
    assert 'find_spec("finlab") is not None' in finlab
    assert 'find_spec("shioaji") is None' in finlab
    assert "findb-fetch-finlab-scheduler --help" in finlab
    assert 'docker build -f fetcher/Dockerfile.shioaji -t "$shioaji_image" .' in shioaji
    assert 'find_spec("shioaji") is not None' in shioaji
    assert "findb-fetch-shioaji-scheduler --help" in shioaji
    assert "Dockerfile.shioaji" in shioaji
    shioaji_dockerfile = (REPO_ROOT / "fetcher" / "Dockerfile.shioaji").read_text(encoding="utf-8")
    assert "--create-home --home-dir /home/fetcher" in shioaji_dockerfile
    assert "HOME=/home/fetcher" in shioaji_dockerfile
    assert 'VOLUME ["/home/fetcher", "/var/lib/findb-shioaji-fetcher"]' in (shioaji_dockerfile)
    assert str(test_job["timeout-minutes"]) == "15"


def _fetcher_scheduler_cases() -> tuple[tuple[str, str, str, str, str, str], ...]:
    """Return provider-specific function arguments for the shell state machine."""
    return (
        (
            "twelve",
            "Release and validate Twelve Data scheduler on Fetcher EC2",
            "findb-fetch-scheduler",
            "findb-fetcher-scheduler",
            "findb-fetcher-scheduler-candidate",
            "findb-fetcher-scheduler-previous",
        ),
        (
            "finlab",
            "Release and validate FinLab scheduler on Fetcher EC2",
            "findb-fetch-finlab-scheduler --schedule-file /app/configs/daily_scheduler.v2.json --slot-id taiwan_market_window --dataset-key tw_equity_eod",
            "findb-fetcher-finlab-scheduler",
            "findb-fetcher-finlab-scheduler-candidate",
            "findb-fetcher-finlab-scheduler-previous",
        ),
        (
            "shioaji",
            "Release and validate Shioaji scheduler on Fetcher EC2",
            "findb-fetch-shioaji-scheduler --manifest /app/configs/shioaji_tw_pilot.v1.json",
            "findb-fetcher-shioaji-scheduler",
            "findb-fetcher-shioaji-scheduler-candidate",
            "findb-fetcher-shioaji-scheduler-previous",
        ),
    )


_FAKE_DOCKER = """#!/bin/sh
set -eu
state_dir="${FAKE_DOCKER_STATE:?}"
printf '%s\\n' "$1" >> "${FAKE_DOCKER_LOG:?}"
operation="$1"
shift
if [ -n "${FAKE_SIGNAL_ON:-}" ] && [ "$operation" = "$FAKE_SIGNAL_ON" ] \
  && [ ! -e "${FAKE_SIGNAL_SENT:?}" ]; then
  : > "$FAKE_SIGNAL_SENT"
  kill -TERM "$PPID"
fi
case "$operation" in
  container)
    [ "$1" = inspect ]
    [ -f "$state_dir/$2" ]
    ;;
  image)
    [ "$1" = prune ]
    [ "$2" = -af ]
    ;;
  pull)
    exit 0
    ;;
  rm)
    [ "$1" != "--name" ] || shift
    [ "$1" != "-f" ] || shift
    rm -f "$state_dir/$1"
    ;;
  rename)
    mv "$state_dir/$1" "$state_dir/$2"
    ;;
  stop)
    [ "$1" = "--time" ]
    name="$3"
    [ "$(cat "$state_dir/$name")" != stop-fail ] || exit 1
    printf 'stopped\\n' > "$state_dir/$name"
    ;;
  start)
    name="$1"
    [ "$(cat "$state_dir/$name")" != start-fail ] || exit 1
    printf 'running\\n' > "$state_dir/$name"
    ;;
  run)
    detached=0
    name=""
    previous=""
    for argument in "$@"; do
      if [ "$argument" = -d ]; then detached=1; fi
      if [ "$previous" = --name ]; then name="$argument"; fi
      previous="$argument"
    done
    if [ "$detached" -eq 1 ]; then
      [ -n "$name" ]
      printf '%s\\n' "${FAKE_CANDIDATE_STATUS:-running}" > "$state_dir/$name"
    else
      [ "${FAKE_PREFLIGHT_FAIL:-0}" != 1 ]
    fi
    ;;
  create)
    name=""
    previous=""
    for argument in "$@"; do
      if [ "$previous" = --name ]; then name="$argument"; fi
      previous="$argument"
    done
    [ -n "$name" ]
    printf 'created\\n' > "$state_dir/$name"
    ;;
  inspect)
    [ "$1" = --format ]
    format="$2"
    name="$3"
    status="$(cat "$state_dir/$name")"
    case "$format" in
      *Config.Labels*)
        case ",${FAKE_UNACCEPTED_NAMES:-}," in
          *",$name,"*) printf 'false\\n' ;;
          *) printf '<no value>\\n' ;;
        esac
        ;;
      *Config.Image*) printf '%s\\n' "${FAKE_IMAGE:?}" ;;
      *Config.User*) printf '10001:10001\\n' ;;
      *HostConfig.RestartPolicy.Name*) printf 'no\\n' ;;
      *State.Running*) [ "$status" = running ] && printf 'true\\n' || printf 'false\\n' ;;
      *State.Status*) printf '%s\\n' "$status" ;;
      *State.ExitCode*) printf '%s\\n' "${FAKE_STABLE_EXIT_CODE:-0}" ;;
      *RestartCount*) printf '0\\n' ;;
      *) printf '%s\\n' "$status" ;;
    esac
    ;;
  *)
    exit 2
    ;;
esac
"""

_FAKE_SUDO = """#!/bin/sh
set -eu
case "$1" in
  chown|chmod)
    exit 0
    ;;
  stat)
    path="${4:?}"
    case "$path" in
      *.sqlite3|*.sha256|*.sha256.*) printf '10001:10001:600\\n' ;;
      *) printf '10001:10001:700\\n' ;;
    esac
    ;;
  *)
    exec "$@"
    ;;
esac
"""


def _run_fetcher_reconciliation(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
    *,
    initial: dict[str, str],
    candidate_status: str = "running",
    preflight_fails: bool = False,
    wrong_raw_bucket_marker: bool = False,
    stable_exit_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[str], str]:
    provider_id, step_name, command, stable, candidate, previous = provider
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    script = _named_step(workflow, "deploy", step_name)["with"]["script"]
    function_start = script.index("reconcile_scheduler() {")
    function_end = script.index("\nruntime_env_args=", function_start)
    function = script[function_start:function_end]

    state_dir = tmp_path / provider_id / "state"
    fake_bin = tmp_path / provider_id / "bin"
    state_dir.mkdir(parents=True)
    fake_bin.mkdir(parents=True)
    for name, status in initial.items():
        (state_dir / name).write_text(status + "\n", encoding="utf-8")
    state_path = state_dir / "state.sqlite3"
    state_path.write_text("durable-state\n", encoding="utf-8")
    marker_components = "0123456789abcdef0123456789abcdef\nraw-bucket"
    if not wrong_raw_bucket_marker:
        marker_components += f"\n{provider_id}"
    (state_dir / "raw-bucket.sha256").write_text(
        hashlib.sha256(marker_components.encode()).hexdigest() + "\n",
        encoding="utf-8",
    )
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(_FAKE_SUDO, encoding="utf-8")
    fake_sudo.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)

    image = "image:test"
    if provider_id == "shioaji":
        invocation = (
            f"reconcile_scheduler {provider_id} {image} "
            f"{state_dir} {state_path} {stable} {candidate} {previous} "
            f'preflight "" {command}'
        )
    else:
        cache_dir = state_dir / "cache" if provider_id == "finlab" else "-"
        invocation = (
            f"reconcile_scheduler {provider_id} {image} "
            f"{state_dir} {state_path} {stable} {candidate} {previous} "
            f'preflight {cache_dir} "" {command}'
        )
    harness = f"""set -euo pipefail
SOURCE_API_URL=https://findb.example.com
SOURCE_CLIENT_KEY=source-key
FINDB_SERVE_BASE_URL=https://findb.example.com
FETCHER_CALENDAR_SERVE_API_KEY=calendar-key
CLOUDFLARE_R2_ACCOUNT_ID=0123456789abcdef0123456789abcdef
CLOUDFLARE_R2_RAW_BUCKET=raw-bucket
FETCHER_STATE_PATH={state_path}
FETCHER_SHIOAJI_STATE_PATH={state_path}
SHIOAJI_SIMULATION=true
export SOURCE_API_URL SOURCE_CLIENT_KEY FINDB_SERVE_BASE_URL FETCHER_CALENDAR_SERVE_API_KEY
export CLOUDFLARE_R2_ACCOUNT_ID CLOUDFLARE_R2_RAW_BUCKET FETCHER_STATE_PATH FETCHER_SHIOAJI_STATE_PATH SHIOAJI_SIMULATION
stable={stable}
candidate={candidate}
previous={previous}
{function}
{invocation}
"""
    log_path = tmp_path / provider_id / "docker.log"
    environment = dict(os.environ)
    environment.update(
        PATH=f"{fake_bin}:{environment['PATH']}",
        FAKE_DOCKER_STATE=str(state_dir),
        FAKE_DOCKER_LOG=str(log_path),
        FAKE_IMAGE=image,
        FAKE_CANDIDATE_STATUS=candidate_status,
        FAKE_PREFLIGHT_FAIL="1" if preflight_fails else "0",
        FAKE_STABLE_EXIT_CODE=str(stable_exit_code),
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    statuses = {
        path.name: path.read_text(encoding="utf-8").strip()
        for path in state_dir.iterdir()
        if path.is_file() and path.name not in {"state.sqlite3", "raw-bucket.sha256"}
    }
    operations = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return completed, statuses, operations, state_path.read_text(encoding="utf-8")


def test_ec2_setup_instructions_match_findb_environment_boundary() -> None:
    setup_script = (BACKEND_ROOT / "scripts" / "setup_ec2.sh").read_text(encoding="utf-8")

    assert "staging-findb" in setup_script
    assert "production-findb" in setup_script
    assert "FINDB_EC2_HOST" not in setup_script
    assert "FINDB_EC2_USER" not in setup_script
    assert "FINDB_EC2_SSH_KEY" not in setup_script
    assert "findb/production/findb/" in setup_script
    assert "\n       EC2_HOST" not in setup_script
    assert "staging-fetcher" in setup_script
    assert "production-fetcher" in setup_script


def test_connection_headroom_excludes_reserved_slots_and_all_clients() -> None:
    assert DEFAULT_MINIMUM_CONNECTION_HEADROOM == 10
    assert (
        calculate_connection_headroom(
            max_connections=80,
            current_connections=13,
            reserved_connection_slots=3,
        )
        == 64
    )
    assert (
        calculate_connection_headroom(
            max_connections=10,
            current_connections=9,
            reserved_connection_slots=3,
        )
        == 0
    )


def test_predeploy_database_state_accepts_safe_capacity() -> None:
    state = {
        "duplicate_raw_run_ids": 0,
        "long_transactions_over_5m": 0,
        "connection_headroom": 120,
        "postgresql_tls_in_use": True,
    }

    assert validate_predeploy_state(state, minimum_connection_headroom=80) == []


def test_predeploy_database_state_reports_every_blocker() -> None:
    state = {
        "duplicate_raw_run_ids": 2,
        "long_transactions_over_5m": 1,
        "connection_headroom": 30,
        "postgresql_tls_in_use": True,
    }

    errors = validate_predeploy_state(state, minimum_connection_headroom=80)

    assert len(errors) == 3
    assert any("duplicate" in error for error in errors)
    assert any("older than five minutes" in error for error in errors)
    assert any("connection headroom" in error for error in errors)


def test_predeploy_database_state_blocks_wave_four_contract_drift() -> None:
    state = {
        "duplicate_raw_run_ids": 0,
        "long_transactions_over_5m": 0,
        "connection_headroom": 120,
        "postgresql_tls_in_use": True,
        "noncanonical_scheduler_control_slots": 1,
        "noncanonical_dataset_delivery_schedule_slots": 2,
        "legacy_finlab_scheduler_keys": 1,
        "dataset_keys_projection_mismatches": 3,
    }

    errors = validate_predeploy_state(state, minimum_connection_headroom=80)

    assert len(errors) == 4
    assert any("scheduler_control" in error for error in errors)
    assert any("delivery schedules" in error for error in errors)
    assert any("legacy FinLab" in error for error in errors)
    assert any("projection" in error for error in errors)


@pytest.mark.parametrize(
    ("database_url", "expected"),
    (
        ("postgresql+asyncpg://user:password@db.example/findb?ssl=require", True),
        ("postgresql+asyncpg://user:password@db.example/findb?sslmode=verify-full", True),
        ("postgresql+asyncpg://user:password@db.example/findb?sslmode=disable", False),
        ("postgresql+asyncpg://user:password@db.example/findb", False),
    ),
)
def test_predeploy_database_url_requires_explicit_tls(database_url: str, expected: bool) -> None:
    assert database_url_requires_tls(database_url) is expected


@pytest.mark.parametrize(
    ("database_url", "expected_endpoint", "expected"),
    (
        (
            "postgresql+asyncpg://user:password@db-staging.example.com/findb?ssl=require",
            "DB-STAGING.EXAMPLE.COM",
            True,
        ),
        (
            "postgresql+asyncpg://user:password@other.example.com/findb?ssl=require",
            "db-staging.example.com",
            False,
        ),
        ("postgresql+asyncpg://user:password@/findb?ssl=require", "db.example.com", False),
    ),
)
def test_predeploy_database_url_matches_expected_rds_endpoint(
    database_url: str, expected_endpoint: str, expected: bool
) -> None:
    assert database_url_matches_expected_host(database_url, expected_endpoint) is expected


def test_predeploy_schema_compatibility_allows_current_or_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Revision:
        def __init__(self, revision: str) -> None:
            self.revision = revision

    class Scripts:
        def iterate_revisions(self, target: str, _base: str) -> list[Revision]:
            assert target == "target"
            return [Revision("target"), Revision("current"), Revision("base")]

    monkeypatch.setattr(predeploy_db_check, "Config", lambda _path: object())
    monkeypatch.setattr(
        predeploy_db_check.ScriptDirectory, "from_config", lambda _config: Scripts()
    )
    assert is_schema_compatible_with_target(current_revision="target", target_revision="target")
    assert is_schema_compatible_with_target(current_revision="current", target_revision="target")


def test_predeploy_schema_compatibility_rejects_newer_or_unknown_database_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Revision:
        def __init__(self, revision: str) -> None:
            self.revision = revision

    class Scripts:
        def iterate_revisions(self, target: str, _base: str) -> list[Revision]:
            if target == "unknown":
                raise ValueError("unknown revision")
            return [Revision("older-target"), Revision("base")]

    monkeypatch.setattr(predeploy_db_check, "Config", lambda _path: object())
    monkeypatch.setattr(
        predeploy_db_check.ScriptDirectory, "from_config", lambda _config: Scripts()
    )
    assert not is_schema_compatible_with_target(
        current_revision="newer-database", target_revision="older-target"
    )
    assert not is_schema_compatible_with_target(
        current_revision="current", target_revision="unknown"
    )


def test_activation_requires_database_at_the_exact_selected_revision() -> None:
    assert database_revision_matches_expected(current_revision="target", expected_revision="target")
    assert not database_revision_matches_expected(
        current_revision="ancestor", expected_revision="target"
    )
    assert not database_revision_matches_expected(current_revision=None, expected_revision="target")
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    assert "--require-exact-alembic-revision" in helper
    migration_block = helper.split("<<'MIGRATION_SCRIPT'", 1)[1].split("MIGRATION_SCRIPT", 1)[0]
    assert "alembic upgrade head" in migration_block
    assert helper.count("alembic upgrade head") == 1


def test_predeploy_database_state_rejects_missing_or_unnegotiated_tls() -> None:
    state = {
        "duplicate_raw_run_ids": 0,
        "long_transactions_over_5m": 0,
        "connection_headroom": 120,
        "postgresql_tls_in_use": False,
    }
    errors = validate_predeploy_state(state, minimum_connection_headroom=80)
    assert errors == ["database connection did not negotiate PostgreSQL TLS"]
    state.pop("postgresql_tls_in_use")
    assert validate_predeploy_state(state, minimum_connection_headroom=80) == [
        "database connection did not negotiate PostgreSQL TLS"
    ]


def test_predeploy_empty_database_bootstrap_requires_explicit_allowance() -> None:
    state = {
        "database_empty": True,
        "user_relation_count": 0,
        "alembic_revision": None,
        "duplicate_raw_run_ids": 0,
        "long_transactions_over_5m": 0,
        "connection_headroom": 120,
        "postgresql_tls_in_use": True,
    }

    assert validate_predeploy_state(state, minimum_connection_headroom=80) == [
        "empty database bootstrap is not allowed"
    ]
    assert (
        validate_predeploy_state(
            state,
            minimum_connection_headroom=80,
            allow_empty_database_bootstrap=True,
        )
        == []
    )


def test_predeploy_unversioned_nonempty_database_fails_with_bootstrap_allowance() -> None:
    state = {
        "database_empty": False,
        "user_relation_count": 1,
        "alembic_revision": None,
        "duplicate_raw_run_ids": 0,
        "long_transactions_over_5m": 0,
        "connection_headroom": 120,
        "postgresql_tls_in_use": True,
    }

    assert validate_predeploy_state(
        state,
        minimum_connection_headroom=80,
        allow_empty_database_bootstrap=True,
    ) == ["database contains user relations without an Alembic revision"]


def test_empty_database_bootstrap_flag_is_production_only() -> None:
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    migration_check = helper.split("<<'MIGRATION_CHECK_SCRIPT'\n", 1)[1].split(
        "\nMIGRATION_CHECK_SCRIPT", 1
    )[0]

    assert 'if [ "$DEPLOYMENT_TARGET" = production ]; then' in migration_check
    assert "bootstrap_arg=--allow-empty-database-bootstrap" in migration_check
    assert 'if [ "$DEPLOYMENT_TARGET" = staging ]' not in migration_check


def test_staging_predeploy_preserves_release_context_through_runtime_wrapper(
    tmp_path: Path,
) -> None:
    """An incompatible target fails before writer-stop, migration, or candidate cleanup."""
    helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    preserve = (
        "preserve_env=" + helper.split("preserve_env=", 1)[1].split("\n\nrun_runtime()", 1)[0]
    )
    runtime_body = helper.split("run_runtime() {\n", 1)[1].split("\n}\n\nif [ ! -f", 1)[0]
    migration_check = helper.split("<<'MIGRATION_CHECK_SCRIPT'\n", 1)[1].split(
        "\nMIGRATION_CHECK_SCRIPT", 1
    )[0]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    (fake_bin / "sudo").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\npreserved=""\nif [[ "$1" == --preserve-env=* ]]; then preserved="${1#--preserve-env=}"; shift; fi\nargs=("PATH=$PATH")\nIFS=, read -r -a names <<< "$preserved"\nfor name in "${names[@]}"; do args+=("$name=${!name-}"); done\nexec env -i "${args[@]}" "$@"\n',
        encoding="utf-8",
    )
    (fake_bin / "runtime-secret-command").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nwhile [[ "$#" -gt 0 && "$1" != -- ]]; do shift; done\nshift\nexec "$@"\n',
        encoding="utf-8",
    )
    (fake_bin / "docker").write_text(
        f'#!/usr/bin/env bash\nset -euo pipefail\nprintf \'%s\\n\' "$*" >> {command_log!s}\ncase "$*" in *predeploy_db_check.py*) exit 42 ;; esac\nexit 0\n',
        encoding="utf-8",
    )
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    script = "\n".join(
        (
            "set -euo pipefail",
            f'runtime_command="{fake_bin / "runtime-secret-command"}"',
            "catalog=/opt/findb/releases/test/infra/deploy/runtime-secrets/findb.json",
            "AWS_REGION=ap-southeast-1",
            "AWS_ACCOUNT_ID=439622209937",
            "DEPLOYMENT_TARGET=staging",
            "FINDB_RELEASE_ROOT=/opt/findb/releases/test",
            "PREDEPLOY_EXPECTED_ALEMBIC_REVISION=incompatible-target",
            "PREDEPLOY_EXPECTED_RDS_ENDPOINT=db.example.com",
            "compose_file=/tmp/compose.yml",
            "export AWS_REGION AWS_ACCOUNT_ID DEPLOYMENT_TARGET FINDB_RELEASE_ROOT PREDEPLOY_EXPECTED_ALEMBIC_REVISION PREDEPLOY_EXPECTED_RDS_ENDPOINT",
            preserve,
            "run_runtime() {",
            runtime_body,
            "}",
            "run_runtime --consumer migration --consumer compose --map MIGRATION_DATABASE_URL=DATABASE_URL -- bash -s -- \"$compose_file\" <<'MIGRATION_CHECK_SCRIPT'",
            migration_check,
            "MIGRATION_CHECK_SCRIPT",
        )
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert completed.returncode == 42
    commands = command_log.read_text(encoding="utf-8")
    assert "predeploy_db_check.py --expected-alembic-revision incompatible-target" in commands
    assert "--expected-rds-endpoint db.example.com" in commands
    assert " stop --timeout " not in commands
    assert "alembic upgrade" not in commands
    cleanup_arm = helper.index("release_services_may_have_started=1")
    migration_end = helper.index("MIGRATION_SCRIPT\nfi", helper.index("MIGRATION_SCRIPT"))
    candidate_up = helper.index("<<'UP_SCRIPT'", migration_end)
    assert migration_end < cleanup_arm < candidate_up


def test_queue_health_requires_recent_worker_and_no_expired_leases() -> None:
    assert (
        validate_queue_health(
            {
                "worker_heartbeat_age_seconds": 12.5,
                "expired_leases": 0,
                "dlq_ready": 0,
                "dlq_unacked": 0,
                "dlq_depth": 0,
            },
            maximum_heartbeat_age=90,
        )
        == []
    )

    errors = validate_queue_health(
        {
            "worker_heartbeat_age_seconds": None,
            "expired_leases": 3,
            "dlq_ready": 0,
            "dlq_unacked": 0,
            "dlq_depth": 0,
        },
        maximum_heartbeat_age=90,
    )
    assert errors == [
        "worker heartbeat has not been recorded",
        "3 normalization execution leases are expired",
    ]


def test_queue_health_fails_when_dlq_is_not_empty() -> None:
    payload = {
        "worker_heartbeat_age_seconds": 12.5,
        "expired_leases": 0,
        "dlq_ready": 9,
        "dlq_unacked": 1,
        "dlq_depth": 10,
    }
    assert validate_queue_health(payload, maximum_heartbeat_age=90) == []

    errors = validate_queue_health(
        payload,
        maximum_heartbeat_age=90,
        require_empty_dlq=True,
    )

    assert errors == ["normalization DLQ is not empty (depth=10, ready=9, unacked=1)"]


def test_deployment_readiness_does_not_enable_mass_import_dlq_gate() -> None:
    workflow = FINDB_CD_WORKFLOW.read_text()

    assert "--require-empty-dlq" not in workflow


def test_queue_readiness_fails_closed_when_dlq_query_fails() -> None:
    def failed_dlq_query(*args, **kwargs):
        raise ConnectionError("management API unavailable")

    with pytest.raises(ConnectionError, match="management API unavailable"):
        fetch_readiness_payload(
            "http://127.0.0.1:8080/api/v1/admin/queue/health",
            api_key_header="X-API-Key",
            api_key="test-admin-key",
            management_url="http://rabbitmq:15672",
            broker_url="amqp://user:password@rabbitmq:5672/%2Ffindb",
            vhost="/findb",
            queue_name="findb.normalize.dlq.v1",
            queue_health_fetcher=lambda *args, **kwargs: {
                "worker_heartbeat_age_seconds": 12.5,
                "expired_leases": 0,
            },
            dlq_health_fetcher=failed_dlq_query,
        )


def test_fetch_dlq_health_encodes_queue_path_and_uses_broker_credentials() -> None:
    captured = {}

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def open_management(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response(b'{"messages_ready": 0, "messages_unacknowledged": 0, "messages": 0}')

    result = fetch_dlq_health(
        "http://rabbitmq:15672",
        broker_url="amqp://service:p%40ss@rabbitmq:5672/%2Ffindb",
        vhost="/findb",
        queue_name="findb.normalize.dlq.v1",
        urlopen_func=open_management,
    )

    assert result == {"dlq_ready": 0, "dlq_unacked": 0, "dlq_depth": 0}
    assert captured == {
        "url": ("http://rabbitmq:15672/api/queues/%2Ffindb/findb.normalize.dlq.v1"),
        "authorization": "Basic c2VydmljZTpwQHNz",
        "timeout": 10,
    }


def test_fetch_dlq_health_rejects_missing_or_non_integer_counters() -> None:
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    with pytest.raises(ValueError, match="messages_unacknowledged"):
        fetch_dlq_health(
            "http://rabbitmq:15672",
            broker_url="amqp://service:password@rabbitmq:5672/%2Ffindb",
            vhost="/findb",
            queue_name="findb.normalize.dlq.v1",
            urlopen_func=lambda *args, **kwargs: Response(
                b'{"messages_ready": 0, "messages_unacknowledged": false, "messages": 0}'
            ),
        )
