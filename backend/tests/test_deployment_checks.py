"""Tests for deployment gates."""

import os
import re
import runpy
import subprocess
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
FINDB_CI_WORKFLOW = WORKFLOWS_ROOT / "findb-ci.yml"
FINDB_CD_WORKFLOW = WORKFLOWS_ROOT / "findb-cd.yml"
FETCHER_CI_WORKFLOW = WORKFLOWS_ROOT / "fetcher-ci.yml"
FETCHER_CD_WORKFLOW = WORKFLOWS_ROOT / "fetcher-cd.yml"
DEPLOY_WORKFLOW = FINDB_CD_WORKFLOW
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
ENV_CONFIG_ROOT = REPO_ROOT / "infra" / "env"
ENV_SYNC_SCRIPT = ENV_CONFIG_ROOT / "sync_github_environment.py"


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
        "findb-ci.yml",
        "findb-cd.yml",
        "fetcher-ci.yml",
        "fetcher-cd.yml",
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
        assert jobs["build-push"]["needs"] == "verify"
        assert jobs["deploy"]["needs"] == "build-push"
        assert jobs["deploy"]["environment"] == environment
        assert workflow["concurrency"]["group"] == environment
        assert workflow["concurrency"]["cancel-in-progress"] == "false"
        target_input = workflow["on"]["workflow_dispatch"]["inputs"]["deployment_target"]
        assert target_input["default"] == "staging"
        assert target_input["options"] == ["staging", "production"]


def test_main_push_runs_each_ci_workflow_only_through_its_cd_gate() -> None:
    for ci_path, cd_path, reusable_path in (
        (FINDB_CI_WORKFLOW, FINDB_CD_WORKFLOW, "./.github/workflows/findb-ci.yml"),
        (FETCHER_CI_WORKFLOW, FETCHER_CD_WORKFLOW, "./.github/workflows/fetcher-ci.yml"),
    ):
        ci_workflow = _load_workflow(ci_path)
        ci_triggers = ci_workflow["on"]
        assert "push" not in ci_triggers
        assert "pull_request" in ci_triggers
        assert "workflow_call" in ci_triggers

        cd_workflow = _load_workflow(cd_path)
        assert cd_workflow["on"]["push"]["branches"] == ["main"]
        assert cd_workflow["jobs"]["verify"]["uses"] == reusable_path


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
            documented_names = set((*config.variables, *config.secrets, *config.optional_secrets))
            assert configured_names == documented_names


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
    for path in (FINDB_CI_WORKFLOW, FETCHER_CI_WORKFLOW):
        workflow = _load_workflow(path)
        triggers = workflow["on"]
        assert "contracts/**" in triggers["pull_request"]["paths"]
        assert "workflow_call" in triggers

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
    fetcher_ci = _load_workflow(FETCHER_CI_WORKFLOW)

    assert required_paths <= set(fetcher_ci["on"]["pull_request"]["paths"])


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

    findb_ci = _load_workflow(FINDB_CI_WORKFLOW)
    assert required_paths <= set(findb_ci["on"]["pull_request"]["paths"])

    findb_cd = _load_workflow(FINDB_CD_WORKFLOW)
    assert required_paths <= set(findb_cd["on"]["push"]["paths"])


def test_service_ci_and_cd_triggers_cover_deployment_units_without_cross_deploy() -> None:
    findb_ci = _load_workflow(FINDB_CI_WORKFLOW)
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
    assert required_findb_ci_paths <= set(findb_ci["on"]["pull_request"]["paths"])

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
        ".github/workflows/fetcher-cd.yml",
    }


