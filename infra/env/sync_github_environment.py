"""Validate and publish an ignored deployment env source to GitHub."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dotenv import dotenv_values

REPOSITORY: Final = "FPI-TW/findb"
ENV_ROOT: Final = Path(__file__).resolve().parent
DEPLOYMENT_TARGETS: Final = ("staging", "production")


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
            "CLOUDFLARE_R2_ACCOUNT_ID",
            "CLOUDFLARE_R2_BUCKET",
        ),
        secrets=(
            "FINDB_EC2_HOST",
            "FINDB_EC2_USER",
            "FINDB_EC2_SSH_KEY",
            "DATABASE_URL",
            "ADMIN_BREAK_GLASS_API_KEY",
            "FINDB_LOOKUP_SERVE_API_KEY",
            "FINDB_STATIC_CACHE_SERVE_API_KEY",
            "CELERY_BROKER_URL",
            "RABBITMQ_DEFAULT_USER",
            "RABBITMQ_DEFAULT_PASS",
            "RABBITMQ_ERLANG_COOKIE",
            "CLOUDFLARE_R2_CONFIG_READ_API_TOKEN",
            "CLOUDFLARE_R2_ACCESS_KEY_ID",
            "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        ),
        optional_secrets=(
            "SOURCE_API_KEY",
            "SERVE_API_KEYS",
            "ADMIN_API_KEY",
            "CLOUDFLARE_R2_SESSION_TOKEN",
        ),
    ),
    "fetcher": ServiceConfig(
        variables=(
            "FETCHER_SOURCE_API_URL",
            "FETCHER_REQUEST_TIMEOUT_SECONDS",
            "FETCHER_MAX_ATTEMPTS",
            "FETCHER_MAX_RETRY_AFTER_SECONDS",
            "TWELVE_DATA_BASE_URL",
            "TWELVE_DATA_TIMEOUT_SECONDS",
            "TWELVE_DATA_MAX_RESPONSE_BYTES",
            "CLOUDFLARE_R2_ACCOUNT_ID",
            "CLOUDFLARE_R2_BUCKET",
            "CLOUDFLARE_R2_PREFIX",
            "CLOUDFLARE_R2_MAX_OBJECT_BYTES",
        ),
        secrets=(
            "FETCHER_EC2_HOST",
            "FETCHER_EC2_USER",
            "FETCHER_EC2_SSH_KEY",
            "FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY",
            "TWELVE_DATA_API_KEY",
            "CLOUDFLARE_R2_ACCESS_KEY_ID",
            "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        ),
        optional_secrets=("CLOUDFLARE_R2_SESSION_TOKEN",),
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

    values = {key: str(value or "").strip() for key, value in dotenv_values(source).items()}
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
    missing = sorted(
        name for name in (*config.variables, *required_secrets) if not values.get(name)
    )
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
    print(f"variables ({len(config.variables)}): {', '.join(config.variables)}")
    print(f"secrets ({len(config.secrets)}): {', '.join(config.secrets)}")
    if missing:
        print(f"missing required values: {', '.join(missing)}")
        return 1
    if not arguments.apply:
        print("validation passed; rerun with --apply to publish")
        return 0

    _ensure_environment(environment)
    for name in config.variables:
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
