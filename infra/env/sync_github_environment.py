"""Validate and publish an ignored deployment env source to GitHub."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import dotenv_values

REPOSITORY: Final = "FPI-TW/findb"
ENV_ROOT: Final = Path(__file__).resolve().parent
DEPLOYMENT_TARGETS: Final = ("staging", "production")
ENV_ASSIGNMENT_RE: Final = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


@dataclass(frozen=True)
class ServiceConfig:
    variables: tuple[str, ...]
    secrets: tuple[str, ...]
    optional_secrets: tuple[str, ...] = ()


SERVICE_CONFIGS: Final = {
    "findb": ServiceConfig(
        variables=(
            "APP_NAME",
            "APP_VERSION",
            "DEBUG",
            "PORT",
            "DATABASE_POOL_SIZE",
            "DATABASE_MAX_OVERFLOW",
            "API_V1_PREFIX",
            "API_KEY_HEADER",
            "SOURCE_ALLOWLIST_CIDRS",
            "SOURCE_TRUST_PROXY_HEADERS",
            "SERVE_REQUIRE_AUTH",
            "RATE_LIMIT_REQUESTS",
            "RATE_LIMIT_WINDOW",
            "RAW_RETENTION_ENABLED",
            "RAW_RETENTION_DAYS",
            "FINDB_STATIC_CACHE_BASE_URL",
            "FINDB_LATEST_PRICE_WORKERS",
            "FINDB_PUBLIC_HOST",
            "CLOUDFLARE_R2_ACCOUNT_ID",
            "CLOUDFLARE_R2_CANONICAL_BUCKET",
        ),
        secrets=(
            "FINDB_EC2_HOST",
            "FINDB_EC2_USER",
            "FINDB_EC2_SSH_KEY",
            "DATABASE_URL",
            "ADMIN_BREAK_GLASS_API_KEY",
            "FINDB_QUEUE_HEALTH_ADMIN_API_KEY",
            "FINDB_LOOKUP_SERVE_API_KEY",
            "FINDB_STATIC_CACHE_SERVE_API_KEY",
            "CELERY_BROKER_URL",
            "RABBITMQ_DEFAULT_USER",
            "RABBITMQ_DEFAULT_PASS",
            "RABBITMQ_ERLANG_COOKIE",
            "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID",
            "CLOUDFLARE_R2_CANONICAL_PUBLISHER_SECRET_ACCESS_KEY",
            "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID",
            "CLOUDFLARE_R2_CANONICAL_READER_SECRET_ACCESS_KEY",
        ),
        optional_secrets=(
            "CLOUDFLARE_R2_CANONICAL_PUBLISHER_SESSION_TOKEN",
            "CLOUDFLARE_R2_CANONICAL_READER_SESSION_TOKEN",
        ),
    ),
    "fetcher": ServiceConfig(
        variables=(
            "FETCHER_SCHEDULER_CONTROL_POLL_SECONDS",
            "SHIOAJI_SIMULATION",
            "FETCHER_SOURCE_API_URL",
            "FINDB_SERVE_BASE_URL",
            "FETCHER_CALENDAR_TIMEOUT_SECONDS",
            "FETCHER_CALENDAR_CACHE_TTL_SECONDS",
            "FETCHER_REQUEST_TIMEOUT_SECONDS",
            "FETCHER_MAX_ATTEMPTS",
            "FETCHER_MAX_RETRY_AFTER_SECONDS",
            "TWELVE_DATA_BASE_URL",
            "TWELVE_DATA_TIMEOUT_SECONDS",
            "TWELVE_DATA_MAX_RESPONSE_BYTES",
            "CLOUDFLARE_R2_ACCOUNT_ID",
            "CLOUDFLARE_R2_RAW_BUCKET",
            "CLOUDFLARE_R2_MAX_OBJECT_BYTES",
        ),
        secrets=(
            "FETCHER_EC2_HOST",
            "FETCHER_EC2_USER",
            "FETCHER_EC2_SSH_KEY",
            "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY",
            "FETCHER_FINLAB_SOURCE_CLIENT_KEY",
            "FETCHER_SHIOAJI_SOURCE_CLIENT_KEY",
            "FETCHER_CALENDAR_SERVE_API_KEY",
            "TWELVE_DATA_API_KEY",
            "FINLAB_API_TOKEN",
            "SHIOAJI_API_KEY",
            "SHIOAJI_SECRET_KEY",
            "CLOUDFLARE_R2_RAW_ACCESS_KEY_ID",
            "CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY",
        ),
        optional_secrets=("CLOUDFLARE_R2_RAW_SESSION_TOKEN",),
    ),
}


def _run(
    arguments: list[str],
    *,
    input_text: str | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        check=True,
        input=input_text,
        text=True,
        capture_output=capture_output,
    )


def _duplicate_env_names(source: Path) -> tuple[str, ...]:
    """Return duplicate active assignments without reading or exposing values."""
    names = (
        match.group(1)
        for line in source.read_text(encoding="utf-8").splitlines()
        if (match := ENV_ASSIGNMENT_RE.match(line))
    )
    counts = Counter(names)
    return tuple(sorted(name for name, count in counts.items() if count > 1))


def _ensure_environment(environment: str) -> None:
    endpoint = f"repos/{REPOSITORY}/environments/{environment}"
    existing = subprocess.run(
        ["gh", "api", endpoint],
        check=False,
        text=True,
        capture_output=True,
    )
    if existing.returncode == 0:
        policy = json.loads(existing.stdout).get("deployment_branch_policy") or {}
        if (
            policy.get("protected_branches") is not False
            or policy.get("custom_branch_policies") is not True
        ):
            raise RuntimeError(f"{environment} must use custom deployment branch policies")
    elif "HTTP 404" in existing.stderr:
        payload = json.dumps(
            {
                "deployment_branch_policy": {
                    "protected_branches": False,
                    "custom_branch_policies": True,
                }
            }
        )
        _run(
            [
                "gh",
                "api",
                "--method",
                "PUT",
                endpoint,
                "--input",
                "-",
            ],
            input_text=payload,
        )
    else:
        existing.check_returncode()

    policies = _run(
        [
            "gh",
            "api",
            f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies",
            "--paginate",
        ],
        capture_output=True,
    )
    response = json.loads(policies.stdout)
    branch_policies = response["branch_policies"]
    unexpected = sorted(
        str(policy.get("name") or "<unnamed>")
        for policy in branch_policies
        if policy.get("name") != "main"
    )
    if unexpected:
        raise RuntimeError(
            f"{environment} has unexpected deployment branches: {', '.join(unexpected)}"
        )
    if not branch_policies:
        _run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{REPOSITORY}/environments/{environment}/deployment-branch-policies",
                "-f",
                "name=main",
                "-f",
                "type=branch",
            ]
        )


def _validate_fetcher_bucket_contract(target: str, values: dict[str, str]) -> str | None:
    """Return a secret-free configuration error for the target R2 contract."""
    for forbidden in ("CLOUDFLARE_R2_CANONICAL_BUCKET",):
        if values.get(forbidden):
            return f"{target}-fetcher must not configure {forbidden}"
    return None


def _validate_fetcher_simulation_contract(target: str, values: dict[str, str]) -> str | None:
    """Keep the data-only Shioaji account in simulation mode for every target."""
    if values.get("SHIOAJI_SIMULATION") != "true":
        return f"{target}-fetcher must set SHIOAJI_SIMULATION=true"
    return None


def _validate_findb_canonical_contract(target: str, values: dict[str, str]) -> str | None:
    publisher_id = values.get("CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID", "")
    publisher_secret = values.get("CLOUDFLARE_R2_CANONICAL_PUBLISHER_SECRET_ACCESS_KEY", "")
    reader_id = values.get("CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID", "")
    reader_secret = values.get("CLOUDFLARE_R2_CANONICAL_READER_SECRET_ACCESS_KEY", "")
    if publisher_id and reader_id and publisher_id == reader_id:
        return f"{target}-findb canonical publisher and reader access key IDs must differ"
    if publisher_secret and reader_secret and publisher_secret == reader_secret:
        return f"{target}-findb canonical publisher and reader secrets must differ"
    return None


def _unexpected_names(service: str, values: dict[str, str]) -> tuple[str, ...]:
    config = SERVICE_CONFIGS[service]
    allowed = set((*config.variables, *config.secrets, *config.optional_secrets))
    return tuple(sorted(name for name in values if name not in allowed))


def _remote_environment_names(environment: str) -> tuple[str, ...]:
    names: set[str] = set()
    for endpoint in (
        f"repos/{REPOSITORY}/environments/{environment}/variables?per_page=100",
        f"repos/{REPOSITORY}/environments/{environment}/secrets?per_page=100",
    ):
        completed = subprocess.run(
            ["gh", "api", endpoint],
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            if "HTTP 404" in completed.stderr:
                return ()
            completed.check_returncode()
        payload = json.loads(completed.stdout)
        for item in payload.get("variables", payload.get("secrets", [])):
            name = str(item.get("name") or "")
            if name:
                names.add(name)
    return tuple(sorted(names))


def _unexpected_remote_names(service: str, environment: str) -> tuple[str, ...]:
    config = SERVICE_CONFIGS[service]
    allowed = set((*config.variables, *config.secrets, *config.optional_secrets))
    return tuple(name for name in _remote_environment_names(environment) if name not in allowed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=DEPLOYMENT_TARGETS)
    parser.add_argument("service", choices=sorted(SERVICE_CONFIGS))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create/update the GitHub Environment after validation.",
    )
    arguments = parser.parse_args()

    config = SERVICE_CONFIGS[arguments.service]
    environment = f"{arguments.target}-{arguments.service}"
    source = ENV_ROOT / arguments.target / arguments.service / ".env.remote"
    if not source.is_file():
        parser.error(f"missing ignored source: {source}")

    duplicate_names = _duplicate_env_names(source)
    if duplicate_names:
        print(f"{environment} has duplicate names: {', '.join(duplicate_names)}")
        return 1

    values = {key: str(value or "").strip() for key, value in dotenv_values(source).items()}
    unexpected = _unexpected_names(arguments.service, values)
    if unexpected:
        print(f"{environment} has unsupported names: {', '.join(unexpected)}")
        return 1
    required_secrets = list(config.secrets)
    if arguments.service == "findb" and values.get("SERVE_REQUIRE_AUTH") == "false":
        required_secrets = [
            name
            for name in required_secrets
            if name
            not in {
                "FINDB_LOOKUP_SERVE_API_KEY",
                "FINDB_STATIC_CACHE_SERVE_API_KEY",
            }
        ]
    if arguments.service == "fetcher":
        isolated_credentials = (
            values.get("FETCHER_CALENDAR_SERVE_API_KEY"),
            values.get("FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY"),
            values.get("FETCHER_FINLAB_SOURCE_CLIENT_KEY"),
            values.get("FETCHER_SHIOAJI_SOURCE_CLIENT_KEY"),
        )
        configured_credentials = tuple(value for value in isolated_credentials if value)
        if len(configured_credentials) != len(set(configured_credentials)):
            print("Calendar Serve and provider Source credentials must be distinct")
            return 1
        bucket_contract_error = _validate_fetcher_bucket_contract(arguments.target, values)
        if bucket_contract_error:
            print(bucket_contract_error)
            return 1
        simulation_contract_error = _validate_fetcher_simulation_contract(arguments.target, values)
        if simulation_contract_error:
            print(simulation_contract_error)
            return 1
    if arguments.service == "findb":
        canonical_contract_error = _validate_findb_canonical_contract(arguments.target, values)
        if canonical_contract_error:
            print(canonical_contract_error)
            return 1
    variables = config.variables
    missing = sorted(name for name in (*variables, *required_secrets) if not values.get(name))
    if (
        arguments.service == "findb"
        and values.get("SERVE_REQUIRE_AUTH") == "true"
        and values.get("FINDB_LOOKUP_SERVE_API_KEY")
        == values.get("FINDB_STATIC_CACHE_SERVE_API_KEY")
    ):
        print("Serve lookup and static-cache credentials must be different")
        return 1
    print(f"service: {arguments.service}")
    print(f"environment: {environment}")
    print(f"source: {source}")
    print(f"variables ({len(variables)}): {', '.join(variables)}")
    print(f"secrets ({len(config.secrets)}): {', '.join(config.secrets)}")
    if missing:
        print(f"missing required values: {', '.join(missing)}")
        return 1
    if not arguments.apply:
        print("validation passed; rerun with --apply to publish")
        return 0

    unexpected_remote = _unexpected_remote_names(arguments.service, environment)
    if unexpected_remote:
        print(f"{environment} has unsupported remote names: {', '.join(unexpected_remote)}")
        return 1

    _ensure_environment(environment)
    # This synchronizer only creates or updates the target contract. It never
    # deletes remote variables or secrets; operators must remove retired names
    # explicitly after validating the target-specific predeploy gate.
    for name in variables:
        _run(
            [
                "gh",
                "variable",
                "set",
                name,
                "--env",
                environment,
                "--body",
                values[name],
            ]
        )
        print(f"set variable: {name}")
    for name in config.secrets:
        if not values.get(name):
            continue
        _run(
            ["gh", "secret", "set", name, "--env", environment],
            input_text=values[name],
        )
        print(f"set secret: {name}")
    for name in config.optional_secrets:
        if values.get(name):
            _run(
                ["gh", "secret", "set", name, "--env", environment],
                input_text=values[name],
            )
            print(f"set optional secret: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
