"""Cross-platform development command entrypoint for FinDB."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from dotenv import dotenv_values

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DEFAULT_PARTIAL_DUMP_CONFIG = BACKEND_ROOT / "configs" / "partial_dump.yaml"
DEFAULT_PARTIAL_DUMP_ROOT = BACKEND_ROOT / "seed" / "partial_dump"
DEFAULT_DATABASE_URL = "postgresql+asyncpg://findb:findb@localhost:5435/findb"
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://findb:findb@localhost:5435/findb_test"
DEPENDENCY_SERVICES = ("db", "rabbitmq")
RUNTIME_SERVICES = ("app", "dispatcher", "worker")


def _run(cmd: Sequence[str], env: dict[str, str] | None = None) -> int:
    print(f"$ {' '.join(cmd)}")
    completed = subprocess.run(cmd, cwd=BACKEND_ROOT, env=env)
    return completed.returncode


def _docker_compose_cmd() -> list[str]:
    docker = ["docker", "compose"]
    if (
        shutil.which("docker")
        and subprocess.run(
            docker + ["version"],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        return [
            *docker,
            "--project-directory",
            str(REPO_ROOT),
            "-f",
            str(COMPOSE_FILE),
        ]

    legacy = ["docker-compose"]
    if shutil.which("docker-compose"):
        print("warning: 'docker compose' not found, falling back to 'docker-compose'.")
        return [
            *legacy,
            "--project-directory",
            str(REPO_ROOT),
            "-f",
            str(COMPOSE_FILE),
        ]

    raise RuntimeError("Docker Compose not found. Install Docker Desktop or docker compose plugin.")


def _local_env() -> dict[str, str]:
    """Return safe local defaults shared by host commands and Compose."""
    env: dict[str, str] = {}
    env.update(
        {
            key: value
            for key, value in dotenv_values(REPO_ROOT / ".env").items()
            if value is not None
        }
    )
    env.update(os.environ)
    env.setdefault("DATABASE_URL", DEFAULT_DATABASE_URL)
    env.setdefault("SOURCE_API_KEY", "dev-source-key")
    env.setdefault("ADMIN_API_KEY", "dev-admin-key")
    return env


def _start_dependencies(
    compose: Sequence[str],
    services: Sequence[str],
    env: dict[str, str],
) -> int:
    return _run([*compose, "up", "-d", "--wait", *services], env=env)


def _start_runtime(
    compose: Sequence[str],
    env: dict[str, str],
    *,
    build: bool,
) -> int:
    image_option = "--build" if build else "--no-build"
    return _run(
        [
            *compose,
            "--profile",
            "queue",
            "up",
            "-d",
            image_option,
            "--wait",
            *RUNTIME_SERVICES,
        ],
        env=env,
    )


def _apply_migrations(env: dict[str, str]) -> int:
    return _run(["uv", "run", "alembic", "upgrade", "head"], env=env)


def _seed_data(env: dict[str, str]) -> int:
    return _run(["uv", "run", "python", "-m", "scripts.seed_data"], env=env)


def cmd_up_db(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    return _start_dependencies(compose, ["db"], _local_env())


def cmd_up_rabbit(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    return _start_dependencies(compose, ["rabbitmq"], _local_env())


def cmd_migrate(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    env = _local_env()
    start_code = _start_dependencies(compose, ["db"], env)
    return start_code if start_code != 0 else _apply_migrations(env)


def cmd_seed_data(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    env = _local_env()
    start_code = _start_dependencies(compose, ["db"], env)
    if start_code != 0:
        return start_code
    migrate_code = _apply_migrations(env)
    return migrate_code if migrate_code != 0 else _seed_data(env)


def cmd_up_server(args: argparse.Namespace) -> int:
    port = str(args.port or os.getenv("PORT", "8080"))
    cmd = [
        "uv",
        "run",
        "uvicorn",
        "app.main:app",
        "--host",
        args.host,
        "--port",
        port,
    ]
    if args.reload:
        cmd.append("--reload")
    return _run(cmd)


def cmd_up_app(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    env = _local_env()
    start_code = _start_dependencies(compose, ["db"], env)
    if start_code != 0:
        return start_code
    migrate_code = _apply_migrations(env)
    if migrate_code != 0:
        return migrate_code
    seed_code = _seed_data(env)
    if seed_code != 0:
        return seed_code
    return _run([*compose, "up", "-d", "--build", "--wait", "app"], env=env)


def cmd_up(_args: argparse.Namespace) -> int:
    """Start a complete local durable-ingestion stack."""
    compose = _docker_compose_cmd()
    env = _local_env()
    start_code = _start_dependencies(compose, DEPENDENCY_SERVICES, env)
    if start_code != 0:
        return start_code
    migrate_code = _apply_migrations(env)
    if migrate_code != 0:
        return migrate_code
    seed_code = _seed_data(env)
    if seed_code != 0:
        return seed_code
    return _start_runtime(compose, env, build=True)


def cmd_start(_args: argparse.Namespace) -> int:
    """Start the complete stack from existing images after Docker restarts."""
    compose = _docker_compose_cmd()
    env = _local_env()
    start_code = _start_dependencies(compose, DEPENDENCY_SERVICES, env)
    return start_code if start_code != 0 else _start_runtime(compose, env, build=False)


def cmd_restart(_args: argparse.Namespace) -> int:
    """Stop and start every core container in dependency order."""
    compose = _docker_compose_cmd()
    env = _local_env()
    stop_runtime_code = _run(
        [*compose, "--profile", "queue", "stop", *RUNTIME_SERVICES],
        env=env,
    )
    if stop_runtime_code != 0:
        return stop_runtime_code
    stop_dependency_code = _run([*compose, "stop", *DEPENDENCY_SERVICES], env=env)
    if stop_dependency_code != 0:
        return stop_dependency_code
    start_code = _start_dependencies(compose, DEPENDENCY_SERVICES, env)
    return start_code if start_code != 0 else _start_runtime(compose, env, build=False)


def cmd_build(_args: argparse.Namespace) -> int:
    """Build images shared by the app, dispatcher, and worker."""
    compose = _docker_compose_cmd()
    return _run(
        [*compose, "--profile", "queue", "build", *RUNTIME_SERVICES],
        env=_local_env(),
    )


def cmd_queue_status(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    return _run([*compose, "--profile", "queue", "ps"], env=_local_env())


def cmd_queue_logs(args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    cmd = [
        *compose,
        "--profile",
        "queue",
        "logs",
        f"--tail={args.tail}",
    ]
    if args.follow:
        cmd.append("--follow")
    cmd.extend(["rabbitmq", "dispatcher", "worker"])
    return _run(cmd, env=_local_env())


def cmd_test_db(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    up_code = _run([*compose, "up", "-d", "db"])
    if up_code != 0:
        return up_code

    env = os.environ.copy()
    env.setdefault("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    env["DEBUG"] = "true"
    env["SOURCE_API_KEY"] = env.get("SOURCE_API_KEY", "test-source-key")
    env["ADMIN_API_KEY"] = env.get("ADMIN_API_KEY", "test-admin-key")
    return _run(["uv", "run", "pytest", "--tb=short", "-q"], env=env)


def cmd_down(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    return _run([*compose, "--profile", "queue", "down"])


def cmd_partial_dump_validate(args: argparse.Namespace) -> int:
    cmd = ["uv", "run", "python", "-m", "scripts.partial_dump", "validate", "--config", args.config]
    if args.no_require_env:
        cmd.append("--no-require-env")
    return _run(cmd)


def cmd_partial_dump_run(args: argparse.Namespace) -> int:
    cmd = ["uv", "run", "python", "-m", "scripts.partial_dump", "dump", "--config", args.config]
    if args.output_dir:
        cmd.extend(["--output-dir", args.output_dir])
    if args.dry_run:
        cmd.append("--dry-run")
    return _run(cmd)


def cmd_seed_upsert(args: argparse.Namespace) -> int:
    cmd = ["uv", "run", "python", "-m", "scripts.seed_upsert"]
    if args.artifact_dir:
        cmd.extend(["--artifact-dir", args.artifact_dir])
    if args.partial_dump_root:
        cmd.extend(["--partial-dump-root", args.partial_dump_root])
    if args.database_url:
        cmd.extend(["--database-url", args.database_url])
    if args.chunk_size:
        cmd.extend(["--chunk-size", str(args.chunk_size)])
    if args.truncate:
        cmd.append("--truncate")
    return _run(cmd)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FinDB development command wrapper")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("up-db", help="Start database container only")
    subparsers.add_parser("up-rabbit", help="Start RabbitMQ container only")
    subparsers.add_parser("migrate", help="Start database and apply Alembic migrations")
    subparsers.add_parser(
        "seed-data",
        help="Apply migrations and seed the local dataset registry",
    )

    up_server = subparsers.add_parser("up-server", help="Start FastAPI server locally")
    up_server.add_argument("--host", default="0.0.0.0", help="Bind host")
    up_server.add_argument("--port", type=int, help="Bind port (default: env PORT or 8080)")
    up_server.add_argument(
        "--reload",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Enable auto reload (default: true)",
    )

    subparsers.add_parser("up-app", help="Start app and database without queue workers")
    subparsers.add_parser(
        "up",
        help="Migrate, seed, build, and start the complete local stack",
    )
    subparsers.add_parser(
        "start",
        help="Start the complete stack from existing images without migration",
    )
    subparsers.add_parser(
        "restart",
        help="Stop and start every core container without rebuilding",
    )
    subparsers.add_parser(
        "build",
        help="Build app, dispatcher, and worker images without starting them",
    )
    subparsers.add_parser("queue-status", help="Show queue service status")
    queue_logs = subparsers.add_parser(
        "queue-logs",
        help="Show RabbitMQ, dispatcher, and worker logs",
    )
    queue_logs.add_argument("--tail", type=int, default=200, help="Number of log lines")
    queue_logs.add_argument("--follow", action="store_true", help="Follow log output")
    subparsers.add_parser("test-db", help="Run pytest after ensuring db container is up")
    subparsers.add_parser("down", help="Stop development containers")

    partial_dump_validate = subparsers.add_parser(
        "partial-dump-validate",
        help="Validate configs/partial_dump.yaml contract",
    )
    partial_dump_validate.add_argument(
        "--config",
        default=str(DEFAULT_PARTIAL_DUMP_CONFIG),
        help="Partial dump config path",
    )
    partial_dump_validate.add_argument(
        "--no-require-env",
        action="store_true",
        help="Skip source environment-variable existence check",
    )

    partial_dump_run = subparsers.add_parser(
        "partial-dump-run",
        help="Run partial dump export flow",
    )
    partial_dump_run.add_argument(
        "--config",
        default=str(DEFAULT_PARTIAL_DUMP_CONFIG),
        help="Partial dump config path",
    )
    partial_dump_run.add_argument(
        "--output-dir",
        help="Override output root directory",
    )
    partial_dump_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; skip data export",
    )

    seed_upsert = subparsers.add_parser(
        "seed-upsert",
        help="Upsert seed data artifact into a running database",
    )
    seed_upsert.add_argument(
        "--artifact-dir",
        help="Specific artifact directory containing manifest.json",
    )
    seed_upsert.add_argument(
        "--partial-dump-root",
        default=str(DEFAULT_PARTIAL_DUMP_ROOT),
        help="Artifact root to auto-pick latest dump when --artifact-dir is omitted",
    )
    seed_upsert.add_argument(
        "--database-url",
        help="Override target database URL (default: env DATABASE_URL)",
    )
    seed_upsert.add_argument(
        "--chunk-size",
        type=int,
        default=1000,
        help="Rows per upsert batch",
    )
    seed_upsert.add_argument(
        "--truncate",
        action="store_true",
        help=(
            "Reset managed schemas before upsert: drop obsolete tables "
            "(except public.alembic_version) and truncate imported tables"
        ),
    )

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {
        "up-db": cmd_up_db,
        "up-rabbit": cmd_up_rabbit,
        "migrate": cmd_migrate,
        "seed-data": cmd_seed_data,
        "up-server": cmd_up_server,
        "up-app": cmd_up_app,
        "up": cmd_up,
        "start": cmd_start,
        "restart": cmd_restart,
        "build": cmd_build,
        "queue-status": cmd_queue_status,
        "queue-logs": cmd_queue_logs,
        "test-db": cmd_test_db,
        "down": cmd_down,
        "partial-dump-validate": cmd_partial_dump_validate,
        "partial-dump-run": cmd_partial_dump_run,
        "seed-upsert": cmd_seed_upsert,
    }

    if not args.command:
        parser.print_help()
        return 1

    try:
        return handlers[args.command](args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
