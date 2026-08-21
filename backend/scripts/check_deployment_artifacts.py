"""Run deterministic checks for repository deployment artifacts.

This command intentionally has no access to deployment credentials or remote
systems.  It validates the checked-in workflow, infrastructure, and Compose
contracts and renders both Compose files with bounded CI-only values.
"""

from __future__ import annotations

import os
import re
import runpy
import subprocess
import sys
from pathlib import Path

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
WORKFLOWS_ROOT = REPO_ROOT / ".github" / "workflows"
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
LOCAL_COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_CONFIG_ROOT = REPO_ROOT / "infra" / "env"


class UniqueKeyLoader(yaml.BaseLoader):
    """Parse YAML without YAML 1.1 coercion and reject duplicate keys."""


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


def _load_workflow(path: Path) -> dict[object, object]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(REPO_ROOT)} must contain a mapping")
    return value


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


def check_workflows() -> None:
    paths = sorted((*WORKFLOWS_ROOT.glob("*.yml"), *WORKFLOWS_ROOT.glob("*.yaml")))
    if not paths:
        raise ValueError("no GitHub Actions workflow files found")
    for path in paths:
        workflow = _load_workflow(path)
        for reference in _uses_references(workflow):
            if reference.startswith("./"):
                continue
            if not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference):
                raise ValueError(
                    f"{path.relative_to(REPO_ROOT)} contains an unpinned action: {reference}"
                )


def _compose_variables(compose_text: str) -> set[str]:
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)(?=[:}])", compose_text))


