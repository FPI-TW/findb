"""Cross-platform development command entrypoint for FinDB."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://findb:findb@localhost:5435/findb_test"


def _run(cmd: Sequence[str], env: dict[str, str] | None = None) -> int:
    print(f"$ {' '.join(cmd)}")
    completed = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env)
    return completed.returncode


def _docker_compose_cmd() -> list[str]:
    docker = ["docker", "compose"]
    if (
        shutil.which("docker")
        and subprocess.run(
            docker + ["version"],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        return docker

    legacy = ["docker-compose"]
    if shutil.which("docker-compose"):
        print("warning: 'docker compose' not found, falling back to 'docker-compose'.")
        return legacy

    raise RuntimeError("Docker Compose not found. Install Docker Desktop or docker compose plugin.")


def cmd_up_db(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    return _run([*compose, "up", "-d", "db"])


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


def cmd_up(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    return _run([*compose, "up", "-d", "--build", "app", "db"])


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
    return _run([*compose, "down"])


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

    up_server = subparsers.add_parser("up-server", help="Start FastAPI server locally")
    up_server.add_argument("--host", default="0.0.0.0", help="Bind host")
    up_server.add_argument("--port", type=int, help="Bind port (default: env PORT or 8080)")
    up_server.add_argument(
        "--reload",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Enable auto reload (default: true)",
    )

    subparsers.add_parser("up", help="Start app and db containers")
    subparsers.add_parser("test-db", help="Run pytest after ensuring db container is up")
    subparsers.add_parser("down", help="Stop development containers")

    partial_dump_validate = subparsers.add_parser(
        "partial-dump-validate",
        help="Validate configs/partial_dump.yaml contract",
    )
    partial_dump_validate.add_argument(
        "--config",
        default="configs/partial_dump.yaml",
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
        default="configs/partial_dump.yaml",
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
        default="seed/partial_dump",
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
        "up-server": cmd_up_server,
        "up": cmd_up,
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
