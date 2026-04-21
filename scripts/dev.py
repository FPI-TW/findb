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
    env["SOURCE_ALLOWLIST_CIDRS"] = ""
    env["SOURCE_API_KEYS"] = env.get("SOURCE_API_KEYS", "test-source-key")
    env["ADMIN_API_KEYS"] = env.get("ADMIN_API_KEYS", "test-admin-key")
    return _run(["uv", "run", "pytest", "--tb=short", "-q"], env=env)


def cmd_down(_args: argparse.Namespace) -> int:
    compose = _docker_compose_cmd()
    return _run([*compose, "down"])


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
