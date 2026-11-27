"""Cross-platform development command entrypoint for FinDB."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, perf_counter
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
PYTEST_COMMAND = (
    "uv",
    "run",
    "pytest",
    "--tb=short",
    "-q",
    "--durations=20",
    "--durations-min=0.5",
)


def _run(cmd: Sequence[str], env: dict[str, str] | None = None) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
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


def _start_test_db() -> int:
    return _run([*_docker_compose_cmd(), "up", "-d", "db"])


def _test_db_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    env["DEBUG"] = "true"
    return env


def _backend_pytest(workers: int) -> list[str]:
    return [
        *PYTEST_COMMAND,
        "-n",
        str(workers),
        "--dist",
        "worksteal",
        "--ignore-glob=tests/test_*migration*.py",
    ]


def _migration_pytest() -> list[str]:
    # Migration templates are reused in one serial session, matching CI.
    migration_paths = sorted((BACKEND_ROOT / "tests").glob("test_*migration*.py"))
    return [*PYTEST_COMMAND, *(str(path.relative_to(BACKEND_ROOT)) for path in migration_paths)]


def _infra_pytest(workers: int, pytest_args: Sequence[str] = ()) -> list[str]:
    return [
        *PYTEST_COMMAND,
        "../infra/tests",
        "-n",
        str(workers),
        "--dist",
        "worksteal",
        "--timeout=300",
        *pytest_args,
    ]


def _fetcher_pytest(workers: int) -> list[str]:
    # Same command as `pnpm test:fetcher`, with the worker count of this run; runs from REPO_ROOT.
    return [
        "uv",
        "--directory",
        "fetcher",
        "run",
        "pytest",
        "-n",
        str(workers),
        "--dist",
        "worksteal",
    ]


def cmd_test_db(args: argparse.Namespace) -> int:
    if not args.no_start_db:
        up_code = _start_test_db()
        if up_code != 0:
            return up_code

    env = _test_db_env()
    test_code = _run(_backend_pytest(args.workers), env=env)
    if test_code != 0:
        return test_code
    return _run(_migration_pytest(), env=env)


def cmd_test_infra(args: argparse.Namespace, pytest_args: Sequence[str] = ()) -> int:
    return _run(_infra_pytest(args.workers, pytest_args))


@dataclass(frozen=True)
class _Suite:
    name: str
    command: tuple[str, ...]
    cwd: Path
    env: dict[str, str]


@dataclass(frozen=True)
class _SuiteResult:
    suite: _Suite
    status: int
    seconds: float
    log: Path


# Every level of `pnpm test`; the two Backend sessions use PostgreSQL.
TEST_SUITES = (
    "contracts",
    "backend",
    "migrations",
    "infra",
    "fetcher",
    "dashboard-unit",
    "dashboard-browser",
    "dashboard-e2e",
)
DATABASE_TEST_SUITES = frozenset({"backend", "migrations"})
# Longest first, so a limit on suites running at once does not leave a long one for last.
TEST_SUITE_START_ORDER = (
    "infra",
    "fetcher",
    "backend",
    "migrations",
    "dashboard-e2e",
    "dashboard-unit",
    "dashboard-browser",
    "contracts",
)
# Set for every suite it starts: a test reaching the real command must not start them all again.
TEST_PARALLEL_ACTIVE = "FINDB_TEST_PARALLEL_ACTIVE"


def _test_suites(args: argparse.Namespace) -> list[_Suite]:
    db_env = _test_db_env()
    # The other applications resolve their own environments, not Backend's.
    repo_env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    pnpm = shutil.which("pnpm") or "pnpm"
    commands: dict[str, tuple[Sequence[str], Path, dict[str, str]]] = {
        "contracts": ([pnpm, "test:contracts"], REPO_ROOT, repo_env),
        "backend": (_backend_pytest(args.workers), BACKEND_ROOT, db_env),
        "migrations": (_migration_pytest(), BACKEND_ROOT, db_env),
        "infra": (_infra_pytest(args.workers), BACKEND_ROOT, os.environ.copy()),
        "fetcher": (_fetcher_pytest(args.workers), REPO_ROOT, repo_env),
        "dashboard-unit": ([pnpm, "test:dashboard:unit"], REPO_ROOT, repo_env),
        "dashboard-browser": ([pnpm, "test:dashboard:browser"], REPO_ROOT, repo_env),
        "dashboard-e2e": ([pnpm, "test:dashboard:e2e"], REPO_ROOT, repo_env),
    }
    selected = [name for name in args.only or TEST_SUITES if name not in (args.skip or ())]
    return [
        _Suite(name, tuple(commands[name][0]), commands[name][1], commands[name][2])
        for name in TEST_SUITES
        if name in selected
    ]


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OUTCOME = re.compile(r"\b\d+ (passed|failed|errors?|skipped)\b")


def _suite_outcome(log: Path) -> str:
    """The runner's own closing count, e.g. ``2299 passed, 3 skipped in 90.35s``."""
    outcome = ""
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = _ANSI.sub("", line).strip(" =")
        if _OUTCOME.search(line):
            outcome = line
    return outcome


def _run_suite(
    suite: _Suite, log_dir: Path, running: list[subprocess.Popen[bytes]]
) -> _SuiteResult:
    log = log_dir / f"{suite.name}.log"
    started = perf_counter()
    with log.open("wb") as output:
        process = subprocess.Popen(
            suite.command,
            cwd=suite.cwd,
            env={**suite.env, TEST_PARALLEL_ACTIVE: "1"},
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            # Its own process group, so a stop reaches the whole suite exactly once.
            start_new_session=True,
        )
        running.append(process)
        status = process.wait()
    seconds = perf_counter() - started
    verdict = "passed" if status == 0 else f"FAILED (exit {status})"
    print(f"[{suite.name}] {verdict} in {seconds:.1f}s", flush=True)
    return _SuiteResult(suite, status, seconds, log)


