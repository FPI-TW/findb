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

from scripts.check_queue_health import (
    fetch_dlq_health,
    fetch_readiness_payload,
    validate_queue_health,
)
from scripts.predeploy_db_check import (
    DEFAULT_MINIMUM_CONNECTION_HEADROOM,
    calculate_connection_headroom,
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
INGESTION_RUNBOOK = REPO_ROOT / "docs" / "operations" / "ingestion.md"
ENV_CONFIG_ROOT = REPO_ROOT / "infra" / "env"
ENV_SYNC_SCRIPT = ENV_CONFIG_ROOT / "sync_github_environment.py"
PLAN_JSON_GUARD = REPO_ROOT / "infra" / "tofu" / "plan_json_guard.py"

LEGACY_ROLLBACK_CI_ONLY_PATHS = frozenset(
    {
        "infra/deploy/runtime-secrets/build_ecr_image_if_missing.sh",
        "infra/deploy/release_manifest.py",
        ".github/workflows/findb-cd.yml",
        ".github/workflows/fetcher-cd.yml",
        "docs/operations/deployment.md",
        "docs/dev/staging-aws-deployment-plan.md",
        "backend/tests/test_deployment_checks.py",
        "backend/tests/test_release_manifest.py",
    }
)


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
    workflow = _load_workflow(workflow_path)
    unit = "FinDB" if workflow_path == FINDB_CD_WORKFLOW else "Fetcher"
    synced = _named_step(
        workflow,
        "deploy",
        f"Sync AWS runtime-secret loader and exact {unit} catalog",
    )["with"]["source"]
    paths = set(synced.split(","))
    if workflow_path == FINDB_CD_WORKFLOW:
        renderer = _named_step(
            workflow,
            "deploy",
            "Render nonsecret nginx configs for AWS runtime secrets",
        )["run"]
        rendered_scripts = set(
            re.findall(r"python backend/scripts/(render_nginx_[a-z_]+\.py)", renderer)
        )
        assert rendered_scripts == {
            "render_nginx_public_host.py",
            "render_nginx_source_allowlist.py",
            "render_nginx_cloudflare_real_ip.py",
        }
        assert "render_nginx_serve_key.py" not in renderer
        paths.update(f"backend/scripts/{script}" for script in rendered_scripts)
    return paths


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
        "fetcher-ci.yml",
        "fetcher-cd.yml",
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


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    for path in (*WORKFLOWS_ROOT.glob("*.yml"), *WORKFLOWS_ROOT.glob("*.yaml")):
        for reference in _uses_references(_load_workflow(path)):
            if reference.startswith("./"):
                continue
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference), (
                f"{path.name} contains an unpinned action reference: {reference}"
            )


def test_cd_workflows_verify_the_same_commit_before_deployment() -> None:
    findb_cd = _load_workflow(FINDB_CD_WORKFLOW)
    fetcher_cd = _load_workflow(FETCHER_CD_WORKFLOW)

    for workflow, ci_path, service in (
        (findb_cd, "./.github/workflows/findb-ci.yml", "findb"),
        (fetcher_cd, "./.github/workflows/fetcher-ci.yml", "fetcher"),
    ):
        environment = f"${{{{ inputs.deployment_target || 'staging' }}}}-{service}"
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        assert jobs["verify"]["uses"] == ci_path
        if service == "fetcher":
            assert jobs["build-push-production"]["needs"] == [
                "verify",
                "validate-target-routing",
            ]
            assert jobs["deploy"]["needs"] == [
                "build-push",
                "validate-target-routing",
                "validate-r2-bucket-configuration",
                "validate-fetcher-credential-isolation",
                "validate-fetcher-credential-isolation-aws",
            ]
        else:
            assert jobs["build-push-production"]["needs"] == "verify"
            assert jobs["deploy"]["needs"] == "build-push"
            assert jobs["deploy"]["if"] == (
                "always() && github.event_name == 'workflow_dispatch' "
                "&& needs.build-push.result == 'success'"
            )
        assert jobs["deploy"]["environment"] == environment
        assert workflow["concurrency"]["group"] == environment
        assert workflow["concurrency"]["cancel-in-progress"] == "false"
        target_input = workflow["on"]["workflow_dispatch"]["inputs"]["deployment_target"]
        assert target_input["default"] == "staging"
        assert target_input["options"] == ["staging", "production"]
        assert "secret_source" not in workflow["on"]["workflow_dispatch"]["inputs"]
        rollback_input = workflow["on"]["workflow_dispatch"]["inputs"]["image_tag"]
        assert rollback_input["default"] == ""
        assert (
            workflow["env"]["STAGING_ECR_REGISTRY"]
            == "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
        )
        assert workflow["jobs"]["build-push-production"]["permissions"] == {
            "contents": "read",
            "packages": "write",
        }
        expected_bridge_needs = (
            ["verify"]
            if service == "findb"
            else [
                "verify",
                "validate-target-routing",
            ]
        )
        expected_bridge_needs.extend(
            ["build-push-production", "build-push-staging-ecr", "staging-ecr-cutover-disabled"]
        )
        assert workflow["jobs"]["build-push"]["needs"] == expected_bridge_needs
        assert workflow["jobs"]["build-push-staging-ecr"]["permissions"] == {
            "contents": "read",
            "id-token": "write",
        }


def test_runtime_secret_cutover_is_target_derived_and_staging_ecr_is_gated() -> None:
    for path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        workflow = _load_workflow(path)
        assert "secret_source" not in workflow["on"]["workflow_dispatch"]["inputs"]
        staged = workflow["jobs"]["build-push-staging-ecr"]
        assert "github.event_name == 'workflow_dispatch'" in staged["if"]
        selection = _named_step(
            workflow, "build-push-staging-ecr", "Validate staging ECR release selection"
        )
        assert "git merge-base --is-ancestor" in selection["run"]
        assert "reachable from protected main" in selection["run"]
        assert 'git diff --quiet "$ECR_IMAGE_TAG" HEAD' in selection["run"]
        assert "Incompatible staging rollback" in selection["run"]
        assert _legacy_rollback_runtime_paths(path) == _host_runtime_contract_paths(path)
        assert "rollback_contract_files" not in selection["run"]
        credential_index = next(
            index
            for index, step in enumerate(staged["steps"])
            if step.get("uses", "").startswith("aws-actions/configure-aws-credentials")
        )
        assert staged["steps"].index(selection) < credential_index
        assert selection["run"]
        assert staged["steps"][0]["with"]["fetch-depth"] == "0"
        assert "vars.STAGING_ECR_CUTOVER_ENABLED == 'true'" in staged["if"]
        assert "packages" not in staged["permissions"]
        assert "ghcr.io" not in json.dumps(staged, sort_keys=True)
        disabled = workflow["jobs"]["staging-ecr-cutover-disabled"]
        assert "github.event_name == 'workflow_dispatch'" in disabled["if"]
        assert "vars.STAGING_ECR_CUTOVER_ENABLED != 'true'" in disabled["if"]
        bridge = workflow["jobs"]["build-push"]
        assert bridge["if"] == "always()"
        assert "STAGING_ECR_BUILD" in bridge["steps"][0]["env"]
        assert "PRODUCTION_BUILD" in bridge["steps"][0]["env"]
        bridge_script = bridge["steps"][0]["run"]
        assert 'if [ "$EVENT_NAME" = workflow_dispatch ]; then' in bridge_script
        assert "image_tag is available only for staging ECR rollback" in bridge_script
        assert "github.event_name == 'workflow_dispatch'" in workflow["jobs"]["deploy"]["if"]


def test_staging_ecr_build_bridge_reaches_existing_aws_host_rollout_without_ghcr_credentials() -> (
    None
):
    ecr_registry = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
    for workflow_path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        workflow = _load_workflow(workflow_path)
        jobs = workflow["jobs"]
        deploy = jobs["deploy"]
        assert deploy["needs"] == "build-push" or "build-push" in deploy["needs"]
        assert "needs.build-push.result == 'success'" in deploy["if"]
        staging_steps = [
            step
            for step in deploy["steps"]
            if "STAGING_ECR_CUTOVER_ENABLED == 'true'" in str(step.get("if", ""))
        ]
        assert staging_steps
        assert all(
            token not in json.dumps(staging_steps, sort_keys=True)
            for token in ("GITHUB_TOKEN", "GITHUB_ACTOR", "GHCR_USERNAME", "GHCR_TOKEN", "ghcr.io")
        )

    findb = _load_workflow(FINDB_CD_WORKFLOW)
    findb_rollout = _named_step(findb, "deploy", "Deploy to EC2 with AWS runtime secrets")
    assert findb_rollout["env"]["ECR_REGISTRY"] == ecr_registry
    assert findb_rollout["env"]["FINDB_IMAGE"] == f"{ecr_registry}/findb/staging/backend"
    assert findb_rollout["env"]["DASHBOARD_IMAGE"] == f"{ecr_registry}/findb/staging/dashboard"
    assert findb_rollout["env"]["IMAGE_TAG"] == "${{ inputs.image_tag || github.sha }}"
    assert "ECR_REGISTRY,FINDB_IMAGE,DASHBOARD_IMAGE" in findb_rollout["with"]["envs"]

    fetcher = _load_workflow(FETCHER_CD_WORKFLOW)
    for name, image in (
        ("Release Twelve Data scheduler with AWS runtime secrets", "fetcher/twelve-data"),
        ("Release FinLab scheduler with AWS runtime secrets", "fetcher/finlab"),
        ("Release Shioaji scheduler with AWS runtime secrets", "fetcher/shioaji"),
    ):
        step = _named_step(fetcher, "deploy", name)
        assert step["env"]["ECR_REGISTRY"] == ecr_registry
        assert (
            step["env"].get(
                "FETCHER_IMAGE",
                step["env"].get("FETCHER_FINLAB_IMAGE", step["env"].get("FETCHER_SHIOAJI_IMAGE")),
            )
            == f"{ecr_registry}/findb/staging/{image}"
        )
        assert "ECR_REGISTRY" in step["with"]["envs"]
        assert '--ecr-registry "$ECR_REGISTRY" --docker-login' in step["with"]["script"]

    smoke = _named_step(
        fetcher,
        "finlab-acquisition-smoke",
        "Run bounded FinLab acquisition smoke with AWS runtime secrets",
    )
    assert smoke["env"]["ECR_REGISTRY"] == ecr_registry
    assert smoke["env"]["FETCHER_FINLAB_IMAGE"] == f"{ecr_registry}/findb/staging/fetcher/finlab"

    for workflow_path, expected_images in (
        (FINDB_CD_WORKFLOW, ("FINDB_IMAGE", "DASHBOARD_IMAGE")),
        (FETCHER_CD_WORKFLOW, ("TWELVE_IMAGE", "FINLAB_IMAGE", "SHIOAJI_IMAGE")),
    ):
        staged_steps = _load_workflow(workflow_path)["jobs"]["build-push-staging-ecr"]["steps"]
        build_steps = [
            step for step in staged_steps if "Build or reuse immutable" in step.get("name", "")
        ]
        assert len(build_steps) == len(expected_images)
        for variable, step in zip(expected_images, build_steps, strict=True):
            assert "build_ecr_image_if_missing.sh" in step["run"]
            assert f'"${variable}"' in step["run"]
        assert (
            _load_workflow(workflow_path)["jobs"]["build-push-staging-ecr"]["env"]["ECR_REUSE_ONLY"]
            == "${{ inputs.image_tag != '' }}"
        )


def test_staging_digest_refs_reach_bounded_ssm_pull_and_inspection() -> None:
    expected = {
        FINDB_CD_WORKFLOW: {
            "backend_image_ref": "findb/staging/backend",
            "dashboard_image_ref": "findb/staging/dashboard",
        },
        FETCHER_CD_WORKFLOW: {
            "twelve_image_ref": "findb/staging/fetcher/twelve-data",
            "finlab_image_ref": "findb/staging/fetcher/finlab",
            "shioaji_image_ref": "findb/staging/fetcher/shioaji",
        },
    }
    for workflow_path, image_outputs in expected.items():
        workflow = _load_workflow(workflow_path)
        bridge = workflow["jobs"]["build-push"]
        selection = bridge["steps"][0]
        assert selection["id"] == "release_selection"
        assert set(bridge["outputs"]) == set(image_outputs)

        for output_name, repository in image_outputs.items():
            assert bridge["outputs"][output_name] == (
                f"${{{{ steps.release_selection.outputs.{output_name} }}}}"
            )
            assert selection["env"][output_name.upper()] == (
                f"${{{{ needs.build-push-staging-ecr.outputs.{output_name} }}}}"
            )
            assert repository in selection["run"]
            assert "@sha256:[0-9a-f]{64}" in selection["run"]
            assert f"{output_name}=%s\\n" in selection["run"]

        preflight = _named_step(workflow, "deploy", "Run staging AWS and SSM preflight")
        for output_name, repository in image_outputs.items():
            variable = output_name.upper()
            assert preflight["env"][variable] == (
                f"${{{{ needs.build-push.outputs.{output_name} }}}}"
            )
            assert repository in preflight["run"]
            assert "@sha256:[0-9a-f]{64}" in preflight["run"]

        script = preflight["run"]
        host_start = script.index("host_script=\"$(cat <<'HOST_SCRIPT'\n") + len(
            "host_script=\"$(cat <<'HOST_SCRIPT'\n"
        )
        host_end = script.index("\nHOST_SCRIPT\n", host_start)
        host_script = script[host_start:host_end]
        assert 'aws ecr get-login-password --region "$expected_aws_region"' in host_script
        assert '--username AWS --password-stdin "$expected_ecr_registry"' in host_script
        assert 'docker --config "$docker_config" pull "$image_ref"' in host_script
        assert 'docker image inspect "$image_ref"' in host_script
        assert "--format '{{range .RepoDigests}}{{println .}}{{end}}'" in host_script
        assert 'grep -Fxq "$image_ref"' in host_script
        assert "ecr_digest_inspection=failed" in host_script
        assert f"ecr_digest_pull_inspect=ok images={len(image_outputs)}" in host_script
        assert "mktemp -d /run/" in host_script
        assert 'chmod 0700 "$docker_config"' in host_script
        assert 'rm -rf -- "$docker_config"' in host_script
        assert host_script.index('docker --config "$docker_config" pull') < host_script.index(
            "disk_free_percent="
        )