def test_root_context_images_use_service_specific_dockerignore_files() -> None:
    assert not (REPO_ROOT / ".dockerignore").exists()

    findb_cd = _load_workflow(FINDB_CD_WORKFLOW)
    dashboard_build = _named_step(findb_cd, "build-push", "Build and push dashboard image")
    assert dashboard_build["with"]["context"] == "."
    assert dashboard_build["with"]["file"] == "dashboard/Dockerfile"
    backend_build = _named_step(findb_cd, "build-push", "Build and push backend image")
    assert backend_build["with"]["context"] == "./backend"
    assert "${{ env.IMAGE }}:${{ github.sha }}" in backend_build["with"]["tags"]
    assert "${{ env.DASHBOARD_IMAGE }}:${{ github.sha }}" in dashboard_build["with"]["tags"]

    fetcher_cd = _load_workflow(FETCHER_CD_WORKFLOW)
    fetcher_build = _named_step(fetcher_cd, "build-push", "Build and push Fetcher image")
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
        for reference_path in _secret_reference_paths(workflow):
            referenced_value: object = workflow
            for component in reference_path:
                if isinstance(referenced_value, list):
                    referenced_value = referenced_value[int(component)]
                else:
                    referenced_value = referenced_value[component]
            if referenced_value == "${{ secrets.GITHUB_TOKEN }}":
                continue
            assert reference_path[:2] == ("jobs", "deploy")

    for path in (FINDB_CI_WORKFLOW, FETCHER_CI_WORKFLOW):
        assert _secret_reference_paths(_load_workflow(path)) == []


def test_findb_deployment_uses_dedicated_credentials_and_keeps_legacy_optional() -> None:
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
    assert "SERVE_API_KEYS must be configured" not in validation_script

    required_loop = next(line for line in validation_script.splitlines() if "for name in " in line)
    for legacy_name in (
        "SOURCE_API_KEY",
        "SERVE_API_KEYS",
        "ADMIN_API_KEY",
    ):
        assert legacy_name not in required_loop

    assert render["env"]["FINDB_LOOKUP_SERVE_API_KEY"] == (
        "${{ secrets.FINDB_LOOKUP_SERVE_API_KEY }}"
    )
    assert '--key "${FINDB_LOOKUP_SERVE_API_KEY:-}"' in render["run"]
    assert "--keys" not in render["run"]

    forwarded = set(deploy["with"]["envs"].split(","))
    assert "ADMIN_BREAK_GLASS_API_KEY" in forwarded
    assert "FINDB_LOOKUP_SERVE_API_KEY" not in forwarded

    compose = PROD_COMPOSE.read_text(encoding="utf-8")
    assert (
        'ADMIN_BREAK_GLASS_API_KEY: "${ADMIN_BREAK_GLASS_API_KEY:'
        "?ADMIN_BREAK_GLASS_API_KEY must be set"
    ) in compose
    assert 'SOURCE_API_KEY: "${SOURCE_API_KEY:-}"' in compose
    assert 'ADMIN_API_KEY: "${ADMIN_API_KEY:-}"' in compose
    assert "DASHBOARD_USERNAME" not in compose
    assert "DASHBOARD_PASSWORD" not in compose


def test_cd_workflows_do_not_reference_cross_service_credentials() -> None:
    findb_cd = FINDB_CD_WORKFLOW.read_text(encoding="utf-8")
    fetcher_cd = FETCHER_CD_WORKFLOW.read_text(encoding="utf-8")

    assert "FINDB_EC2_HOST" in findb_cd
    assert "FETCHER_EC2_HOST" not in findb_cd
    assert "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY" not in findb_cd
    assert "PROVIDER_" not in findb_cd

    assert "FETCHER_EC2_HOST" in fetcher_cd
    assert "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY" in fetcher_cd
    assert "FETCHER_SOURCE_CLIENT_KEY" not in fetcher_cd
    for forbidden in (
        "FINDB_EC2_",
        "DATABASE_URL",
        "CELERY_BROKER_URL",
        "RABBITMQ_",
        "ADMIN_API_KEY",
        "DASHBOARD_PASSWORD",
        "DASHBOARD_SESSION_SECRET",
        "SERVE_API_KEYS",
    ):
        assert forbidden not in fetcher_cd

    assert "secrets: inherit" not in findb_cd
    assert "secrets: inherit" not in fetcher_cd


