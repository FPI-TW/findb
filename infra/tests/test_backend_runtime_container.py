"""Run the deployment's migration command in the production image, without dev deps."""

from __future__ import annotations

import os
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from script_env import script_env

ROOT = Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("FINDB_TEST_BACKEND_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE, reason="set FINDB_TEST_BACKEND_IMAGE to a built Backend image"
)


def docker_env() -> dict[str, str]:
    # The Docker CLI still needs its own context/host selection.
    return script_env() | {k: v for k, v in os.environ.items() if k.startswith("DOCKER_")}


def docker(*args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=docker_env(),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def deployment_migration_command() -> list[str]:
    helper = (ROOT / "infra/deploy/runtime-secrets/deploy_findb_aws.sh").read_text()
    migration = helper.split("<<'MIGRATION_SCRIPT'\n", 1)[1].split("\nMIGRATION_SCRIPT", 1)[0]
    command = migration.split("ingest sh -euc ", 1)[1].split("</dev/null", 1)[0]
    script = shlex.split(command)[0]
    # Exercise the migration portion with the installed runtime. The following
    # RDS TLS/credential gates use AWS-specific roles and are tested separately.
    migration_only, _ = script.split("python /app/scripts/predeploy_db_check.py", 1)
    return ["sh", "-euc", migration_only]


@pytest.fixture
def runtime() -> Iterator[tuple[Callable[..., str], Callable[[str], str]]]:
    token = secrets.token_hex(8)
    network = f"findb-runtime-{token}"
    postgres = f"{network}-postgres"
    containers: list[str] = []
    docker("network", "create", "--internal", network)
    try:
        containers.append(postgres)
        docker(
            "run",
            "-d",
            "--name",
            postgres,
            "--network",
            network,
            "--network-alias",
            "db",
            "-e",
            "POSTGRES_USER=findb",
            "-e",
            "POSTGRES_PASSWORD=findb",
            "-e",
            "POSTGRES_DB=findb_runtime",
            "postgres:16-alpine",
        )
        deadline = time.monotonic() + 60
        while True:
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    postgres,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "findb",
                    "-d",
                    "findb_runtime",
                ],
                capture_output=True,
                timeout=10,
                check=False,
                env=docker_env(),
            )
            if probe.returncode == 0:
                break
            assert time.monotonic() < deadline, docker("logs", postgres)
            time.sleep(0.5)

        def sql(statement: str) -> str:
            return docker(
                "exec",
                postgres,
                "psql",
                "-U",
                "findb",
                "-d",
                "findb_runtime",
                "-v",
                "ON_ERROR_STOP=1",
                "-Atc",
                statement,
            )

        def run(*command: str) -> str:
            name = f"{network}-app-{len(containers)}"
            containers.append(name)
            assert IMAGE is not None
            return docker(
                "run",
                "--rm",
                "--name",
                name,
                "--network",
                network,
                "--read-only",
                "--tmpfs",
                "/tmp",
                "-e",
                "UV_OFFLINE=1",
                "-e",
                "UV_CACHE_DIR=/tmp/uv",
                "-e",
                "DATABASE_URL=postgresql+asyncpg://findb:findb@db:5432/findb_runtime",
                IMAGE,
                *command,
            )

        yield run, sql
    finally:
        # Only resources created by this fixture are eligible for removal.
        for name in reversed(containers):
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=30,
                check=False,
                env=docker_env(),
            )
        docker("network", "rm", network)


@pytest.mark.parametrize(
    # PR5 requires the accepted Shioaji retirement revision before deployment.
    "previous_revision",
    [None, "d8e3f0b5a2c4"],
    ids=["fresh", "accepted-upgrade"],
)
def test_deployed_migration_uses_installed_runtime(
    runtime: tuple[Callable[..., str], Callable[[str], str]], previous_revision: str | None
) -> None:
    run, sql = runtime
    run(
        "python",
        "-c",
        "import importlib.util; from pathlib import Path; "
        "assert not Path('/fetcher').exists(); "
        "assert importlib.util.find_spec('findb_fetcher') is None; "
        "assert importlib.util.find_spec('pytest') is None",
    )
    head = run("alembic", "heads").split()[0]

    def seed_sentinel() -> None:
        sql("CREATE TABLE runtime_sentinel (id integer PRIMARY KEY, value text NOT NULL)")
        sql("INSERT INTO runtime_sentinel VALUES (1, 'preserve-existing-data')")

    if previous_revision:
        run("alembic", "upgrade", previous_revision)
        assert sql("SELECT version_num FROM alembic_version") == previous_revision
        seed_sentinel()
    else:
        assert sql("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") == "0"

    # Execute the deployed migration command twice to prove idempotence.
    for iteration in range(2):
        run(*deployment_migration_command())
        assert f"{head} (head)" in run("alembic", "current")
        assert sql("SELECT version_num FROM alembic_version") == head
        if previous_revision is None and iteration == 0:
            seed_sentinel()
        assert sql("SELECT value FROM runtime_sentinel WHERE id = 1") == "preserve-existing-data"


def test_deployment_rejects_unretired_shioaji_without_writes(
    runtime: tuple[Callable[..., str], Callable[[str], str]],
) -> None:
    run, sql = runtime
    previous_revision = "a42c89d3e610"
    run("alembic", "upgrade", previous_revision)
    snapshot_query = """
        SELECT jsonb_build_object(
            'datasets', (SELECT jsonb_agg(to_jsonb(d) ORDER BY dataset_key)
                         FROM dataset_registry d),
            'controls', (SELECT jsonb_agg(to_jsonb(c) ORDER BY scheduler_key)
                         FROM scheduler_control c)
        )
    """
    before = sql(snapshot_query)
    with pytest.raises(AssertionError, match="unknown_difference") as failure:
        run(*deployment_migration_command())
    assert "pilot_shioaji_v1" in str(failure.value)
    assert "full_market_shioaji_v1" in str(failure.value)
    assert sql("SELECT version_num FROM alembic_version") == previous_revision
    assert sql(snapshot_query) == before