@pytest.mark.parametrize(
    ("workflow_path", "repositories"),
    (
        (
            FINDB_CD_WORKFLOW,
            {
                "BACKEND_IMAGE_REF": "findb/staging/backend",
                "DASHBOARD_IMAGE_REF": "findb/staging/dashboard",
            },
        ),
        (
            FETCHER_CD_WORKFLOW,
            {
                "TWELVE_IMAGE_REF": "findb/staging/fetcher/twelve-data",
                "FINLAB_IMAGE_REF": "findb/staging/fetcher/finlab",
                "SHIOAJI_IMAGE_REF": "findb/staging/fetcher/shioaji",
            },
        ),
    ),
)
def test_staging_release_bridge_rejects_missing_or_cross_unit_digest(
    tmp_path: Path,
    workflow_path: Path,
    repositories: dict[str, str],
) -> None:
    registry = "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
    selection = _load_workflow(workflow_path)["jobs"]["build-push"]["steps"][0]
    script = selection["run"]
    output_path = tmp_path / "github-output"
    env = {
        **os.environ,
        "DEPLOYMENT_TARGET": "staging",
        "EVENT_NAME": "workflow_dispatch",
        "IMAGE_TAG_INPUT": "",
        "PRODUCTION_BUILD": "skipped",
        "STAGING_ECR_BUILD": "success",
        "CUTOVER_DISABLED": "skipped",
        "VERIFY_RESULT": "success",
        "TARGET_ROUTING_RESULT": "success",
        "GITHUB_OUTPUT": str(output_path),
    }
    for index, (variable, repository) in enumerate(repositories.items(), start=1):
        env[variable] = f"{registry}/{repository}@sha256:{index:064x}"

    accepted = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr
    output_lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(output_lines) == len(repositories)
    for variable, repository in repositories.items():
        output_name = variable.lower()
        assert f"{output_name}={env[variable]}" in output_lines

    output_path.unlink()
    first_variable = next(iter(repositories))
    env[first_variable] = f"{registry}/findb/staging/cross-unit@sha256:{'f' * 64}"
    rejected = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "release bridge" in rejected.stdout
    assert not output_path.exists()


def test_staging_builds_publish_digest_only_release_manifest_artifacts() -> None:
    expected = {
        FINDB_CD_WORKFLOW: (
            ("backend_image", "dashboard_image"),
            "staging-findb-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}",
            "findb-staging-release-manifest.json",
        ),
        FETCHER_CD_WORKFLOW: (
            ("twelve_image", "finlab_image", "shioaji_image"),
            "staging-fetcher-release-manifest-${{ github.run_id }}-${{ github.run_attempt }}",
            "fetcher-staging-release-manifest.json",
        ),
    }
    for workflow_path, (image_steps, artifact_name, manifest_name) in expected.items():
        workflow = _load_workflow(workflow_path)
        staged = workflow["jobs"]["build-push-staging-ecr"]
        assert "infra/deploy/release_manifest.py" in workflow["on"]["push"]["paths"]
        assert set(staged["outputs"]) == {f"{step}_ref" for step in image_steps}
        for image_step in image_steps:
            assert staged["outputs"][f"{image_step}_ref"] == (
                f"${{{{ steps.{image_step}.outputs.image_ref }}}}"
            )
        generate = next(
            step
            for step in staged["steps"]
            if step.get("name") == "Generate and validate staging release manifest"
        )
        script = generate["run"]
        assert 'release_manifest.py" generate' in script
        assert 'release_manifest.py" validate' in script
        assert 'python3 "$SOURCE_BUNDLE_ROOT/infra/deploy/release_manifest.py"' in script
        assert "python3 infra/deploy/release_manifest.py" not in script
        assert '"$ECR_IMAGE_TAG"' in script
        assert '--created-by-run-id "$GITHUB_RUN_ID"' in script
        assert ":latest" not in script
        validate_script = script.split('release_manifest.py" validate', 1)[1]
        assert '--commit-sha "$ECR_IMAGE_TAG"' in validate_script
        assert '--created-by-run-id "$GITHUB_RUN_ID"' in validate_script
        if workflow_path == FINDB_CD_WORKFLOW:
            alembic_head = next(
                step
                for step in staged["steps"]
                if step.get("name") == "Read exact backend Alembic head"
            )
            assert alembic_head["id"] == "backend_alembic_head"
            assert alembic_head["env"] == {
                "BACKEND_IMAGE_REF": "${{ steps.backend_image.outputs.image_ref }}"
            }
            assert (
                'docker run --rm --pull always --network none "$BACKEND_IMAGE_REF" alembic heads'
                in (alembic_head["run"])
            )
            assert "^([0-9a-f]{12})\\ \\(head\\)$" in alembic_head["run"]
            assert "git rev-parse" not in script
            assert generate["env"]["MIGRATION_REVISION"] == (
                "${{ steps.backend_alembic_head.outputs.migration_revision }}"
            )
            assert "--unit findb" in validate_script
            assert '--migration-revision "$MIGRATION_REVISION"' in validate_script
            assert '--image "backend=$BACKEND_IMAGE_REF"' in validate_script
            assert '--image "dashboard=$DASHBOARD_IMAGE_REF"' in validate_script
        else:
            assert "--unit fetcher" in validate_script
            assert "--migration-revision none" in validate_script
            assert '--image "twelve_data=$TWELVE_IMAGE_REF"' in validate_script
            assert '--image "finlab=$FINLAB_IMAGE_REF"' in validate_script
            assert '--image "shioaji=$SHIOAJI_IMAGE_REF"' in validate_script
        upload = next(
            step
            for step in staged["steps"]
            if step.get("name") == "Upload staging release manifest"
        )
        assert upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
        assert upload["if"] == "${{ steps.manifest_support.outputs.supported == 'true' }}"
        assert upload["with"] == {
            "name": artifact_name,
            "path": f"${{{{ runner.temp }}}}/{manifest_name}",
            "if-no-files-found": "error",
            "retention-days": "30",
        }


def test_staging_release_manifest_contracts_come_from_the_selected_source_root() -> None:
    for workflow_path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        staged = _load_workflow(workflow_path)["jobs"]["build-push-staging-ecr"]
        generate = next(
            step
            for step in staged["steps"]
            if step.get("name") == "Generate and validate staging release manifest"
        )
        assert "CONTRACT_MANIFEST" not in generate["env"]
        assert (
            '--contract-manifest "$SOURCE_BUNDLE_ROOT/contracts/manifest.json"' in generate["run"]
        )
        assert "--contract-version" not in generate["run"]
        assert "Materialize selected ingress contract manifest" not in {
            step.get("name") for step in staged["steps"]
        }
        source_bundle = next(
            step
            for step in staged["steps"]
            if step.get("name") == "Materialize selected deployment source inputs"
        )
        assert source_bundle["id"] == "selected_source_bundle"
        assert source_bundle["if"] == "${{ steps.manifest_support.outputs.supported == 'true' }}"
        assert (
            'git archive --format=tar "$ECR_IMAGE_TAG" | tar -x -C "$source_root"'
            in (source_bundle["run"])
        )
        assert generate["env"]["SOURCE_BUNDLE_ROOT"] == (
            "${{ steps.selected_source_bundle.outputs.path }}"
        )
        assert '--repo-root "$SOURCE_BUNDLE_ROOT"' in generate["run"]


def test_selected_manifest_tool_policy_is_immutable_with_bounded_legacy_rollback() -> None:
    for workflow_path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        staged = _load_workflow(workflow_path)["jobs"]["build-push-staging-ecr"]
        support = next(
            step
            for step in staged["steps"]
            if step.get("name") == "Classify selected release manifest support"
        )
        assert support["id"] == "manifest_support"
        script = support["run"]
        assert 'git cat-file -e "$ECR_IMAGE_TAG^{commit}"' in script
        assert 'git ls-tree -z "$ECR_IMAGE_TAG" -- "$manifest_tool_path"' in script
        assert (
            'manifest_entries_file="$(mktemp "${RUNNER_TEMP:?}/selected-manifest-entry.XXXXXX")"'
            in (script)
        )
        assert '[ ! -s "$manifest_entries_file" ]' in script
        assert 'python3 - "$manifest_entries_file" "$manifest_tool_path"' in script
        assert 'git cat-file -e "$manifest_object_id^{blob}"' in script
        assert 'raw.count(b"\\0") != 1' in script
        assert 'git cat-file -e "$ECR_IMAGE_TAG:infra/deploy/release_manifest.py"' not in script
        assert 'elif [ "$ECR_REUSE_ONLY" = true ]; then' in script
        assert 'echo "supported=false" >> "$GITHUB_OUTPUT"' in script
        assert "Legacy staging rollback" in script
        assert "Missing selected release manifest tool" in script
        assert "exit 1" in script
        generate = next(
            step
            for step in staged["steps"]
            if step.get("name") == "Generate and validate staging release manifest"
        )
        assert generate["if"] == "${{ steps.manifest_support.outputs.supported == 'true' }}"
        assert (
            'python3 "$SOURCE_BUNDLE_ROOT/infra/deploy/release_manifest.py" generate'
            in (generate["run"])
        )


def test_selected_manifest_tool_classification_distinguishes_missing_from_git_failures(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "git").write_text(
        """#!/bin/sh
set -eu
case "$1" in
  cat-file)
    case "${GIT_STUB_MODE:?}" in
      commit-failure) case "$3" in *'^{commit}') exit 43 ;; esac ;;
      blob-failure) case "$3" in *'^{blob}') exit 44 ;; esac ;;
    esac
    exit 0
    ;;
  ls-tree)
    case "${GIT_STUB_MODE:?}" in
      present) printf '100755 blob %040d\\tinfra/deploy/release_manifest.py\\0' 0 ;;
      missing) exit 0 ;;
      newline-only) printf '\\n' ;;
      space-only) printf ' ' ;;
      missing-nul) printf '100755 blob %040d\\tinfra/deploy/release_manifest.py' 0 ;;
      failure) exit 47 ;;
      wrong-path) printf '100755 blob %040d\\tinfra/deploy/not-release-manifest.py\\0' 0 ;;
      wrong-type) printf '040000 tree %040d\\tinfra/deploy/release_manifest.py\\0' 0 ;;
      wrong-mode) printf '100600 blob %040d\\tinfra/deploy/release_manifest.py\\0' 0 ;;
      multiple)
        printf '100755 blob %040d\\tinfra/deploy/release_manifest.py\\0' 0
        printf '100755 blob %040d\\tinfra/deploy/release_manifest.py\\0' 1
        ;;
      *) exit 97 ;;
    esac
    ;;
  *) exit 98 ;;
esac
""",
        encoding="utf-8",
    )
    (fake_bin / "git").chmod(0o755)

    def run_support(
        script: str, *, mode: str, reuse_only: bool
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        output = tmp_path / f"{mode}-{reuse_only}.output"
        output.unlink(missing_ok=True)
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "GIT_STUB_MODE": mode,
                "ECR_IMAGE_TAG": "a" * 40,
                "ECR_REUSE_ONLY": str(reuse_only).lower(),
                "GITHUB_OUTPUT": str(output),
                "RUNNER_TEMP": str(tmp_path),
            },
        )
        return result, output.read_text(encoding="utf-8") if output.exists() else ""

    for workflow_path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        script = _named_step(
            _load_workflow(workflow_path),
            "build-push-staging-ecr",
            "Classify selected release manifest support",
        )["run"]
        present, present_output = run_support(script, mode="present", reuse_only=True)
        assert present.returncode == 0
        assert present_output == "supported=true\n"

        missing, missing_output = run_support(script, mode="missing", reuse_only=True)
        assert missing.returncode == 0
        assert missing_output == "supported=false\n"
        assert "Legacy staging rollback" in missing.stdout

        forward_missing, forward_output = run_support(script, mode="missing", reuse_only=False)
        assert forward_missing.returncode != 0
        assert forward_output == ""
        assert "Missing selected release manifest tool" in forward_missing.stdout

        commit_failure, commit_failure_output = run_support(
            script,
            mode="commit-failure",
            reuse_only=True,
        )
        assert commit_failure.returncode != 0
        assert commit_failure_output == ""
        assert "Selected commit unavailable" in commit_failure.stdout

        for mode in (
            "failure",
            "newline-only",
            "space-only",
            "missing-nul",
            "multiple",
            "wrong-path",
            "wrong-type",
            "wrong-mode",
            "blob-failure",
        ):
            failed, failed_output = run_support(script, mode=mode, reuse_only=True)
            assert failed.returncode != 0
            assert failed_output == ""
            assert "Selected manifest tool inspection failed" in failed.stdout