def _stop_suites(running: Sequence[subprocess.Popen[bytes]], grace: float = 10.0) -> None:
    """Interrupt every suite so pytest drops its databases, then stop what is still there."""
    for signum in (signal.SIGINT, signal.SIGTERM, None):
        for process in running:
            if process.poll() is not None:
                continue
            if signum is None or not hasattr(os, "killpg"):
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    pass
        deadline = monotonic() + grace
        for process in running:
            try:
                process.wait(timeout=max(0.0, deadline - monotonic()))
            except subprocess.TimeoutExpired:
                pass


def _raise_interrupt(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


def cmd_test_parallel(args: argparse.Namespace) -> int:
    if os.environ.get(TEST_PARALLEL_ACTIVE):
        raise RuntimeError("test-parallel is already running these suites")
    suites = _test_suites(args)
    if not suites:
        raise RuntimeError("no test suite selected")
    if not args.no_start_db and DATABASE_TEST_SUITES.intersection(s.name for s in suites):
        up_code = _start_test_db()
        if up_code != 0:
            return up_code

    log_dir = Path(tempfile.mkdtemp(prefix="findb-test-parallel-"))
    print(f"Running {', '.join(suite.name for suite in suites)}; output in {log_dir}", flush=True)
    started = perf_counter()
    running: list[subprocess.Popen[bytes]] = []
    queue = sorted(suites, key=lambda suite: TEST_SUITE_START_ORDER.index(suite.name))
    # A terminated or hung-up runner stops its suites the same way Ctrl-C does.
    handled = [getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]
    if threading.current_thread() is not threading.main_thread():
        handled = []
    previous = {signum: signal.signal(signum, _raise_interrupt) for signum in handled}
    previous[signal.SIGINT] = signal.getsignal(signal.SIGINT)
    try:
        with ThreadPoolExecutor(min(args.jobs or len(suites), len(suites))) as executor:
            futures: dict[str, Future[_SuiteResult]] = {}
            try:
                for suite in queue:
                    futures[suite.name] = executor.submit(_run_suite, suite, log_dir, running)
                results = [futures[suite.name].result() for suite in suites]
            except KeyboardInterrupt:
                for signum in previous:
                    signal.signal(signum, signal.SIG_IGN)
                for future in futures.values():
                    future.cancel()
                _stop_suites(running)
                print(f"interrupted; partial output in {log_dir}", file=sys.stderr)
                return 130
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    elapsed = perf_counter() - started

    failed = [result for result in results if result.status != 0]
    # Only a failed suite's output is worth reading in full; the rest stays in its file.
    for result in failed:
        print(f"\n===== {result.suite.name}: {' '.join(result.suite.command)} =====", flush=True)
        sys.stdout.write(result.log.read_text(encoding="utf-8", errors="replace"))
        sys.stdout.flush()
    print()
    width = max(len(result.suite.name) for result in results)
    for result in results:
        verdict = "passed" if result.status == 0 else "FAILED"
        print(
            f"{result.suite.name:<{width}}  {verdict:<6}  {result.seconds:>6.1f}s  "
            f"{_suite_outcome(result.log)}".rstrip()
        )
    serial = sum(result.seconds for result in results)
    print(
        f"{'total':<{width}}  {'FAILED' if failed else 'passed':<6}  {elapsed:>6.1f}s  "
        f"({serial:.1f}s of suite time)"
    )
    if failed:
        print(f"failed: {', '.join(result.suite.name for result in failed)}; logs in {log_dir}")
    return 1 if failed else 0


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


def _test_worker_count(value: str) -> int:
    workers = int(value)
    if workers < 0:
        raise argparse.ArgumentTypeError("workers must be 0 or greater")
    return workers


def _test_job_count(value: str) -> int:
    jobs = int(value)
    if jobs < 1:
        raise argparse.ArgumentTypeError("jobs must be 1 or greater")
    return jobs


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
    test_db = subparsers.add_parser("test-db", help="Run Backend tests")
    test_infra = subparsers.add_parser("test-infra", help="Run Infra pytest suite")
    test_parallel = subparsers.add_parser(
        "test-parallel", help="Run every test suite of `pnpm test` concurrently"
    )
    for test_parser in (test_db, test_infra, test_parallel):
        test_parser.add_argument(
            "--workers",
            type=_test_worker_count,
            default=2,
            help="Workers for each of Backend, Infra and Fetcher (0 for serial; default: 2)",
        )
    test_parallel.add_argument(
        "--jobs",
        type=_test_job_count,
        help="Suites running at once (default: all of them)",
    )
    for test_parser in (test_db, test_parallel):
        test_parser.add_argument(
            "--no-start-db",
            action="store_true",
            help="Use an already running PostgreSQL instance at TEST_DATABASE_URL",
        )
    for option, effect in (("--only", "Run only"), ("--skip", "Leave out")):
        test_parallel.add_argument(
            option,
            nargs="+",
            choices=TEST_SUITES,
            metavar="SUITE",
            help=f"{effect} these suites: {', '.join(TEST_SUITES)}",
        )
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
    args, extra_args = parser.parse_known_args()
    if extra_args and args.command != "test-infra":
        parser.error(f"unrecognized arguments: {' '.join(extra_args)}")

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
        "test-infra": lambda options: cmd_test_infra(options, extra_args),
        "test-parallel": cmd_test_parallel,
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