def _compose_ci_environment(compose_text: str) -> dict[str, str]:
    values = {name: "ci-check" for name in _compose_variables(compose_text)}
    values.update(
        {
            "IMAGE_TAG": "ci-check",
            "DASHBOARD_IMAGE": "ghcr.io/example/findb-dashboard",
            "FINDB_IMAGE_REFERENCE": (
                "ghcr.io/example/findb@sha256:"
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
            "DASHBOARD_IMAGE_REFERENCE": (
                "ghcr.io/example/findb-dashboard@sha256:"
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            ),
            "NGINX_IMAGE_REFERENCE": (
                "nginx@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
            ),
            "APP_NAME": "findb",
            "APP_VERSION": "ci-check",
            "DEBUG": "false",
            "PORT": "8080",
            "DATABASE_URL": "postgresql+asyncpg://findb:findb@db:5432/findb",
            "CELERY_BROKER_URL": "amqp://findb:findb@rabbitmq:5672/%2Ffindb",
            "RABBITMQ_DEFAULT_USER": "findb",
            "RABBITMQ_DEFAULT_PASS": "ci-check-password",
            "RABBITMQ_ERLANG_COOKIE": "ci-check-cookie",
            "CLOUDFLARE_R2_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
            "CLOUDFLARE_R2_CANONICAL_BUCKET": "findb-ci-canonical",
            "CLOUDFLARE_R2_CANONICAL_READER_ACCESS_KEY_ID": "ci-reader-key",
            "CLOUDFLARE_R2_CANONICAL_READER_SECRET_ACCESS_KEY": "ci-reader-secret",
            "CLOUDFLARE_R2_CANONICAL_PUBLISHER_ACCESS_KEY_ID": "ci-publisher-key",
            "CLOUDFLARE_R2_CANONICAL_PUBLISHER_SECRET_ACCESS_KEY": "ci-publisher-secret",
        }
    )
    return values


def _render_compose(path: Path) -> None:
    environment = os.environ.copy()
    environment.update(_compose_ci_environment(path.read_text(encoding="utf-8")))
    completed = subprocess.run(
        ["docker", "compose", "-f", str(path), "config", "--quiet"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"docker compose config failed for {path.name}: {detail}")


def check_compose() -> None:
    for path in (LOCAL_COMPOSE, PROD_COMPOSE):
        yaml.safe_load(path.read_text(encoding="utf-8"))
        _render_compose(path)

    compose_text = PROD_COMPOSE.read_text(encoding="utf-8")
    parsed = yaml.safe_load(compose_text)
    services = parsed.get("services", {}) if isinstance(parsed, dict) else {}
    if not isinstance(services, dict) or not services:
        raise ValueError("docker-compose.prod.yml must define services")
    if "db" in services or any(
        isinstance(service.get("image"), str)
        and service["image"].lower().startswith(("postgres", "postgresql"))
        for service in services.values()
        if isinstance(service, dict)
    ):
        raise ValueError("production Compose must not run a PostgreSQL container")
    for service_name, service in services.items():
        if not isinstance(service, dict):
            raise ValueError(f"production service {service_name!r} must be a mapping")
        if "build" in service:
            raise ValueError(f"production service {service_name!r} must use a published image")
        if service_name != "rabbitmq-policy" and "restart" not in service:
            raise ValueError(f"production service {service_name!r} must define restart policy")
        image = service.get("image")
        if isinstance(image, str) and (":latest" in image or ":-latest" in image):
            raise ValueError(f"production service {service_name!r} has a latest image fallback")
        if isinstance(image, str):
            if "_IMAGE_REFERENCE:?" not in image and "@sha256:" not in image:
                raise ValueError(
                    f"production service {service_name!r} must use an immutable image reference"
                )


def check_infra_rendering() -> None:
    sys.path.insert(0, str(BACKEND_ROOT))
    from scripts.render_nginx_cloudflare_real_ip import render_cloudflare_real_ip
    from scripts.render_nginx_public_host import render_public_host
    from scripts.render_nginx_serve_key import referer_regex_for_host, render_serve_key
    from scripts.render_nginx_source_allowlist import render_source_allowlist

    template = (REPO_ROOT / "infra/nginx/nginx.conf").read_text(encoding="utf-8")
    rendered_host = render_public_host(template, "staging.example.com")
    if "__FINDB_PUBLIC_HOST__" in rendered_host:
        raise ValueError("nginx public-host placeholder was not rendered")
    if rendered_host != render_public_host(template, "staging.example.com"):
        raise ValueError("nginx public-host rendering is not deterministic")

    source = render_source_allowlist("10.0.0.0/8,10.0.0.0/8")
    if source != render_source_allowlist("10.0.0.0/8,10.0.0.0/8"):
        raise ValueError("nginx source allowlist rendering is not deterministic")

    referer = referer_regex_for_host("staging.example.com")
    serve_key = render_serve_key("ci-check-key", referer)
    if serve_key != render_serve_key("ci-check-key", referer):
        raise ValueError("nginx serve-key rendering is not deterministic")

    def fetch_fixture(url: str) -> str:
        return "192.0.2.0/24\n2001:db8::/32\n" if url.endswith("v4") else "2001:db8:1::/48\n"

    cloudflare = render_cloudflare_real_ip(fetch_fixture)
    if cloudflare != render_cloudflare_real_ip(fetch_fixture):
        raise ValueError("Cloudflare real-IP rendering is not deterministic")


def check_environment_contracts() -> None:
    namespace = runpy.run_path(str(ENV_CONFIG_ROOT / "sync_github_environment.py"))
    targets = namespace["DEPLOYMENT_TARGETS"]
    service_configs = namespace["SERVICE_CONFIGS"]
    assignment_pattern = re.compile(r"(?:# )?([A-Z][A-Z0-9_]*)=.*")
    for target in targets:
        for service, config in service_configs.items():
            example = ENV_CONFIG_ROOT / target / service / "remote.env.example"
            configured = {
                match.group(1)
                for line in example.read_text(encoding="utf-8").splitlines()
                if (match := assignment_pattern.fullmatch(line))
            }
            documented = set((*config.variables, *config.secrets, *config.optional_secrets))
            if configured != documented:
                missing = ", ".join(sorted(documented - configured)) or "none"
                unexpected = ", ".join(sorted(configured - documented)) or "none"
                raise ValueError(
                    f"{example.relative_to(REPO_ROOT)} mismatch; "
                    f"missing={missing}; unexpected={unexpected}"
                )


def main() -> int:
    checks = (
        ("workflow YAML and action pinning", check_workflows),
        ("infra renderer determinism", check_infra_rendering),
        ("environment contract", check_environment_contracts),
        ("Compose schema and rendering", check_compose),
    )
    for label, check in checks:
        try:
            check()
        except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
            print(f"::error title={label} failed::{exc}", file=sys.stderr)
            return 1
        print(f"{label}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