def test_selected_manifest_protocol_is_exact_and_never_a_legacy_fallback(
    tmp_path: Path,
) -> None:
    tool_bodies = {
        "valid": "import sys\nsys.stdout.buffer.write(b'findb-release-manifest-v1\\n')\n",
        "no-v1-cli": "raise SystemExit(2)\n",
        "wrong": "import sys\nsys.stdout.buffer.write(b'findb-release-manifest-v2\\n')\n",
        "multiple": (
            "import sys\nsys.stdout.buffer.write("
            "b'findb-release-manifest-v1\\nfindb-release-manifest-v1\\n')\n"
        ),
        "trailing": "import sys\nsys.stdout.buffer.write(b'findb-release-manifest-v1\\n ')\n",
        "stderr": (
            "import sys\nsys.stdout.buffer.write(b'findb-release-manifest-v1\\n')\n"
            "sys.stderr.write('unexpected output\\n')\n"
        ),
        "failure": "raise SystemExit(9)\n",
    }

    for workflow_path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        workflow = _load_workflow(workflow_path)
        staged = workflow["jobs"]["build-push-staging-ecr"]
        steps = staged["steps"]
        materialize_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Materialize selected deployment source inputs"
        )
        protocol_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Verify selected release manifest protocol"
        )
        generate_index = next(
            index
            for index, step in enumerate(steps)
            if step.get("name") == "Generate and validate staging release manifest"
        )
        protocol = steps[protocol_index]
        assert materialize_index < protocol_index < generate_index
        assert protocol["if"] == "${{ steps.manifest_support.outputs.supported == 'true' }}"
        assert protocol["env"] == {
            "SOURCE_BUNDLE_ROOT": "${{ steps.selected_source_bundle.outputs.path }}"
        }
        assert (
            'python3 "$SOURCE_BUNDLE_ROOT/infra/deploy/release_manifest.py" protocol'
            in protocol["run"]
        )
        assert (
            "printf 'findb-release-manifest-v1\\n' | cmp -s - \"$protocol_output\""
            in protocol["run"]
        )
        assert '[ -s "$protocol_error" ]' in protocol["run"]

        for name, body in tool_bodies.items():
            source_root = tmp_path / workflow_path.stem / name
            tool = source_root / "infra" / "deploy" / "release_manifest.py"
            tool.parent.mkdir(parents=True, exist_ok=True)
            tool.write_text(body, encoding="utf-8")
            result = subprocess.run(
                ["bash", "-c", protocol["run"]],
                capture_output=True,
                text=True,
                check=False,
                env={
                    **os.environ,
                    "RUNNER_TEMP": str(tmp_path),
                    "SOURCE_BUNDLE_ROOT": str(source_root),
                },
            )
            if name == "valid":
                assert result.returncode == 0, result.stdout + result.stderr
            else:
                assert result.returncode != 0
                assert "Legacy staging rollback" not in result.stdout
                expected_title = (
                    "Selected manifest protocol failed"
                    if name in {"no-v1-cli", "failure"}
                    else "Selected manifest protocol invalid"
                )
                assert expected_title in result.stdout


def test_legacy_rollback_checks_only_exact_host_runtime_contract_paths() -> None:
    for workflow_path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        workflow = _load_workflow(workflow_path)
        preflight = _named_step(
            workflow,
            "build-push-staging-ecr",
            "Validate staging ECR release selection",
        )
        script = preflight["run"]
        actual_paths = _legacy_rollback_runtime_paths(workflow_path)

        assert actual_paths == _host_runtime_contract_paths(workflow_path)
        assert "infra/deploy/runtime-secrets" not in actual_paths
        assert "infra/nginx" not in actual_paths
        assert actual_paths.isdisjoint(LEGACY_ROLLBACK_CI_ONLY_PATHS)
        assert (
            "build_ecr_image_if_missing.sh"
            not in script.split('git diff --quiet "$ECR_IMAGE_TAG" HEAD --', 1)[1].split(
                "; then", 1
            )[0]
        )
        assert (
            "release_manifest.py"
            not in script.split('git diff --quiet "$ECR_IMAGE_TAG" HEAD --', 1)[1].split(
                "; then", 1
            )[0]
        )

        if workflow_path == FINDB_CD_WORKFLOW:
            assert "infra/nginx/serve-key.conf" not in actual_paths
            assert "backend/scripts/render_nginx_serve_key.py" not in actual_paths


