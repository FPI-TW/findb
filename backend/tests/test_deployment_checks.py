"""Tests for deployment gates."""

import hashlib
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
FETCHER_SHUTDOWN_BOOTSTRAP_TAG = "7915ea856b677a67776476cd87cb531617203ed0"
FETCHER_SHUTDOWN_BOOTSTRAP_IMAGE_IDS = {
    "twelve": "sha256:fab4b2ac7b43a1637d53d9aa60380d3d7720904b9250fd869ea292c4d43728a2",
    "finlab": "sha256:42b268f067da6d5e8be046f2418b16e59258764d3a32f671794775519240f4a8",
    "shioaji": "sha256:828ed5d1a38063b9f511ae3cfd74509a1b8b4d6c08d700c0cbc0cdc0d4c2077d",
}


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
        if service == "fetcher":
            assert jobs["deploy"]["needs"] == [
                "build-push",
                "validate-r2-bucket-configuration",
                "validate-fetcher-credential-isolation",
            ]
        else:
            assert jobs["deploy"]["needs"] == "build-push"
        assert jobs["deploy"]["environment"] == environment
        assert workflow["concurrency"]["group"] == environment
        assert workflow["concurrency"]["cancel-in-progress"] == "false"
        target_input = workflow["on"]["workflow_dispatch"]["inputs"]["deployment_target"]
        assert target_input["default"] == "staging"
        assert target_input["options"] == ["staging", "production"]


def test_fetcher_finlab_smoke_symbols_remain_a_quoted_comma_delimited_choice() -> None:
    workflow = _load_workflow(FETCHER_CD_WORKFLOW)
    symbols_input = workflow["on"]["workflow_dispatch"]["inputs"]["finlab_symbols"]
    assert symbols_input["default"] == "2330,2317"
    assert symbols_input["options"] == ["2330,2317"]

    source = FETCHER_CD_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r'^\s+default: "2330,2317"$', source, re.MULTILINE)
    assert re.search(r'^\s+- "2330,2317"$', source, re.MULTILINE)


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
    serve_env = parsed_compose["services"]["serve"]["environment"]
    worker_env = parsed_compose["services"]["worker"]["environment"]
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
    assert workflow["env"]["FETCHER_IMAGE"] == "ghcr.io/fpi-tw/findb-fetcher"
    assert workflow["env"]["FETCHER_FINLAB_IMAGE"] == "ghcr.io/fpi-tw/findb-fetcher-finlab"
    assert workflow["env"]["FETCHER_SHIOAJI_IMAGE"] == "ghcr.io/fpi-tw/findb-fetcher-shioaji"
    assert workflow["env"]["FETCHER_IMAGE_TAG"] == "${{ github.sha }}"

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
        step = _named_step(workflow, "build-push", name)
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
        assert FETCHER_SHUTDOWN_BOOTSTRAP_TAG in graceful_gate
        assert FETCHER_SHUTDOWN_BOOTSTRAP_IMAGE_IDS[provider] in graceful_gate
        assert '[ "$stable_exit_code" -eq 137 ]' in graceful_gate
        assert '[ "$stable_image_ref" = "$bootstrap_ref" ]' in graceful_gate
        assert '[ "$stable_image_id" = "$bootstrap_id" ]' in graceful_gate
        assert '[ "$image" != "$bootstrap_ref" ]' in graceful_gate
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
        "Run bounded FinLab acquisition smoke on Fetcher staging EC2",
    )
    script = step["with"]["script"]

    image_prune = script.index("docker image prune -af")
    image_pull = script.index('docker pull "$image"')
    assert image_prune < image_pull
    assert script.count("docker image prune -af") == 1
    assert "docker system prune" not in script
    assert "docker volume prune" not in script


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
            "findb-fetch-finlab-scheduler --schedule-file /app/configs/daily_scheduler.v2.json --slot-id tw_1430 --dataset-key tw_equity_eod",
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
      *Config.Image*)
        if [ "$name" = "${FAKE_STABLE_NAME:-}" ] || [ "$name" = "${FAKE_PREVIOUS_NAME:-}" ]; then
          printf '%s\\n' "${FAKE_STABLE_IMAGE:-${FAKE_IMAGE:?}}"
        else
          printf '%s\\n' "${FAKE_IMAGE:?}"
        fi
        ;;
      *Image*) printf '%s\\n' "${FAKE_STABLE_IMAGE_ID:-sha256:unknown}" ;;
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
    legacy_raw_bucket_marker: bool = False,
    stable_exit_code: int = 0,
    stable_image_ref: str | None = None,
    stable_image_id: str = "sha256:unknown",
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
    if not legacy_raw_bucket_marker:
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
        FAKE_STABLE_IMAGE=stable_image_ref or image,
        FAKE_STABLE_IMAGE_ID=stable_image_id,
        FAKE_STABLE_NAME=stable,
        FAKE_PREVIOUS_NAME=previous,
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