def test_fetcher_cd_runs_one_durable_scheduler_with_isolated_runtime_env() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    step = _named_step(
        workflow,
        "deploy",
        "Release and validate Fetcher image on Fetcher EC2",
    )
    step_env = step["env"]
    script = step["with"]["script"]
    forwarded = set(step["with"]["envs"].split(","))

    required_environment_values = {
        "FETCHER_SOURCE_API_URL",
        "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY",
        "TWELVE_DATA_API_KEY",
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_BUCKET",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    }
    assert required_environment_values <= set(step_env)
    assert required_environment_values <= forwarded
    assert step_env["CLOUDFLARE_R2_SESSION_TOKEN"] == ("${{ secrets.CLOUDFLARE_R2_SESSION_TOKEN }}")

    assert 'image="${FETCHER_IMAGE}:${FETCHER_IMAGE_TAG}"' in script
    assert 'docker pull "$image"' in script
    assert "state_dir=/var/lib/findb-fetcher" in script
    assert 'sudo chown 10001:10001 "$state_dir"' in script
    assert 'sudo chmod 0700 "$state_dir"' in script
    assert "stat -c '%u:%g'" in script
    assert "stat -c '%a'" in script
    assert "--user 10001:10001" in script
    assert '--mount "type=bind,src=$state_dir,dst=/var/lib/findb-fetcher"' in script
    assert "--restart unless-stopped" in script
    assert "--read-only" in script
    assert "--cap-drop ALL" in script
    assert "--security-opt no-new-privileges" in script
    assert "--log-opt max-size=10m" in script
    assert "--log-opt max-file=3" in script

    preflight = script.index("findb-fetch-scheduler --check")
    stop_old = script.index('docker stop --time 30 "$stable"')
    start_candidate = script.index("findb-fetch-scheduler --run-forever")
    promote = script.rindex('docker rename "$candidate" "$stable"')
    assert preflight < stop_old < start_candidate < promote
    assert "stable=findb-fetcher-scheduler" in script
    assert "candidate=findb-fetcher-scheduler-candidate" in script
    assert "previous=findb-fetcher-scheduler-previous" in script
    assert 'docker rm -f "$candidate"' in script
    assert 'docker rename "$previous" "$stable"' in script
    assert 'docker start "$stable"' in script
    assert "for attempt in $(seq 1 6)" in script
    assert "{{.RestartCount}}" in script
    assert "recover_scheduler()" in script

    recovery_start = script.index("recover_scheduler()")
    recovery_end = script.index("trap 'recover_scheduler \"$?\"' ERR")
    recovery = script[recovery_start:recovery_end]
    assert recovery_start < recovery_end < stop_old
    assert "trap - ERR INT TERM HUP" in recovery
    assert recovery.index('docker rm -f "$candidate"') < recovery.index(
        'docker container inspect "$stable"'
    )
    assert 'if docker container inspect "$stable"' in recovery
    assert 'docker start "$stable"' in recovery
    assert 'elif docker container inspect "$previous"' in recovery
    assert 'docker rename "$previous" "$stable"' in recovery
    assert '"$had_previous"' not in recovery
    assert 'exit "$original_status"' in recovery
    assert "Scheduler recovery did not complete" in recovery
    assert "trap 'recover_scheduler \"$?\"' ERR" in script
    assert "trap 'recover_scheduler 130' INT" in script
    assert "trap 'recover_scheduler 143' TERM" in script
    assert "trap 'recover_scheduler 129' HUP" in script
    clear_recovery = script.rindex("trap - ERR INT TERM HUP")
    assert promote < clear_recovery
    assert "{{.State.Running}}" in script

    runtime_block = script[script.index('runtime_env_args="') : preflight]
    assert "--env GITHUB_TOKEN" not in runtime_block
    assert "--env GITHUB_ACTOR" not in runtime_block
    for name in (
        "SOURCE_API_URL",
        "SOURCE_CLIENT_KEY",
        "TWELVE_DATA_API_KEY",
        "CLOUDFLARE_R2_ACCOUNT_ID",
        "CLOUDFLARE_R2_BUCKET",
        "CLOUDFLARE_R2_ACCESS_KEY_ID",
        "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    ):
        assert f"--env {name}" in runtime_block
        assert f"--env {name}=" not in runtime_block
    assert ".Config.Env" not in script
    assert "set -x" not in script


@pytest.mark.parametrize(
    ("initial", "succeeds", "expected_running"),
    [
        ({"findb-fetcher-scheduler": "stopped"}, True, 1),
        (
            {
                "findb-fetcher-scheduler": "running",
                "findb-fetcher-scheduler-candidate": "running",
            },
            True,
            1,
        ),
        (
            {
                "findb-fetcher-scheduler": "stopped",
                "findb-fetcher-scheduler-previous": "stopped",
            },
            True,
            1,
        ),
        ({"findb-fetcher-scheduler-candidate": "running"}, True, 1),
        ({"findb-fetcher-scheduler-previous": "stopped"}, True, 1),
        ({"findb-fetcher-scheduler": "start-fail"}, False, 0),
        ({}, True, 0),
    ],
)
def test_fetcher_scheduler_reconciliation_behavior(
    tmp_path: Path,
    initial: dict[str, str],
    succeeds: bool,
    expected_running: int,
) -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    script = _named_step(
        workflow,
        "deploy",
        "Release and validate Fetcher image on Fetcher EC2",
    )["with"]["script"]
    begin = "# BEGIN FETCHER SCHEDULER RECONCILIATION"
    end = "# END FETCHER SCHEDULER RECONCILIATION"
    reconciliation = script.split(begin, 1)[1].split(end, 1)[0]
    assert script.index(end) < script.index('docker pull "$image"')

    state_dir = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    state_dir.mkdir()
    fake_bin.mkdir()
    for name, status in initial.items():
        (state_dir / name).write_text(status, encoding="utf-8")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
set -eu
operation="$1"
shift
case "$operation" in
  container)
    [ "$1" = "inspect" ]
    [ -f "$FAKE_DOCKER_STATE/$2" ]
    ;;
  rm)
    [ "$1" = "-f" ]
    rm -f "$FAKE_DOCKER_STATE/$2"
    ;;
  rename)
    mv "$FAKE_DOCKER_STATE/$1" "$FAKE_DOCKER_STATE/$2"
    ;;
  start)
    if [ "$(cat "$FAKE_DOCKER_STATE/$1")" = "start-fail" ]; then
      exit 1
    fi
    printf 'running\\n' > "$FAKE_DOCKER_STATE/$1"
    ;;
  inspect)
    [ "$1" = "--format" ]
    status="$(cat "$FAKE_DOCKER_STATE/$3")"
    if [ "$status" = "running" ]; then
      printf 'true\\n'
    else
      printf 'false\\n'
    fi
    ;;
  *)
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    harness = f"""
set -euo pipefail
stable=findb-fetcher-scheduler
candidate=findb-fetcher-scheduler-candidate
previous=findb-fetcher-scheduler-previous
{reconciliation}
"""
    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["FAKE_DOCKER_STATE"] = str(state_dir)

    completed = subprocess.run(
        ["bash", "-c", harness],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert (completed.returncode == 0) is succeeds
    statuses = {path.name: path.read_text(encoding="utf-8").strip() for path in state_dir.iterdir()}
    assert sum(status == "running" for status in statuses.values()) == expected_running
    assert "findb-fetcher-scheduler-candidate" not in statuses
    if succeeds:
        assert "findb-fetcher-scheduler-previous" not in statuses
        if expected_running == 1:
            assert statuses == {"findb-fetcher-scheduler": "running"}
        else:
            assert statuses == {}
    else:
        assert "Scheduler state could not be reconciled" in completed.stderr


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
    assert "for dashboard_path in /dashboard/ /dashboard/lookup /dashboard/skill" in deploy


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