def test_legacy_rollback_allows_only_ci_foundation_deltas_before_manifest_support_gate(
    tmp_path: Path,
) -> None:
    def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo,
            capture_output=True,
            text=True,
            check=check,
        )

    for index, workflow_path in enumerate((FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW)):
        runtime_paths = frozenset(_host_runtime_contract_paths(workflow_path))
        repo = tmp_path / f"legacy-{index}"
        repo.mkdir()
        git(repo, "init", "--initial-branch=main")
        git(repo, "config", "user.email", "release-manifest-test@example.invalid")
        git(repo, "config", "user.name", "Release Manifest Test")
        for path in runtime_paths:
            candidate = repo / path
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("selected runtime contract\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "pre-foundation runtime contract")
        selected_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

        # This mirrors a merged current tree whose only new files are the CI
        # foundation helper/tool/workflow/docs/tests; the selected commit has
        # no manifest tool, so the workflow must reach its legacy warning gate.
        for path in LEGACY_ROLLBACK_CI_ONLY_PATHS:
            candidate = repo / path
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("phase3 foundation only\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "phase3 ci foundation")
        assert (
            git(
                repo,
                "diff",
                "--quiet",
                selected_sha,
                "HEAD",
                "--",
                *sorted(runtime_paths),
                check=False,
            ).returncode
            == 0
        )
        assert (
            git(
                repo,
                "cat-file",
                "-e",
                f"{selected_sha}:infra/deploy/release_manifest.py",
                check=False,
            ).returncode
            != 0
        )
        support_script = _named_step(
            _load_workflow(workflow_path),
            "build-push-staging-ecr",
            "Classify selected release manifest support",
        )["run"]
        assert 'elif [ "$ECR_REUSE_ONLY" = true ]; then' in support_script
        assert "Legacy staging rollback" in support_script
        assert 'echo "supported=false" >> "$GITHUB_OUTPUT"' in support_script

        for path in runtime_paths:
            (repo / path).write_text("changed runtime contract\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "runtime contract changed")
        for runtime_path in runtime_paths:
            assert (
                git(
                    repo,
                    "diff",
                    "--quiet",
                    selected_sha,
                    "HEAD",
                    "--",
                    runtime_path,
                    check=False,
                ).returncode
                == 1
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
        os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}", AWS_REGION="ap-southeast-1"
    )

    invalid_findb = subprocess.run(
        ["bash", str(findb_helper)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **environment,
            "AWS_REGION": "ap-southeast-1",
            "IMAGE_TAG": "A" * 40,
            "ECR_REGISTRY": "evil.example",
            "FINDB_IMAGE": "evil.example/backend",
            "DASHBOARD_IMAGE": "evil.example/dashboard",
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
            "IMAGE_TAG": "a" * 40,
            "ECR_REGISTRY": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com",
            "FINDB_IMAGE": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/backend",
            "DASHBOARD_IMAGE": "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/dashboard",
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


def test_fetcher_target_route_is_validated_before_job_routing() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    jobs = workflow["jobs"]
    validation = jobs["validate-target-routing"]
    assert "if" not in validation
    assert validation["permissions"] == {"contents": "read"}
    step = _named_step(
        workflow,
        "validate-target-routing",
        "Validate target-derived runtime route before build",
    )
    script = step["run"]
    assert "Expected staging or production" in script
    assert "STAGING_ECR_CUTOVER_ENABLED" in script
    assert "validate-target-routing" in jobs["build-push-production"]["needs"]
    assert "validate-target-routing" in jobs["deploy"]["needs"]
    assert "needs.validate-target-routing.result == 'success'" in jobs["deploy"]["if"]
    assert "validate-target-routing" in jobs["finlab-acquisition-smoke"]["needs"]


def test_fetcher_deploy_checks_out_pinned_repository_before_local_artifact_steps() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    deploy = workflow["jobs"]["deploy"]
    steps = deploy["steps"]
    checkout = steps[0]

    assert deploy["permissions"] == {"contents": "read", "id-token": "write"}
    assert checkout["uses"] == "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"

    for step_name in (
        "Validate target-derived deployment route",
        "Run staging AWS and SSM preflight",
        "Calculate AWS runtime-secret bundle checksums",
        "Verify AWS runtime-secret bundle ownership, mode, and checksum",
        "Sync AWS runtime-secret loader and exact Fetcher catalog",
    ):
        assert steps.index(checkout) < steps.index(_named_step(workflow, "deploy", step_name))


def test_runtime_secret_canary_precedes_every_aws_runtime_deployment() -> None:
    expected = {
        FINDB_CD_WORKFLOW: (
            "Run FinDB AWS runtime-secret canary",
            ("Deploy to EC2 with AWS runtime secrets",),
        ),
        FETCHER_CD_WORKFLOW: (
            "Run Fetcher AWS runtime-secret canary",
            (
                "Release Twelve Data scheduler with AWS runtime secrets",
                "Release FinLab scheduler with AWS runtime secrets",
                "Release Shioaji scheduler with AWS runtime secrets",
            ),
        ),
    }
    for workflow_path, (canary_name, deployment_names) in expected.items():
        workflow = _load_workflow(workflow_path)
        steps = workflow["jobs"]["deploy"]["steps"]
        canary_index = next(
            index for index, step in enumerate(steps) if step.get("name") == canary_name
        )
        assert "--check-only" in steps[canary_index]["with"]["script"]
        for deployment_name in deployment_names:
            deployment_index = next(
                index for index, step in enumerate(steps) if step.get("name") == deployment_name
            )
            assert canary_index < deployment_index


def test_aws_runtime_bundles_are_verified_from_staging_then_installed() -> None:
    findb_workflow = _load_workflow(FINDB_CD_WORKFLOW)
    findb_sync = _named_step(
        findb_workflow, "deploy", "Sync AWS runtime-secret loader and exact FinDB catalog"
    )
    assert findb_sync["with"]["target"] == "/tmp/findb-runtime-secrets"
    assert "strip_components" not in findb_sync["with"]
    for source in (
        "infra/deploy/runtime-secrets/load_runtime_secrets.py",
        "infra/deploy/runtime-secrets/findb.json",
        "infra/deploy/runtime-secrets/install_findb_bootstrap.sh",
        "docker-compose.prod.yml",
        "infra/nginx/nginx.conf",
        "infra/nginx/source-allowlist.conf",
        "infra/nginx/cloudflare-real-ip.conf",
    ):
        assert source in findb_sync["with"]["source"]
    findb_verify = _named_step(
        findb_workflow,
        "deploy",
        "Verify AWS runtime-secret bundle ownership, mode, and checksum",
    )["with"]["script"]
    assert "staging_root=/tmp/findb-runtime-secrets" in findb_verify
    assert 'sha256sum "$staging_root/$relative_path"' in findb_verify
    assert "sudo install -o root -g root" in findb_verify
    assert "/opt/findb/docker-compose.prod.yml" in findb_verify
    assert '"/home/ubuntu/etc/nginx/$file"' in findb_verify
    assert "ubuntu:ubuntu:644" in findb_verify
    assert "Deploy artifact install failed" in findb_verify
    assert "Nginx artifact install failed" in findb_verify
    assert findb_verify.index('sha256sum "$staging_root/$relative_path"') < findb_verify.index(
        "sudo install -o root -g root"
    )

    aws_render = _named_step(
        findb_workflow,
        "deploy",
        "Render nonsecret nginx configs for AWS runtime secrets",
    )
    assert "render_nginx_source_allowlist.py" in aws_render["run"]
    assert "render_nginx_cloudflare_real_ip.py" in aws_render["run"]
    assert "render_nginx_serve_key.py" not in aws_render["run"]
    assert "${{ secrets." not in json.dumps(aws_render, sort_keys=True)
    aws_validation = _named_step(
        findb_workflow,
        "deploy",
        "Validate AWS runtime deployment configuration",
    )
    assert 'if [ "${PORT:-}" != "8080" ]' in aws_validation["run"]
    assert "${{ secrets." not in json.dumps(aws_validation, sort_keys=True)
    bootstrap_step = _named_step(
        findb_workflow,
        "deploy",
        "Install FinDB boot-time runtime-secret dependency",
    )
    assert "install_findb_bootstrap.sh" in bootstrap_step["with"]["script"]
    steps = findb_workflow["jobs"]["deploy"]["steps"]
    verify_step = _named_step(
        findb_workflow,
        "deploy",
        "Verify AWS runtime-secret bundle ownership, mode, and checksum",
    )
    canary_step = _named_step(
        findb_workflow,
        "deploy",
        "Run FinDB AWS runtime-secret canary",
    )
    assert steps.index(verify_step) < steps.index(bootstrap_step) < steps.index(canary_step)

    fetcher_workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    fetcher_sync = _named_step(
        fetcher_workflow,
        "deploy",
        "Sync AWS runtime-secret loader and exact Fetcher catalog",
    )
    assert fetcher_sync["with"]["target"] == "/tmp/fetcher-runtime-secrets"
    assert "strip_components" not in fetcher_sync["with"]
    fetcher_verify = _named_step(
        fetcher_workflow,
        "deploy",
        "Verify AWS runtime-secret bundle ownership, mode, and checksum",
    )["with"]["script"]
    assert "staging_root=/tmp/fetcher-runtime-secrets/infra/deploy/runtime-secrets" in (
        fetcher_verify
    )
    assert 'sha256sum "$staging_root/$file"' in fetcher_verify
    assert "sudo install -o root -g root" in fetcher_verify
    assert fetcher_verify.index('sha256sum "$staging_root/$file"') < fetcher_verify.index(
        "sudo install -o root -g root"
    )

    findb_prepare = _named_step(
        findb_workflow, "deploy", "Prepare AWS runtime-secret directories on EC2"
    )["with"]["script"]
    assert "sudo rm -rf -- /tmp/findb-runtime-secrets" in findb_prepare
    assert "-o ubuntu -g ubuntu -m 0700 /tmp/findb-runtime-secrets" in findb_prepare
    fetcher_prepare = _named_step(
        fetcher_workflow,
        "deploy",
        "Prepare AWS runtime-secret directories on Fetcher EC2",
    )["with"]["script"]
    assert "sudo rm -rf -- /tmp/fetcher-runtime-secrets" in fetcher_prepare
    assert "-o ubuntu -g ubuntu -m 0700 /tmp/fetcher-runtime-secrets" in fetcher_prepare


def test_aws_runtime_steps_use_exact_consumers_and_no_runner_secret_values() -> None:
    findb_workflow = _load_workflow(FINDB_CD_WORKFLOW)
    findb_workflow_scripts = "\n".join(
        str(step.get("with", {}).get("script", ""))
        for step in findb_workflow["jobs"]["deploy"]["steps"]
        if "STAGING_ECR_CUTOVER_ENABLED" in str(step.get("if", ""))
    )
    findb_helper = (REPO_ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text(
        encoding="utf-8"
    )
    assert "--consumer canary" in findb_workflow_scripts
    assert "--ecr-registry" in findb_helper
    assert "--consumer compose" in findb_helper
    assert "--consumer migration" in findb_helper
    assert "MIGRATION_DATABASE_URL=DATABASE_URL" in findb_helper
    assert "FINDB_LOOKUP_SERVE_API_KEY" not in findb_helper
    nginx_helper = (REPO_ROOT / "infra/deploy/runtime-secrets/render_nginx_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "--consumer nginx" in nginx_helper

    fetcher_workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    fetcher_steps = {
        step["name"]: step["with"]["script"]
        for step in fetcher_workflow["jobs"]["deploy"]["steps"]
        if "STAGING_ECR_CUTOVER_ENABLED" in str(step.get("if", ""))
        and "with" in step
        and "script" in step["with"]
    }
    assert "--consumer canary" in fetcher_steps["Run Fetcher AWS runtime-secret canary"]
    for name, consumer in (
        ("Release Twelve Data scheduler with AWS runtime secrets", "twelve-data"),
        ("Release FinLab scheduler with AWS runtime secrets", "finlab"),
        ("Release Shioaji scheduler with AWS runtime secrets", "shioaji"),
    ):
        script = fetcher_steps[name]
        assert "--consumer registry" not in script
        assert script.count(f"--consumer {consumer}") == 1
        assert '--ecr-registry "$ECR_REGISTRY"' in script
        assert "--consumer canary" not in script
    smoke_step = _named_step(
        fetcher_workflow,
        "finlab-acquisition-smoke",
        "Run bounded FinLab acquisition smoke with AWS runtime secrets",
    )
    smoke_canary_step = _named_step(
        fetcher_workflow,
        "finlab-acquisition-smoke",
        "Run Fetcher AWS runtime-secret canary before FinLab smoke",
    )
    smoke_steps = fetcher_workflow["jobs"]["finlab-acquisition-smoke"]["steps"]
    assert smoke_steps.index(smoke_canary_step) < smoke_steps.index(smoke_step)
    assert "--check-only" in smoke_canary_step["with"]["script"]
    smoke_script = smoke_step["with"]["script"]
    assert "--consumer registry" not in smoke_script
    assert smoke_script.count("--consumer finlab-smoke") == 1
    assert '--ecr-registry "$ECR_REGISTRY"' in smoke_script
    assert "expected_region=ap-southeast-1" in smoke_script
    assert (
        "expected_image=439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/fetcher/finlab"
        in smoke_script
    )
    assert '[[ ! "$FETCHER_IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]]' in smoke_script
    assert 'expected_full_image="${expected_image}:${FETCHER_IMAGE_TAG}"' in smoke_script
    assert 'requested_image="${FETCHER_FINLAB_IMAGE}:${FETCHER_IMAGE_TAG}"' in smoke_script
    assert '[ "$requested_image" = "$expected_full_image" ]' in smoke_script
    assert smoke_script.index("expected_full_image=") < smoke_script.index(
        "runtime_secret_command.sh"
    )
    assert "FETCHER_CALENDAR_SERVE_API_KEY" not in smoke_script
    assert "FETCHER_FINLAB_SOURCE_CLIENT_KEY" not in smoke_script
    assert "CLOUDFLARE_R2_RAW_" not in smoke_script
    assert "${{ secrets." not in json.dumps(smoke_step.get("env", {}), sort_keys=True)
    assert "${{ secrets." not in smoke_step["with"]["envs"]
    assert "GITHUB_TOKEN" not in smoke_step["with"]["envs"]


def test_runtime_secret_helpers_enforce_tmpfs_cleanup_and_registry_isolation() -> None:
    command = (REPO_ROOT / "infra/deploy/runtime-secrets/runtime_secret_command.sh").read_text(
        encoding="utf-8"
    )
    assert "set +x" in command
    assert 'findmnt -n -o FSTYPE -T "$runtime_root"' in command
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

    runbook = INGESTION_RUNBOOK.read_text(encoding="utf-8")
    assert "runtime-secret wrapper" in runbook
    assert "persistent `.env`" in runbook
    assert "`export` `CELERY_BROKER_URL`" in runbook
    assert (
        "sudo /opt/findb/runtime-secrets/runtime_secret_command.sh \\\n"
        "  --catalog /opt/findb/runtime-secrets/findb.json \\\n"
        "  --region ap-southeast-1 \\\n"
        "  --consumer compose \\\n"
        "  -- docker exec -e CELERY_BROKER_URL findb-ingest \\\n"
        "  python /app/scripts/check_queue_health.py"
    ) in runbook


def test_lookup_secret_is_rendered_only_to_tmpfs_and_compose_never_mounts_persistent_key() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    nginx_volumes = compose["services"]["nginx"]["volumes"]
    assert (
        "/run/findb-runtime-secrets/nginx/serve-key.conf:/etc/nginx/serve-key.conf:ro"
        in nginx_volumes
    )
    assert not any("/home/ubuntu/etc/nginx/serve-key.conf:" in volume for volume in nginx_volumes)

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
    nginx_helper = (REPO_ROOT / "infra/deploy/runtime-secrets/render_nginx_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "/run/findb-runtime-secrets/nginx/serve-key.conf" in nginx_helper


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

    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    shioaji = _named_step(
        workflow,
        "deploy",
        "Release Shioaji scheduler with AWS runtime secrets",
    )["with"]["script"]
    assert "/var/lib/findb-shioaji-fetcher/cache" in shioaji


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
    tmp_path: Path, *, marker_identity: str
) -> subprocess.CompletedProcess[str]:
    helper = REPO_ROOT / "infra/deploy/runtime-secrets/release_fetcher_provider.sh"
    state_dir = tmp_path / "state"
    cache_dir = tmp_path / "cache"
    fake_bin = tmp_path / "bin"
    state_dir.mkdir(parents=True)
    fake_bin.mkdir()
    state_path = state_dir / "state.sqlite3"
    state_path.write_text("durable-state\n", encoding="utf-8")
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
        "439622209937.dkr.ecr.ap-southeast-1.amazonaws.com/findb/staging/fetcher/twelve-data:"
        + "a" * 40
    )
    environment = dict(os.environ)
    environment.update(
        AWS_REGION="ap-southeast-1",
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


def test_fetcher_finlab_smoke_symbols_remain_a_quoted_comma_delimited_choice() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    symbols_input = workflow["on"]["workflow_dispatch"]["inputs"]["finlab_symbols"]
    assert symbols_input["default"] == "2330,2317"
    assert symbols_input["options"] == ["2330,2317"]

    source = FETCHER_CD_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r'^\s+default: "2330,2317"$', source, re.MULTILINE)
    assert re.search(r'^\s+- "2330,2317"$', source, re.MULTILINE)


def test_pull_requests_use_aggregate_ci_and_main_push_uses_each_cd_gate() -> None:
    required_ci = _load_workflow(REQUIRED_CI_WORKFLOW)
    required_triggers = required_ci["on"]
    assert required_triggers["pull_request"] == {"branches": ["main"]}
    assert "push" not in required_triggers
    assert required_ci["jobs"]["findb"]["uses"] == "./.github/workflows/findb-ci.yml"
    assert required_ci["jobs"]["fetcher"]["uses"] == "./.github/workflows/fetcher-ci.yml"
    normal_infra_plan = required_ci["jobs"]["staging-infra-plan"]
    assert normal_infra_plan["uses"] == "./.github/workflows/staging-infra-plan.yml"
    assert "with" not in normal_infra_plan
    assert normal_infra_plan["if"] == (
        "${{ needs.changes.outputs.infra == 'true' && "
        "github.event.pull_request.head.ref != 'chore/staging-phase2-retirement' }}"
    )
    assert normal_infra_plan["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }

    retirement_infra_plan = required_ci["jobs"]["staging-infra-retirement-plan"]
    assert retirement_infra_plan["uses"] == (
        "FPI-TW/findb/.github/workflows/staging-infra-plan.yml@"
        "efe573be9267a52a0b7e3090d00445f38df9945a"
    )
    assert retirement_infra_plan["with"] == {"allow_ghcr_metadata_retirement": "true"}
    assert retirement_infra_plan["if"] == (
        "${{ needs.changes.outputs.infra == 'true' && "
        "github.event.pull_request.head.ref == 'chore/staging-phase2-retirement' }}"
    )
    assert retirement_infra_plan["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert required_ci["jobs"]["required"]["name"] == "Required CI"
    assert required_ci["jobs"]["required"]["if"] == "${{ always() }}"
    assert required_ci["jobs"]["required"]["needs"] == [
        "changes",
        "findb",
        "fetcher",
        "staging-infra-plan",
        "staging-infra-retirement-plan",
    ]

    changes = required_ci["jobs"]["changes"]
    assert changes["outputs"]["infra"] == "${{ steps.classify.outputs.infra }}"
    classify_script = _named_step(required_ci, "changes", "Classify changed paths")["run"]
    infra_match = re.search(
        r'case "\$path" in\s+(?P<patterns>[^)]+)\)\s+infra=true',
        classify_script,
    )
    assert infra_match is not None
    assert {pattern.strip() for pattern in infra_match.group("patterns").split("|")} == {
        "infra/tofu/**",
        ".github/workflows/required-ci.yml",
        ".github/workflows/staging-infra-plan.yml",
    }

    verify_script = _named_step(required_ci, "required", "Verify routed CI results")["run"]
    assert "INFRA_NORMAL_RESULT" in verify_script
    assert "INFRA_RETIREMENT_RESULT" in verify_script
    assert '"$INFRA_EXPECTED" == "false"' in verify_script
    assert (
        '"$INFRA_EXPECTED" == "true" && "$PR_HEAD_REF" == "chore/staging-phase2-retirement"'
        in verify_script
    )
    assert (
        '"$INFRA_NORMAL_RESULT" != "skipped" || "$INFRA_RETIREMENT_RESULT" != "skipped"'
        in verify_script
    )
    assert (
        '"$INFRA_NORMAL_RESULT" != "skipped" || "$INFRA_RETIREMENT_RESULT" != "success"'
        in verify_script
    )
    assert (
        '"$INFRA_NORMAL_RESULT" != "success" || "$INFRA_RETIREMENT_RESULT" != "skipped"'
        in verify_script
    )

    for ci_path, cd_path, reusable_path in (
        (FINDB_CI_WORKFLOW, FINDB_CD_WORKFLOW, "./.github/workflows/findb-ci.yml"),
        (FETCHER_CI_WORKFLOW, FETCHER_CD_WORKFLOW, "./.github/workflows/fetcher-ci.yml"),
    ):
        ci_workflow = _load_workflow(ci_path)
        ci_triggers = ci_workflow["on"]
        assert "push" not in ci_triggers
        assert "pull_request" not in ci_triggers
        assert "workflow_call" in ci_triggers
        assert "workflow_dispatch" in ci_triggers

        cd_workflow = _load_workflow(cd_path)
        assert cd_workflow["on"]["push"]["branches"] == ["main"]
        assert cd_workflow["jobs"]["verify"]["uses"] == reusable_path


@pytest.mark.parametrize(
    ("infra_expected", "head_ref", "normal_result", "retirement_result", "expected_returncode"),
    [
        ("false", "feature/no-infra", "skipped", "skipped", 0),
        ("true", "chore/staging-phase2-retirement", "skipped", "success", 0),
        ("true", "feat/ordinary-infra", "success", "skipped", 0),
        ("false", "feature/no-infra", "success", "skipped", 1),
        ("true", "chore/staging-phase2-retirement", "success", "skipped", 1),
        ("true", "feat/ordinary-infra", "skipped", "success", 1),
    ],
)
def test_required_ci_infrastructure_routing_truth_table(
    infra_expected: str,
    head_ref: str,
    normal_result: str,
    retirement_result: str,
    expected_returncode: int,
) -> None:
    required_ci = _load_workflow(REQUIRED_CI_WORKFLOW)
    verify_script = _named_step(required_ci, "required", "Verify routed CI results")["run"]
    assert isinstance(verify_script, str)

    completed = subprocess.run(
        ["bash", "-c", verify_script],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CHANGES_RESULT": "success",
            "FINDB_RESULT": "skipped",
            "FETCHER_RESULT": "skipped",
            "INFRA_NORMAL_RESULT": normal_result,
            "INFRA_RETIREMENT_RESULT": retirement_result,
            "FINDB_EXPECTED": "false",
            "FETCHER_EXPECTED": "false",
            "INFRA_EXPECTED": infra_expected,
            "PR_HEAD_REF": head_ref,
        },
    )

    assert completed.returncode == expected_returncode, completed.stderr


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
                (*config.variables_for(target), *config.secrets, *config.optional_secrets)
            )
            assert configured_names == documented_names


def test_phase1_aws_variables_are_staging_only_and_match_examples() -> None:
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
        },
        "fetcher": {
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCOUNT_ID": "439622209937",
            "AWS_DEPLOY_ROLE_ARN": "arn:aws:iam::439622209937:role/fetcher-staging-deploy",
            "AWS_INSTANCE_PROFILE_NAME": "fetcher-staging-instance",
            "AWS_SSM_LOG_GROUP": "/findb/staging/fetcher/ssm",
            "AWS_DNS_CHECK_NAME": "findb-staging.tingfong.com",
        },
    }

    for service, config in namespace["SERVICE_CONFIGS"].items():
        assert phase1_names <= set(config.variables_for("staging"))
        assert not phase1_names & set(config.variables_for("production"))

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

        production_example = ENV_CONFIG_ROOT / "production" / service / "remote.env.example"
        production_text = production_example.read_text(encoding="utf-8")
        assert not any(f"{name}=" in production_text for name in phase1_names)


def test_staging_cd_preflight_is_oidc_ssm_bounded_and_deploy_only() -> None:
    action_sha = "aws-actions/configure-aws-credentials@e6de054238d6b7531b4efff3b6587d9aade6a06c"
    common_script_markers = (
        "aws sts get-caller-identity",
        "Name=tag:Project,Values=findb",
        "Name=tag:Environment,Values=staging",
        "--query 'Reservations[].Instances[].InstanceId'",
        "IamInstanceProfile.Arn",
        "describe-instance-information",
        "Online",
        "AWS-RunShellScript",
        '--instance-ids "$target_id"',
        "CloudWatchOutputEnabled=true",
        "AWS_SSM_LOG_GROUP",
        "filter-log-events",
        '--log-group-name "$AWS_SSM_LOG_GROUP"',
        '--log-stream-name-prefix "${command_id}/${target_id}/aws-runShellScript/stdout"',
        "--query 'length(events)'",
        "--output json",
        "phase1_preflight_marker=",
        "status=success",
        "status=failed",
        "169.254.169.254/latest/api/token",
        "amazon-ssm-agent",
        "docker --version",
        "docker compose version",
        "disk_free_percent",
        "inode_free_percent",
        "timedatectl show -p NTPSynchronized",
        "date -u",
        "getent ahosts",
    )

    for workflow_path, unit in (
        (FINDB_CD_WORKFLOW, "findb"),
        (FETCHER_CD_WORKFLOW, "fetcher"),
    ):
        workflow = _load_workflow(workflow_path)
        jobs = workflow["jobs"]
        deploy = jobs["deploy"]
        assert deploy["permissions"]["id-token"] == "write"
        for job_name, job in jobs.items():
            if job_name not in {"deploy", "build-push-staging-ecr"}:
                assert job.get("permissions", {}).get("id-token") != "write"

        credential_step = _named_step(workflow, "deploy", "Configure staging AWS credentials")
        preflight_step = _named_step(workflow, "deploy", "Run staging AWS and SSM preflight")
        assert credential_step["uses"] == action_sha
        assert (
            credential_step["if"] == "${{ (inputs.deployment_target || 'staging') == 'staging' }}"
        )
        assert credential_step["with"] == {
            "role-to-assume": "${{ vars.AWS_DEPLOY_ROLE_ARN }}",
            "aws-region": "ap-southeast-1",
            "allowed-account-ids": "${{ vars.AWS_ACCOUNT_ID }}",
            "role-session-name": f"{unit}-staging-preflight-${{{{ github.run_id }}}}",
        }
        assert preflight_step["if"] == credential_step["if"]
        assert preflight_step["env"]["DEPLOYMENT_UNIT"] == unit
        assert preflight_step["env"]["AWS_REGION"] == "ap-southeast-1"
        assert preflight_step["env"]["PREFLIGHT_MARKER_TOKEN"] == (
            f"${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}-{unit}"
        )
        script = preflight_step["run"]
        for marker in common_script_markers:
            assert marker in script
        assert f"Name=tag:DeploymentUnit,Values={unit}" in script
        assert "AWS_ACCOUNT_ID" in script
        assert "AWS_DEPLOY_ROLE_ARN" in script
        assert "AWS_INSTANCE_PROFILE_NAME" in script
        assert "AWS_DNS_CHECK_NAME" in script
        assert "get-secret-value" in script
        assert "phase1-preflight-denial-probe" in script
        assert "findb/staging/${DEPLOYMENT_UNIT}/phase1-preflight-denial-probe" in script
        assert "arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_-]+$" in script
        assert 'AWS_INSTANCE_PROFILE_NAME" =~ ^[A-Za-z0-9+=,.@_-]+$' in script
        assert 'AWS_SSM_LOG_GROUP" =~ ^[A-Za-z0-9_.\\/#-]+$' in script
        assert ">/dev/null 2>&1" in script
        assert "unexpectedly succeeded" in script
        assert 'probe_stderr="$(mktemp)"' in script
        assert "trap cleanup_probe EXIT" in script
        assert (
            'get-secret-value --region "$AWS_REGION" --secret-id "$probe_secret_arn" >/dev/null 2>"$probe_stderr"'
            in script
        )
        assert 'grep -Fq "AccessDenied" "$probe_stderr" 2>/dev/null' in script
        assert "Secret denial probe did not return AccessDenied" in script
        assert "secret_read_denial=AccessDenied" in script
        assert "--query StandardOutputContent" not in script
        assert "--query StandardErrorContent" not in script
        assert "send-command" in script
        assert "get-command-invocation" not in script
        assert "--query Status" not in script
        assert "StandardOutputContent" not in script
        assert "StandardErrorContent" not in script
        assert "events[0].eventId" not in script
        assert "--log-stream-names" not in script
        assert "None" not in script.split("failure_event_count=", 1)[1]
        assert "None" not in script.split("success_event_count=", 1)[1]
        assert "--no-paginate" not in script
        assert "for attempt in $(seq 1 36); do" in script
        send_command = script.split("aws ssm send-command", 1)[1].split('if [ -z "$command_id"', 1)[
            0
        ]
        assert send_command.count('--instance-ids "$target_id"') == 1
        assert "--cloud-watch-output-config" in script
        assert "--output json 2>/dev/null" in script
        assert script.count("aws logs filter-log-events") == 2
        assert script.count('--log-group-name "$AWS_SSM_LOG_GROUP"') == 2
        assert (
            script.count(
                '--log-stream-name-prefix "${command_id}/${target_id}/aws-runShellScript/stdout"'
            )
            == 2
        )
        assert script.count("--query 'length(events)'") == 2
        for marker_count in ("failure_event_count", "success_event_count"):
            marker_guard = re.search(
                rf'if ! \[\[ "\${marker_count}" =~ \^\[0-9\]\+\$ \]\]; then(?P<body>.*?)\n\s+fi',
                script,
                re.DOTALL,
            )
            assert marker_guard is not None
            assert "exit 1" in marker_guard.group("body")
        assert 'if [ "$failure_event_count" -gt 0 ]; then' in script
        assert 'if [ "$success_event_count" -gt 0 ]; then' in script
        assert "marker_event_count=${success_event_count}" in script
        failure_query = script.index('failure_event_count="$(')
        success_query = script.index('success_event_count="$(')
        failure_decision = script.index('if [ "$failure_event_count" -gt 0 ]; then')
        success_decision = script.index('if [ "$success_event_count" -gt 0 ]; then')
        assert failure_query < failure_decision < success_query < success_decision
        assert "command_id=${command_id}" in script
        host_start = script.index("host_script=\"$(cat <<'HOST_SCRIPT'\n") + len(
            "host_script=\"$(cat <<'HOST_SCRIPT'\n"
        )
        host_end = script.index("\nHOST_SCRIPT\n", host_start)
        host_script = script[host_start:host_end]
        assert "security-credentials/" not in host_script
        assert "meta-data/iam/info" in host_script
        assert "InstanceProfileArn" in host_script
        assert "sed -n" in host_script
        assert "jq" not in host_script
        assert "instance_profile_match" in host_script
        assert "amazon-ssm-agent.service" in host_script
        assert "snap.amazon-ssm-agent.amazon-ssm-agent.service" in host_script
        assert 'systemctl is-active --quiet "$candidate"' in host_script
        assert 'if [ -z "$ssm_agent_service" ]' in host_script
        assert "ssm_agent=active service=%s" in host_script
        assert "trap emit_preflight_failure EXIT" in host_script
        assert "trap - EXIT" in host_script
        assert "phase1_preflight_marker=%s status=failed" in host_script
        assert "phase1_preflight_marker=%s status=success" in host_script

        step_indices = deploy["steps"]
        credential_index = step_indices.index(credential_step)
        preflight_index = step_indices.index(preflight_step)
        ssh_indices = [
            index
            for index, step in enumerate(step_indices)
            if str(step.get("uses", "")).startswith("appleboy/")
        ]
        assert ssh_indices
        assert credential_index < preflight_index < min(ssh_indices)

        production = (ENV_CONFIG_ROOT / "production" / unit / "remote.env.example").read_text(
            encoding="utf-8"
        )
        for aws_name in (
            "AWS_REGION",
            "AWS_ACCOUNT_ID",
            "AWS_DEPLOY_ROLE_ARN",
            "AWS_INSTANCE_PROFILE_NAME",
            "AWS_SSM_LOG_GROUP",
            "AWS_DNS_CHECK_NAME",
        ):
            assert f"{aws_name}=" not in production


def _run_staging_ssm_marker_polling_case(
    tmp_path: Path,
    workflow_path: Path,
    *,
    failure_counts: tuple[str, ...],
    success_counts: tuple[str, ...],
    aws_query_status: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run the workflow's marker polling block with an offline AWS CLI stub."""

    workflow = _load_workflow(workflow_path)
    script = _named_step(workflow, "deploy", "Run staging AWS and SSM preflight")["run"]
    polling_start = script.index('success_marker="')
    polling_end = script.index('if [ "$marker_state" != "success" ]', polling_start)
    polling_script = script[polling_start:polling_end]
    state_dir = tmp_path / workflow_path.stem
    state_dir.mkdir()

    harness = (
        """
set -euo pipefail

aws() {
  if [ "$AWS_QUERY_STATUS" -ne 0 ]; then
    return "$AWS_QUERY_STATUS"
  fi
  local log_stream_prefix=""
  local filter_pattern=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --log-stream-name-prefix)
        log_stream_prefix="$2"
        shift 2
        ;;
      --filter-pattern)
        filter_pattern="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  if [ "$log_stream_prefix" != "${command_id}/${target_id}/aws-runShellScript/stdout" ]; then
    return 41
  fi
  case "$filter_pattern" in
    *"status=failed"*)
      query_index="$(<"$FAILURE_QUERY_INDEX_FILE")"
      if [ "$query_index" -lt "${#FAILURE_COUNTS[@]}" ]; then
        count_value="${FAILURE_COUNTS[$query_index]}"
      else
        count_value="${FAILURE_COUNTS[$(( ${#FAILURE_COUNTS[@]} - 1 ))]}"
      fi
      [ "$count_value" = "__EMPTY__" ] && count_value=""
      printf '%s\n' "$count_value"
      printf '%s\n' "$((query_index + 1))" > "$FAILURE_QUERY_INDEX_FILE"
      ;;
    *"status=success"*)
      query_index="$(<"$SUCCESS_QUERY_INDEX_FILE")"
      if [ "$query_index" -lt "${#SUCCESS_COUNTS[@]}" ]; then
        count_value="${SUCCESS_COUNTS[$query_index]}"
      else
        count_value="${SUCCESS_COUNTS[$(( ${#SUCCESS_COUNTS[@]} - 1 ))]}"
      fi
      [ "$count_value" = "__EMPTY__" ] && count_value=""
      printf '%s\n' "$count_value"
      printf '%s\n' "$((query_index + 1))" > "$SUCCESS_QUERY_INDEX_FILE"
      ;;
    *)
      return 42
      ;;
  esac
}

sleep() { :; }

AWS_REGION=test-region
AWS_SSM_LOG_GROUP=test-log-group
PREFLIGHT_MARKER_TOKEN=test-token
command_id=test-command
target_id=test-target
IFS='|' read -r -a FAILURE_COUNTS <<< "$FAILURE_COUNT_SEQUENCE"
IFS='|' read -r -a SUCCESS_COUNTS <<< "$SUCCESS_COUNT_SEQUENCE"
FAILURE_QUERY_INDEX_FILE="$STATE_DIR/failure_query_index"
SUCCESS_QUERY_INDEX_FILE="$STATE_DIR/success_query_index"
printf '0\n' > "$FAILURE_QUERY_INDEX_FILE"
printf '0\n' > "$SUCCESS_QUERY_INDEX_FILE"
"""
        + polling_script
        + """
if [ "$marker_state" != "success" ]; then
  exit 1
fi
"""
    )
    environment = os.environ.copy()
    environment.update(
        {
            "FAILURE_COUNT_SEQUENCE": "|".join(
                "__EMPTY__" if count == "" else count for count in failure_counts
            ),
            "SUCCESS_COUNT_SEQUENCE": "|".join(
                "__EMPTY__" if count == "" else count for count in success_counts
            ),
            "AWS_QUERY_STATUS": str(aws_query_status),
            "STATE_DIR": str(state_dir),
        }
    )
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    ("case", "failure_counts", "success_counts", "aws_query_status", "expected_returncode"),
    (
        ("stream absent/count 0 times out", ("0",), ("0",), 0, 1),
        ("stream absent then success passes", ("0", "0"), ("0", "3"), 0, 0),
        ("failure marker takes priority", ("2",), ("3",), 0, 1),
        ("empty failure count fails closed", ("",), ("1",), 0, 1),
        ("non-numeric failure count fails closed", ("None",), ("1",), 0, 1),
        ("empty success count fails closed", ("0",), ("",), 0, 1),
        ("non-numeric success count fails closed", ("0",), ("invalid",), 0, 1),
        ("AWS query failure fails closed", ("0",), ("1",), 7, 1),
    ),
)
def test_staging_ssm_marker_count_polling_behavior(
    tmp_path: Path,
    case: str,
    failure_counts: tuple[str, ...],
    success_counts: tuple[str, ...],
    aws_query_status: int,
    expected_returncode: int,
) -> None:
    del case
    for workflow_path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        completed = _run_staging_ssm_marker_polling_case(
            tmp_path,
            workflow_path,
            failure_counts=failure_counts,
            success_counts=success_counts,
            aws_query_status=aws_query_status,
        )
        message = f"{workflow_path.name}: stdout={completed.stdout!r} stderr={completed.stderr!r}"
        if expected_returncode == 0:
            assert completed.returncode == 0, message
        else:
            assert completed.returncode != 0, message


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
    assert "\nSERVE_REQUIRE_AUTH=true\n" in production_findb


def test_contract_changes_gate_both_ci_workflows_but_not_cd() -> None:
    assert "contracts/**" in _aggregate_ci_paths("findb")
    assert "contracts/**" in _aggregate_ci_paths("fetcher")

    for path in (FINDB_CI_WORKFLOW, FETCHER_CI_WORKFLOW):
        assert "workflow_call" in _load_workflow(path)["on"]

    for path in (FINDB_CD_WORKFLOW, FETCHER_CD_WORKFLOW):
        workflow = _load_workflow(path)
        assert "contracts/**" not in workflow["on"]["push"]["paths"]


def test_fetcher_ci_covers_contract_generator_source_and_dependency_inputs() -> None:
    required_paths = {
        "contracts/**",
        "backend/app/**",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/scripts/export_ingress_contracts.py",
        "backend/tests/test_contract_artifacts.py",
    }
    assert required_paths <= _aggregate_ci_paths("fetcher")


def test_fetcher_cd_policy_changes_route_the_backend_manifest_contract_tests() -> None:
    assert ".github/workflows/fetcher-cd.yml" in _aggregate_ci_paths("findb")
    assert ".github/workflows/fetcher-cd.yml" in _aggregate_ci_paths("fetcher")
    assert "fetcher/**" not in _aggregate_ci_paths("findb")
    findb_ci = _load_workflow(FINDB_CI_WORKFLOW)
    backend_tests = _named_step(findb_ci, "backend", "Run tests")["run"]
    assert "pytest" in backend_tests
    assert "test_deployment_checks.py" not in backend_tests
    assert "test_release_manifest.py" not in backend_tests
    # No filter means this policy route executes both tests, while ordinary
    # Fetcher source edits continue to route only the Fetcher reusable CI.
    required = _load_workflow(REQUIRED_CI_WORKFLOW)["jobs"]["required"]
    assert required["needs"] == [
        "changes",
        "findb",
        "fetcher",
        "staging-infra-plan",
        "staging-infra-retirement-plan",
    ]


def test_fetcher_ci_retries_transient_container_build_failures() -> None:
    fetcher_ci = _load_workflow(FETCHER_CI_WORKFLOW)
    build_step = _named_step(fetcher_ci, "test", "Build container")
    script = build_step["run"]

    assert "for attempt in 1 2 3; do" in script
    assert 'if docker build -f fetcher/Dockerfile -t "$image" .; then' in script
    assert 'if [ "$attempt" -eq 3 ]; then' in script
    assert 'sleep "$delay"' in script
    assert "Docker build failed after 3 attempts" in script


def test_dashboard_image_inputs_gate_findb_ci_and_cd() -> None:
    required_paths = {
        "dashboard/**",
        "package.json",
        "pnpm-workspace.yaml",
        "pnpm-lock.yaml",
    }

    assert required_paths <= _aggregate_ci_paths("findb")

    findb_cd = _load_workflow(FINDB_CD_WORKFLOW)
    assert required_paths <= set(findb_cd["on"]["push"]["paths"])


def test_service_ci_and_cd_triggers_cover_deployment_units_without_cross_deploy() -> None:
    findb_cd = _load_workflow(FINDB_CD_WORKFLOW)
    fetcher_cd = _load_workflow(FETCHER_CD_WORKFLOW)

    required_findb_ci_paths = {
        "backend/**",
        "dashboard/**",
        "contracts/**",
        "docker-compose.yml",
        "docker-compose.prod.yml",
        "infra/nginx/**",
    }
    assert required_findb_ci_paths <= _aggregate_ci_paths("findb")
    shared_runtime_paths = {
        "infra/deploy/release_manifest.py",
        "infra/deploy/runtime-secrets/load_runtime_secrets.py",
        "infra/deploy/runtime-secrets/runtime_secret_command.sh",
        "infra/deploy/runtime-secrets/build_ecr_image_if_missing.sh",
    }
    assert shared_runtime_paths <= _aggregate_ci_paths("findb")
    assert shared_runtime_paths <= _aggregate_ci_paths("fetcher")
    assert {
        "infra/deploy/runtime-secrets/render_nginx_runtime.sh",
        "infra/deploy/runtime-secrets/render_serve_key.py",
        "infra/deploy/runtime-secrets/install_findb_bootstrap.sh",
        "infra/deploy/runtime-secrets/deploy_findb_aws.sh",
        "infra/deploy/runtime-secrets/findb.json",
    } <= _aggregate_ci_paths("findb")
    assert {
        "infra/deploy/runtime-secrets/release_fetcher_provider.sh",
        "infra/deploy/runtime-secrets/fetcher.json",
    } <= _aggregate_ci_paths("fetcher")
    assert "infra/deploy/runtime-secrets/release_fetcher_provider.sh" not in _aggregate_ci_paths(
        "findb"
    )
    assert "infra/deploy/runtime-secrets/findb.json" not in _aggregate_ci_paths("fetcher")

    required_findb_cd_paths = {
        "backend/**",
        "dashboard/**",
        "infra/nginx/**",
        "docker-compose.prod.yml",
        ".github/workflows/findb-cd.yml",
    }
    findb_cd_paths = set(findb_cd["on"]["push"]["paths"])
    assert required_findb_cd_paths <= findb_cd_paths
    assert "fetcher/**" not in findb_cd_paths
    assert "contracts/**" not in findb_cd_paths

    fetcher_cd_paths = set(fetcher_cd["on"]["push"]["paths"])
    assert fetcher_cd_paths == {
        "fetcher/**",
        "infra/deploy/runtime-secrets/load_runtime_secrets.py",
        "infra/deploy/runtime-secrets/runtime_secret_command.sh",
        "infra/deploy/runtime-secrets/release_fetcher_provider.sh",
        "infra/deploy/runtime-secrets/build_ecr_image_if_missing.sh",
        "infra/deploy/runtime-secrets/fetcher.json",
        "infra/deploy/release_manifest.py",
        ".github/workflows/fetcher-cd.yml",
    }


def test_root_context_images_use_service_specific_dockerignore_files() -> None:
    assert not (REPO_ROOT / ".dockerignore").exists()

    findb_cd = _load_workflow(FINDB_CD_WORKFLOW)
    dashboard_build = _named_step(
        findb_cd, "build-push-production", "Build and push dashboard image"
    )
    assert dashboard_build["with"]["context"] == "."
    assert dashboard_build["with"]["file"] == "dashboard/Dockerfile"
    backend_build = _named_step(findb_cd, "build-push-production", "Build and push backend image")
    assert backend_build["with"]["context"] == "./backend"
    assert "${{ env.IMAGE }}:${{ github.sha }}" in backend_build["with"]["tags"]
    assert "${{ env.DASHBOARD_IMAGE }}:${{ github.sha }}" in dashboard_build["with"]["tags"]

    fetcher_cd = _load_workflow(FETCHER_CD_WORKFLOW)
    fetcher_build = _named_step(fetcher_cd, "build-push-production", "Build and push Fetcher image")
    assert fetcher_build["with"]["context"] == "."
    assert fetcher_build["with"]["file"] == "fetcher/Dockerfile"
    assert fetcher_build["with"]["tags"] == (
        "${{ env.FETCHER_IMAGE }}:${{ env.FETCHER_IMAGE_TAG }}"
    )

    service_builds = (
        (dashboard_build, FINDB_CD_WORKFLOW, "dashboard/**"),
        (fetcher_build, FETCHER_CD_WORKFLOW, "fetcher/**"),
    )
    for build_step, workflow_path, trigger_path in service_builds:
        dockerfile = build_step["with"]["file"]
        assert (REPO_ROOT / f"{dockerfile}.dockerignore").is_file()
        workflow = _load_workflow(workflow_path)
        assert trigger_path in workflow["on"]["push"]["paths"]


def test_deployment_secret_references_are_confined_to_environment_jobs() -> None:
    for path, service in (
        (FINDB_CD_WORKFLOW, "findb"),
        (FETCHER_CD_WORKFLOW, "fetcher"),
    ):
        workflow = _load_workflow(path)
        environment = f"${{{{ inputs.deployment_target || 'staging' }}}}-{service}"
        assert workflow["jobs"]["deploy"]["environment"] == environment
        permitted_secret_jobs = {"deploy": environment}
        if path == FETCHER_CD_WORKFLOW:
            smoke = workflow["jobs"]["finlab-acquisition-smoke"]
            assert smoke["environment"] == "staging-fetcher"
            assert "github.event_name == 'workflow_dispatch'" in smoke["if"]
            assert "inputs.run_finlab_smoke == true" in smoke["if"]
            permitted_secret_jobs["finlab-acquisition-smoke"] = "staging-fetcher"
            isolation = workflow["jobs"]["validate-fetcher-credential-isolation"]
            assert isolation["environment"] == environment
            permitted_secret_jobs["validate-fetcher-credential-isolation"] = environment

        secret_jobs: set[str] = set()
        for reference_path in _secret_reference_paths(workflow):
            referenced_value: object = workflow
            for component in reference_path:
                if isinstance(referenced_value, list):
                    referenced_value = referenced_value[int(component)]
                else:
                    referenced_value = referenced_value[component]
            if referenced_value == "${{ secrets.GITHUB_TOKEN }}":
                continue
            assert reference_path[:1] == ("jobs",)
            job_name = reference_path[1]
            assert job_name in permitted_secret_jobs
            assert workflow["jobs"][job_name]["environment"] == permitted_secret_jobs[job_name]
            secret_jobs.add(job_name)
        assert secret_jobs == set(permitted_secret_jobs)

    for path in (FINDB_CI_WORKFLOW, FETCHER_CI_WORKFLOW):
        assert _secret_reference_paths(_load_workflow(path)) == []


def test_findb_deployment_uses_dedicated_credentials_and_queue_health_key() -> None:
    workflow = _load_workflow(FINDB_CD_WORKFLOW)
    validate = _named_step(workflow, "deploy", "Validate deployment configuration")
    render = _named_step(workflow, "deploy", "Render nginx configs")
    deploy = _named_step(workflow, "deploy", "Deploy to EC2")

    validation_script = validate["run"]
    assert "ADMIN_BREAK_GLASS_API_KEY" in validation_script
    assert "FINDB_LOOKUP_SERVE_API_KEY must be configured" in validation_script
    assert "FINDB_STATIC_CACHE_SERVE_API_KEY must be configured" in validation_script
    assert (
        "FINDB_LOOKUP_SERVE_API_KEY and FINDB_STATIC_CACHE_SERVE_API_KEY must be distinct"
        in validation_script
    )

    required_loop = next(line for line in validation_script.splitlines() if "for name in " in line)
    assert "FINDB_QUEUE_HEALTH_ADMIN_API_KEY" in required_loop
    for name in (
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_CANONICAL_BUCKET",
        "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_CANONICAL_PUBLISHER_SECRET_ACCESS_KEY",
        "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_CANONICAL_READER_SECRET_ACCESS_KEY",
    ):
        assert name in required_loop
    assert "Canonical R2 credential reuse" in validation_script

    assert render["env"]["FINDB_LOOKUP_SERVE_API_KEY"] == (
        "${{ secrets.FINDB_LOOKUP_SERVE_API_KEY }}"
    )
    assert '--key "${FINDB_LOOKUP_SERVE_API_KEY:-}"' in render["run"]
    assert "--keys" not in render["run"]

    forwarded = set(deploy["with"]["envs"].split(","))
    assert "ADMIN_BREAK_GLASS_API_KEY" in forwarded
    assert deploy["env"]["CELERY_BROKER_URL"] == "${{ secrets.CELERY_BROKER_URL }}"
    assert "CELERY_BROKER_URL" in forwarded
    assert "FINDB_LOOKUP_SERVE_API_KEY" not in forwarded
    assert "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID" in forwarded
    assert "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID" in forwarded

    compose = PROD_COMPOSE.read_text(encoding="utf-8")
    assert (
        'ADMIN_BREAK_GLASS_API_KEY: "${ADMIN_BREAK_GLASS_API_KEY:'
        "?ADMIN_BREAK_GLASS_API_KEY must be set"
    ) in compose
    assert 'FINDB_QUEUE_HEALTH_ADMIN_API_KEY: "${FINDB_QUEUE_HEALTH_ADMIN_API_KEY:' in compose
    parsed_compose = yaml.safe_load(compose)
    ingest_env = parsed_compose["services"]["ingest"]["environment"]
    serve_env = parsed_compose["services"]["serve"]["environment"]
    worker_env = parsed_compose["services"]["worker"]["environment"]
    assert "CELERY_BROKER_URL" not in ingest_env
    assert "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID" in serve_env
    assert "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID" not in serve_env
    assert "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID" in worker_env
    assert "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID" not in worker_env
    for service in ("ingest", "dispatcher", "raw-cleanup"):
        environment = parsed_compose["services"][service]["environment"]
        assert "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID" not in environment
        assert "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID" not in environment
    assert "DASHBOARD_USERNAME" not in compose
    assert "DASHBOARD_PASSWORD" not in compose

    legacy_script = deploy["with"]["script"]
    assert "exec -T -e CELERY_BROKER_URL ingest" in legacy_script
    assert "export CELERY_BROKER_URL" not in legacy_script


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

    assert "FINDB_EC2_HOST" in findb_cd
    assert "FETCHER_EC2_HOST" not in findb_cd
    assert "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY" not in findb_cd
    assert "PROVIDER_" not in findb_cd

    assert "FETCHER_EC2_HOST" in fetcher_cd
    assert "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY" in fetcher_cd
    assert "FETCHER_FINLAB_SOURCE_CLIENT_KEY" in fetcher_cd
    assert "FETCHER_SHIOAJI_SOURCE_CLIENT_KEY" in fetcher_cd
    assert "FETCHER_SOURCE_CLIENT_KEY" not in fetcher_cd
    for forbidden in (
        "FINDB_EC2_",
        "DATABASE_URL",
        "CELERY_BROKER_URL",
        "RABBITMQ_",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_SESSION_SECRET",
    ):
        assert forbidden not in fetcher_cd

    assert "secrets: inherit" not in findb_cd
    assert "secrets: inherit" not in fetcher_cd


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


def test_fetcher_cd_builds_and_pushes_three_immutable_images() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    assert workflow["env"]["FETCHER_IMAGE_TAG"] == "${{ inputs.image_tag || github.sha }}"
    production_build = workflow["jobs"]["build-push-production"]
    assert production_build["env"] == {
        "FETCHER_IMAGE": "ghcr.io/fpi-tw/findb-fetcher",
        "FETCHER_FINLAB_IMAGE": "ghcr.io/fpi-tw/findb-fetcher-finlab",
        "FETCHER_SHIOAJI_IMAGE": "ghcr.io/fpi-tw/findb-fetcher-shioaji",
    }

    expected = {
        "Build and push Fetcher image": (
            "fetcher/Dockerfile",
            "${{ env.FETCHER_IMAGE }}:${{ env.FETCHER_IMAGE_TAG }}",
        ),
        "Build and push FinLab Fetcher image": (
            "fetcher/Dockerfile.finlab",
            "${{ env.FETCHER_FINLAB_IMAGE }}:${{ env.FETCHER_IMAGE_TAG }}",
        ),
        "Build and push Shioaji Fetcher image": (
            "fetcher/Dockerfile.shioaji",
            "${{ env.FETCHER_SHIOAJI_IMAGE }}:${{ env.FETCHER_IMAGE_TAG }}",
        ),
    }
    for name, (dockerfile, tag) in expected.items():
        step = _named_step(workflow, "build-push-production", name)
        assert step["with"]["context"] == "."
        assert step["with"]["file"] == dockerfile
        assert str(step["with"]["push"]).lower() == "true"
        assert step["with"]["tags"] == tag


def test_fetcher_provider_deployment_steps_are_secret_confined() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    providers = {
        "twelve": (
            "Release and validate Twelve Data scheduler on Fetcher EC2",
            "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY",
            {"TWELVE_DATA_API_KEY"},
            {"FINLAB_API_TOKEN", "SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"},
        ),
        "finlab": (
            "Release and validate FinLab scheduler on Fetcher EC2",
            "FETCHER_FINLAB_SOURCE_CLIENT_KEY",
            {"FINLAB_API_TOKEN"},
            {"TWELVE_DATA_API_KEY", "SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"},
        ),
        "shioaji": (
            "Release and validate Shioaji scheduler on Fetcher EC2",
            "FETCHER_SHIOAJI_SOURCE_CLIENT_KEY",
            {"SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"},
            {"TWELVE_DATA_API_KEY", "FINLAB_API_TOKEN"},
        ),
    }
    workflow_text = FETCHER_CD_WORKFLOW.read_text(encoding="utf-8")
    for desired_state_name in (
        "FETCHER_SCHEDULER_DESIRED_STATE",
        "FETCHER_FINLAB_SCHEDULER_DESIRED_STATE",
        "FETCHER_SHIOAJI_SCHEDULER_DESIRED_STATE",
    ):
        assert desired_state_name not in workflow_text

    for provider, (
        step_name,
        source_key,
        own_credentials,
        forbidden_credentials,
    ) in providers.items():
        step = _named_step(workflow, "deploy", step_name)
        env = step["env"]
        forwarded = set(step["with"]["envs"].split(","))
        script = step["with"]["script"]
        assert source_key in env and source_key in forwarded
        assert "FETCHER_SCHEDULER_CONTROL_POLL_SECONDS" in env
        assert "FETCHER_SCHEDULER_CONTROL_POLL_SECONDS" in forwarded
        assert "--env FETCHER_SCHEDULER_CONTROL_POLL_SECONDS" in script
        assert "FETCHER_CALENDAR_SERVE_API_KEY" in env
        assert own_credentials <= set(env) and own_credentials <= forwarded
        assert not forbidden_credentials & set(env)
        assert not forbidden_credentials & forwarded
        assert (
            "DATABASE_URL" not in env
            and "GITHUB_TOKEN"
            not in script[script.index("runtime_env_args=") : script.index("reconcile_scheduler")]
        )
        assert "--env GITHUB_TOKEN" not in script
        assert "--env GITHUB_ACTOR" not in script
        assert "--read-only" in script
        assert "--cap-drop ALL" in script
        assert "--security-opt no-new-privileges" in script
        assert "--log-opt max-size=10m" in script
        assert "--log-opt max-file=3" in script
        assert "--user 10001:10001" in script
        assert "raw-bucket.sha256" in script
        assert "recover_scheduler()" in script
        assert "for attempt in $(seq 1 6)" in script
        assert "{{.RestartCount}}" in script
        assert "desired_state" not in script
        assert "running|stopped" not in script
        assert "docker create" not in script
        assert "docker run -d" in script
        assert "--run-forever" in script
        assert 'docker start "$stable"' in script
        preflight = script.index("--check")
        stable_stop = script.index('docker stop --time 30 "$stable"', preflight)
        assert preflight < stable_stop
        stable_rename = script.index('docker rename "$stable" "$previous"', stable_stop)
        graceful_gate = script[stable_stop:stable_rename]
        assert "{{.State.ExitCode}}" in graceful_gate
        assert 'if [ "$stable_exit_code" -ne 0 ]' in graceful_gate
        assert "bootstrap_ref" not in graceful_gate
        assert "bootstrap_id" not in graceful_gate
        assert "shutdown bootstrap" not in graceful_gate.lower()
        assert 'docker rename "$candidate" "$stable"' in script
        assert 'docker rename "$previous" "$stable"' in script
        assert 'docker rm -f "$candidate"' in script
        image_prune = script.index("docker image prune -af")
        image_pull = script.index('docker pull "$image"')
        assert image_prune < image_pull
        assert script.count("docker image prune -af") == 1
        assert "docker system prune" not in script
        assert "docker volume prune" not in script

        if provider == "twelve":
            assert "/var/lib/findb-fetcher/state.sqlite3" in script
            assert "findb-fetcher-scheduler-candidate" in script
            assert "findb-fetch-scheduler --check" in script
            assert "findb-fetch-scheduler --run-forever" in script
            assert (
                "--schedule-file /app/configs/daily_scheduler.v2.json "
                "--slot-id western_markets_window --dataset-key us_equity_eod" in script
            )
        elif provider == "finlab":
            assert "/var/lib/findb-finlab-fetcher/state.sqlite3" in script
            assert "/var/lib/findb-finlab-fetcher/cache" in script
            assert "dst=/home/fetcher" in script
            assert "findb-fetcher-finlab-scheduler-candidate" in script
            assert "findb-fetch-finlab-scheduler --check" in script
            assert "--slot-id taiwan_market_window --dataset-key tw_equity_eod" in script
        else:
            assert "/var/lib/findb-shioaji-fetcher/state.sqlite3" in script
            assert 'cache_dir="$state_dir/cache"' in script
            assert "dst=/home/fetcher" in script
            assert "findb-fetcher-shioaji-scheduler-candidate" in script
            assert "findb-fetch-shioaji-scheduler --check" in script
            assert "--manifest /app/configs/shioaji_tw_pilot.v1.json" in script
            assert env["SHIOAJI_SIMULATION"] == "${{ vars.SHIOAJI_SIMULATION || 'true' }}"
            assert "SHIOAJI_SIMULATION must be true" in script


def test_fetcher_smoke_prunes_only_unused_images_before_pull() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    step = _named_step(
        workflow,
        "finlab-acquisition-smoke",
        "Run bounded FinLab acquisition smoke with AWS runtime secrets",
    )
    script = step["with"]["script"]

    image_prune = script.index("docker image prune -af")
    image_pull = script.index('docker pull "$image"')
    assert image_prune < image_pull
    assert script.count("docker image prune -af") == 1
    assert "docker system prune" not in script
    assert "docker volume prune" not in script
    assert '[[ "$FETCHER_IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]]' in script
    assert script.index('[[ "$FETCHER_IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]]') < image_pull


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
  inspect)
    [ "$1" = --format ]
    format="$2"
    name="$3"
    status="$(cat "$state_dir/$name")"
    case "$format" in
      *Config.Image*) printf '%s\\n' "${FAKE_IMAGE:?}" ;;
      *Config.User*) printf '10001:10001\\n' ;;
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