def test_twelve_legacy_raw_bucket_marker_is_migrated_before_reconciliation(
    tmp_path: Path,
) -> None:
    provider = _fetcher_scheduler_cases()[0]
    stable = provider[3]

    completed, statuses, _, durable_state = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "running"},
        legacy_raw_bucket_marker=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert statuses == {stable: "running"}
    assert durable_state == "durable-state\n"
    marker = (tmp_path / "twelve" / "state" / "raw-bucket.sha256").read_text(encoding="utf-8")
    expected = hashlib.sha256(b"0123456789abcdef0123456789abcdef\nraw-bucket\ntwelve").hexdigest()
    assert marker == f"{expected}\n"


@pytest.mark.parametrize("provider", _fetcher_scheduler_cases()[1:], ids=lambda item: item[0])
def test_new_providers_reject_legacy_twelve_raw_bucket_marker(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
) -> None:
    stable = provider[3]

    completed, statuses, _, durable_state = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "running"},
        legacy_raw_bucket_marker=True,
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
@pytest.mark.parametrize(
    ("matching_ref", "matching_id"),
    ((False, False), (True, False), (False, True)),
    ids=("unknown", "wrong-image-id", "wrong-image-ref"),
)
def test_fetcher_provider_forced_stop_blocks_promotion_and_restores_stable(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
    matching_ref: bool,
    matching_id: bool,
) -> None:
    provider_id, _, _, stable, candidate, previous = provider
    completed, statuses, operations, durable_state = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "running"},
        stable_exit_code=137,
        stable_image_ref=(
            f"image:{FETCHER_SHUTDOWN_BOOTSTRAP_TAG}" if matching_ref else "image:unknown"
        ),
        stable_image_id=(
            FETCHER_SHUTDOWN_BOOTSTRAP_IMAGE_IDS[provider_id] if matching_id else "sha256:unknown"
        ),
    )

    assert completed.returncode != 0
    assert "graceful stop failed" in completed.stderr.lower()
    assert statuses == {stable: "running"}
    assert candidate not in statuses
    assert previous not in statuses
    assert "start" in operations
    assert durable_state == "durable-state\n"


@pytest.mark.parametrize("provider", _fetcher_scheduler_cases(), ids=lambda item: item[0])
def test_fetcher_provider_accepts_reviewed_shutdown_bootstrap_once(
    tmp_path: Path,
    provider: tuple[str, str, str, str, str, str],
) -> None:
    provider_id, _, _, stable, candidate, previous = provider
    completed, statuses, operations, durable_state = _run_fetcher_reconciliation(
        tmp_path,
        provider,
        initial={stable: "running"},
        stable_exit_code=137,
        stable_image_ref=f"image:{FETCHER_SHUTDOWN_BOOTSTRAP_TAG}",
        stable_image_id=FETCHER_SHUTDOWN_BOOTSTRAP_IMAGE_IDS[provider_id],
    )

    assert completed.returncode == 0, completed.stderr
    assert "shutdown bootstrap" in completed.stdout.lower()
    assert statuses == {stable: "running"}
    assert candidate not in statuses
    assert previous not in statuses
    assert "run" in operations
    assert "start" not in operations
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
        "validate-r2-bucket-configuration",
        "validate-fetcher-credential-isolation",
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