@pytest.mark.parametrize("provider", _fetcher_scheduler_cases(), ids=lambda item: item[0])
def test_all_providers_reject_unscoped_raw_bucket_marker(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
) -> None:
    stable = provider[3]

    completed, statuses, _, durable_state = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "running"},
        wrong_raw_bucket_marker=True,
    )

    assert completed.returncode != 0
    assert "raw bucket mismatch" in completed.stderr.lower()
    assert statuses == {stable: "running"}
    assert durable_state == "durable-state\n"


@pytest.mark.parametrize("provider", _fetcher_scheduler_cases(), ids=lambda item: item[0])
@pytest.mark.parametrize("initial_status", ("stopped", "running"))
def test_fetcher_provider_reconciliation_always_converges_to_running(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
    initial_status: str,
) -> None:
    stable = provider[3]
    completed, statuses, operations, _ = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: initial_status},
    )

    assert completed.returncode == 0, completed.stderr
    assert statuses == {stable: "running"}
    assert provider[4] not in statuses
    assert provider[5] not in statuses
    assert "run" in operations


@pytest.mark.parametrize("provider", _fetcher_scheduler_cases(), ids=lambda item: item[0])
def test_fetcher_provider_reconciliation_cleans_interrupted_candidate(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
) -> None:
    stable, candidate = provider[3], provider[4]
    completed, statuses, operations, _ = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "running", candidate: "stale-candidate"},
    )

    assert completed.returncode == 0, completed.stderr
    assert statuses == {stable: "running"}
    assert candidate not in statuses
    assert "rm" in operations


@pytest.mark.parametrize("provider", _fetcher_scheduler_cases(), ids=lambda item: item[0])
def test_fetcher_provider_candidate_failure_rolls_back_and_preserves_sqlite(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
) -> None:
    stable, candidate, previous = provider[3], provider[4], provider[5]
    completed, statuses, operations, durable_state = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "stopped"},
        candidate_status="exited",
    )

    assert completed.returncode != 0
    assert statuses == {stable: "running"}
    assert candidate not in statuses
    assert previous not in statuses
    assert "start" in operations
    assert durable_state == "durable-state\n"


@pytest.mark.parametrize("provider", _fetcher_scheduler_cases(), ids=lambda item: item[0])
def test_fetcher_provider_forced_stop_blocks_promotion_and_restores_stable(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
) -> None:
    stable, candidate, previous = provider[3], provider[4], provider[5]
    completed, statuses, operations, durable_state = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "running"},
        stable_exit_code=137,
    )

    assert completed.returncode != 0
    assert "graceful stop failed" in completed.stderr.lower()
    assert statuses == {stable: "running"}
    assert candidate not in statuses
    assert previous not in statuses
    assert "start" in operations
    assert durable_state == "durable-state\n"


def test_fetcher_cd_validates_target_specific_r2_buckets_and_credential_isolation_before_deploy() -> (
    None
):
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    predeploy = workflow["jobs"]["validate-r2-bucket-configuration"]
    isolation = workflow["jobs"]["validate-fetcher-credential-isolation"]
    deploy = workflow["jobs"]["deploy"]
    r2_script = predeploy["steps"][0]["run"]
    isolation_script = isolation["steps"][0]["run"]

    assert predeploy["needs"] == "build-push"
    assert isolation["needs"] == "build-push"
    assert "inputs.run_finlab_smoke != true" in predeploy["if"]
    assert "inputs.run_finlab_smoke != true" in isolation["if"]
    assert deploy["needs"] == [
        "build-push",
        "validate-target-routing",
        "validate-r2-bucket-configuration",
        "validate-fetcher-credential-isolation",
        "validate-fetcher-credential-isolation-aws",
    ]
    assert predeploy["env"] == {
        "DEPLOYMENT_TARGET": "${{ inputs.deployment_target || 'staging' }}",
        "R2_RAW_BUCKET": "${{ vars.CLOUDFLARE_R2_RAW_BUCKET }}",
    }
    assert "staging|production)" in r2_script
    assert 'if [ -z "$R2_RAW_BUCKET" ]; then' in r2_script
    assert "FETCHER_CALENDAR_SERVE_API_KEY" in isolation_script
    assert "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY" in isolation_script
    assert "FETCHER_FINLAB_SOURCE_CLIENT_KEY" in isolation_script
    assert "FETCHER_SHIOAJI_SOURCE_CLIENT_KEY" in isolation_script
    assert "credential isolation failed" in isolation_script
    assert "without printing credential values" in isolation_script


def test_fetcher_postbuild_gates_always_evaluate_their_existing_fail_closed_conditions() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    jobs = workflow["jobs"]
    expected_gates = {
        "validate-r2-bucket-configuration": (
            "github.event_name == 'workflow_dispatch'",
            "inputs.run_finlab_smoke != true",
            "needs.build-push.result == 'success'",
        ),
        "validate-fetcher-credential-isolation": (
            "(inputs.deployment_target || 'staging') == 'production'",
            "github.event_name == 'workflow_dispatch'",
            "inputs.run_finlab_smoke != true",
            "needs.build-push.result == 'success'",
        ),
        "validate-fetcher-credential-isolation-aws": (
            "(inputs.deployment_target || 'staging') == 'staging'",
            "vars.STAGING_ECR_CUTOVER_ENABLED == 'true'",
            "github.event_name == 'workflow_dispatch'",
            "inputs.run_finlab_smoke != true",
            "needs.build-push.result == 'success'",
        ),
        "finlab-acquisition-smoke": (
            "github.event_name == 'workflow_dispatch'",
            "inputs.run_finlab_smoke == true",
            "(inputs.deployment_target || 'staging') == 'staging'",
            "vars.STAGING_ECR_CUTOVER_ENABLED == 'true'",
            "needs.build-push.result == 'success'",
            "needs.validate-target-routing.result == 'success'",
        ),
    }

    for job_name, gates in expected_gates.items():
        condition = jobs[job_name]["if"]
        assert condition.startswith("always() &&")
        for gate in gates:
            assert gate in condition


def test_fetcher_deploy_retains_the_complete_fail_closed_postbuild_result_matrix() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    condition = " ".join(workflow["jobs"]["deploy"]["if"].split())

    assert condition == (
        "always() && "
        "github.event_name == 'workflow_dispatch' && "
        "(github.event_name != 'workflow_dispatch' || inputs.run_finlab_smoke != true) && "
        "needs.build-push.result == 'success' && "
        "needs.validate-target-routing.result == 'success' && "
        "needs.validate-r2-bucket-configuration.result == 'success' && "
        "( "
        "((inputs.deployment_target || 'staging') == 'production' && "
        "needs.validate-fetcher-credential-isolation.result == 'success' && "
        "needs.validate-fetcher-credential-isolation-aws.result == 'skipped') || "
        "((inputs.deployment_target || 'staging') == 'staging' && "
        "needs.validate-fetcher-credential-isolation.result == 'skipped' && "
        "needs.validate-fetcher-credential-isolation-aws.result == 'success') "
        ")"
    )


def test_ec2_setup_instructions_match_findb_environment_boundary() -> None:
    setup_script = (BACKEND_ROOT / "scripts" / "setup_ec2.sh").read_text(encoding="utf-8")

    assert "staging-findb" in setup_script
    assert "production-findb" in setup_script
    assert "FINDB_EC2_HOST" in setup_script
    assert "FINDB_EC2_USER" in setup_script
    assert "FINDB_EC2_SSH_KEY" in setup_script
    assert "\n       EC2_HOST" not in setup_script
    assert "staging-fetcher" in setup_script
    assert "production-fetcher" in setup_script


def test_deploy_does_not_gate_on_ec2_hardware_size() -> None:
    deploy = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "_NPROCESSORS_ONLN" not in deploy
    assert "/proc/meminfo" not in deploy
    assert "EC2 must have at least" not in deploy
    assert "mountpoint -q /var/lib/findb/rabbitmq" not in deploy
    assert "rabbitmq_disk_kib" not in deploy
    assert "RabbitMQ EBS volume is smaller" not in deploy
    assert "sudo mkdir -p /var/lib/findb/rabbitmq" in deploy


def _findb_deploy_script() -> str:
    workflow = _load_workflow(FINDB_CD_WORKFLOW)
    return _named_step(workflow, "deploy", "Deploy to EC2")["with"]["script"]


def test_findb_deploy_retries_transient_ghcr_failures_before_database_preflight() -> None:
    deploy = _findb_deploy_script()

    helper = deploy[deploy.index("retry_registry_command() {") : deploy.index("ghcr_login() {")]
    login = deploy.index('retry_registry_command "GHCR login" ghcr_login')
    pull = deploy.index('retry_registry_command "GHCR image pull"')
    predeploy = deploy.index("python /app/scripts/predeploy_db_check.py", pull)

    assert "local max_attempts=5" in helper
    assert "local delay_seconds=5" in helper
    assert 'for attempt in $(seq 1 "$max_attempts")' in helper
    assert "delay_seconds=$((delay_seconds * 2))" in helper
    assert 'return "$exit_code"' in helper
    assert "failed after ${max_attempts} attempts" in helper
    assert deploy.count('retry_registry_command "') == 2
    assert "printf '%s\\n' \"$GITHUB_TOKEN\"" in deploy
    assert "docker login ghcr.io" in deploy
    assert "trap 'docker logout ghcr.io" in deploy
    assert login < pull < predeploy


def test_deploy_stops_and_verifies_all_writers_before_alembic() -> None:
    deploy = _findb_deploy_script()
    pull = deploy.index("docker compose -f docker-compose.prod.yml pull")
    predeploy = deploy.index("python /app/scripts/predeploy_db_check.py", pull)
    stop = deploy.index(
        'if ! docker compose -f docker-compose.prod.yml stop --timeout 30 "${writer_services[@]}"',
        predeploy,
    )
    verification = deploy.index('if ! verify_services_stopped "${writer_services[@]}"', stop)
    migration = deploy.index('echo "Running Alembic migrations"', verification)
    stop_block = deploy[stop:verification]
    gate = deploy[deploy.index("verify_services_stopped() {") : verification]
    writer_diagnostics = deploy[deploy.index("diagnose_writer_services() {") : stop]

    assert "writer_services=(ingest dispatcher worker raw-cleanup)" in deploy
    assert "2>/dev/null" not in stop_block
    assert "|| true" not in stop_block
    assert 'stop --timeout 30 "${writer_services[@]}"; then' in stop_block
    assert 'diagnose_writer_services "${writer_services[@]}"' in stop_block
    assert 'diagnose_services "${writer_services[@]}"' not in stop_block
    assert "docker compose -f docker-compose.prod.yml logs" not in deploy[stop:migration]
    assert "docker compose -f docker-compose.prod.yml logs" not in writer_diagnostics
    assert "{{.State.Status}} {{.State.Running}} {{.State.Restarting}}" in writer_diagnostics
    assert pull < predeploy < stop < verification < migration
    assert 'docker compose -f docker-compose.prod.yml ps -aq "$service"' in gate
    assert "head -n 1" not in gate
    assert "{{.State.Status}} {{.State.Running}} {{.State.Restarting}}" in gate
    assert "enumeration_sentinel" in gate
    assert "BASH_REMATCH" in gate
    assert "read -r -a" not in gate
    assert "([^[:space:]]+)\\ (true|false)\\ (true|false)" in gate
    assert "exited|created" in gate
    assert '[ "$running" != "false" ]' in gate
    assert '[ "$restarting" != "false" ]' in gate


def test_writer_stopped_gate_fails_closed_for_enumeration_and_state_failures() -> None:
    deploy = _findb_deploy_script()
    gate = deploy[
        deploy.index("verify_services_stopped() {") : deploy.index("\n\n# Legacy mode may stage")
    ]

    assert "container enumeration failed" in gate
    assert "inspect-failed" in gate
    assert "invalid inspect output" in gate
    assert 'if [ "$running" != "false" ] || [ "$restarting" != "false" ]; then' in gate
    assert "failed=1" in gate
    assert "state=unknown" in gate
    assert "unsafe or unknown" in gate
    assert 'case "$status" in' in gate
    assert "exited|created)" in gate
    assert 'return "$failed"' in gate


_FAKE_WRITER_GATE_DOCKER = """#!/bin/sh
set -eu
state_dir="${FAKE_DOCKER_STATE:?}"
operation="${1:-}"
shift

case "$operation" in
  compose)
    while [ "$#" -gt 0 ] && [ "$1" != ps ]; do
      shift
    done
    [ "${1:-}" = ps ]
    shift
    [ "${1:-}" = -aq ]
    service="${2:-}"
    if [ "${FAKE_ENUM_FAIL_SERVICE:-}" = "$service" ]; then
      echo "enumeration failed" >&2
      exit 41
    fi
    if [ -f "$state_dir/service_${service}" ]; then
      cat "$state_dir/service_${service}"
    fi
    ;;
  inspect)
    [ "${1:-}" = --format ]
    shift 2
    container_id="${1:-}"
    if [ "${FAKE_INSPECT_FAIL_CONTAINER:-}" = "$container_id" ] || [ ! -f "$state_dir/state_${container_id}" ]; then
      echo "inspect failed" >&2
      exit 42
    fi
    cat "$state_dir/state_${container_id}"
    ;;
  *)
    echo "unsupported fake docker operation: $operation" >&2
    exit 43
    ;;
esac
"""


def _writer_gate_source() -> str:
    deploy = _findb_deploy_script()
    start = deploy.index("verify_services_stopped() {")
    return deploy[start : deploy.index("\n\ndiagnose_writer_services()", start)]


def _run_writer_gate(
    tmp_path: Path,
    *,
    listing: str = "",
    states: dict[str, str] | None = None,
    enum_fail_service: str = "",
    inspect_fail_container: str = "",
) -> subprocess.CompletedProcess[str]:
    state_dir = tmp_path / "writer-gate-state"
    state_dir.mkdir(parents=True)
    (state_dir / "service_ingest").write_text(listing, encoding="utf-8")
    for container_id, state in (states or {}).items():
        (state_dir / f"state_{container_id}").write_text(state, encoding="utf-8")

    fake_bin = tmp_path / "writer-gate-bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(_FAKE_WRITER_GATE_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)

    harness = f"""set -euo pipefail
{_writer_gate_source()}
verify_services_stopped ingest dispatcher worker raw-cleanup
"""
    environment = dict(os.environ)
    environment.update(
        PATH=f"{fake_bin}:{environment['PATH']}",
        FAKE_DOCKER_STATE=str(state_dir),
        FAKE_ENUM_FAIL_SERVICE=enum_fail_service,
        FAKE_INSPECT_FAIL_CONTAINER=inspect_fail_container,
    )
    return subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_writer_gate_behavior_accepts_zero_and_all_safe_containers(tmp_path: Path) -> None:
    zero = _run_writer_gate(tmp_path / "zero")
    assert zero.returncode == 0, zero.stderr
    assert "service ingest, container=none, state=stopped" in zero.stdout

    all_safe = _run_writer_gate(
        tmp_path / "all-safe",
        listing="container-one\ncontainer-two\n",
        states={
            "container-one": "exited false false",
            "container-two": "created false false",
        },
    )
    assert all_safe.returncode == 0, all_safe.stderr
    assert "container container-one" in all_safe.stdout
    assert "container container-two" in all_safe.stdout


@pytest.mark.parametrize(
    ("name", "listing", "states", "expected", "enum_fail_service", "inspect_fail_container"),
    (
        (
            "second-running",
            "container-one\ncontainer-two\n",
            {"container-one": "exited false false", "container-two": "running true false"},
            "container container-two, state=running",
            "",
            "",
        ),
        (
            "restarting",
            "container-one\n",
            {"container-one": "running false true"},
            "restarting=true",
            "",
            "",
        ),
        (
            "unknown",
            "container-one\n",
            {"container-one": "paused false false"},
            "state=paused",
            "",
            "",
        ),
        ("dead", "container-one\n", {"container-one": "dead false false"}, "state=dead", "", ""),
        ("inspect-failure", "container-one\n", {}, "state=inspect-failed", "", "container-one"),
        ("enumeration-failure", "", {}, "container enumeration failed", "ingest", ""),
        (
            "multiline-inspect",
            "container-one\n",
            {"container-one": "exited false false\nMALFORMED"},
            "invalid inspect output",
            "",
            "",
        ),
        ("newline-only", "\n", {}, "container=<empty>", "", ""),
        (
            "empty-id",
            "container-one\n\n",
            {"container-one": "exited false false"},
            "container=<empty>",
            "",
            "",
        ),
    ),
)
def test_writer_gate_behavior_fails_closed(
    tmp_path: Path,
    name: str,
    listing: str,
    states: dict[str, str],
    expected: str,
    enum_fail_service: str,
    inspect_fail_container: str,
) -> None:
    completed = _run_writer_gate(
        tmp_path / name,
        listing=listing,
        states=states,
        enum_fail_service=enum_fail_service,
        inspect_fail_container=inspect_fail_container,
    )

    assert completed.returncode != 0
    assert expected in completed.stdout


def test_deploy_uses_ordered_health_checks_with_failure_diagnostics() -> None:
    deploy = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "--wait --wait-timeout" not in deploy
    assert "up -d --remove-orphans" in deploy
    assert "diagnose_services()" in deploy
    assert ".State.OOMKilled" in deploy
    assert "{{json .State.Health}}" in deploy
    assert "docker compose -f docker-compose.prod.yml ps -a" in deploy


def test_dashboard_health_checks_use_the_public_landing_page() -> None:
    deploy = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    compose = PROD_COMPOSE.read_text(encoding="utf-8")

    expected_url = "http://127.0.0.1:3333/dashboard/"
    assert expected_url in deploy
    assert expected_url in compose
    assert "http://127.0.0.1:3333/dashboard/login" not in deploy
    assert "http://127.0.0.1:3333/dashboard/login" not in compose
    assert "Checking public Dashboard routes through nginx" in deploy
    assert "for dashboard_path in /dashboard/ /dashboard/lookup" in deploy


def test_deploy_retries_celery_worker_readiness() -> None:
    deploy = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "worker_ready=0" in deploy
    assert "for attempt in $(seq 1 24)" in deploy
    assert "Celery worker not ready (attempt ${attempt}/24); waiting 5s..." in deploy
    assert "Celery worker did not become ready in time" in deploy
    assert 'if [ "$worker_ready" -ne 1 ]' in deploy


def test_rabbitmq_consumer_timeout_has_one_source_and_is_verified() -> None:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    expected = compose["x-normalization-consumer-timeout"]

    assert compose["x-app-environment"]["NORMALIZATION_CONSUMER_TIMEOUT_MS"] == expected
    assert (
        compose["services"]["rabbitmq-policy"]["environment"]["NORMALIZATION_CONSUMER_TIMEOUT_MS"]
        == expected
    )

    deploy = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    assert "expected_consumer_timeout" in deploy
    assert "actual_consumer_timeout" in deploy
    assert "RabbitMQ consumer timeout policy mismatch" in deploy


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
    }

    assert validate_predeploy_state(state, minimum_connection_headroom=80) == []


def test_predeploy_database_state_reports_every_blocker() -> None:
    state = {
        "duplicate_raw_run_ids": 2,
        "long_transactions_over_5m": 1,
        "connection_headroom": 30,
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
