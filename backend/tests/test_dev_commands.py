"""Tests for the cross-platform local development command wrapper."""

import argparse
import json
import signal
import subprocess
import sys
import threading
from pathlib import Path
from threading import Barrier, Lock
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from scripts import dev
from tests.script_env import script_env


def test_up_starts_complete_durable_ingestion_stack(monkeypatch, tmp_path):
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setattr(dev, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dev, "BACKEND_ROOT", tmp_path / "backend")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])

    def fake_run(cmd, env=None):
        calls.append((list(cmd), env))
        return 0

    monkeypatch.setattr(dev, "_run", fake_run)

    result = dev.cmd_up(argparse.Namespace())

    assert result == 0
    assert [cmd for cmd, _ in calls] == [
        ["docker", "compose", "up", "-d", "--wait", "db", "rabbitmq"],
        ["uv", "run", "alembic", "upgrade", "head"],
        ["uv", "run", "python", "-m", "scripts.seed_data"],
        [
            "docker",
            "compose",
            "--profile",
            "queue",
            "up",
            "-d",
            "--build",
            "--wait",
            "app",
            "dispatcher",
            "worker",
        ],
    ]
    for _, env in calls:
        assert env is not None
        assert env["DATABASE_URL"] == dev.DEFAULT_DATABASE_URL
        assert "FINDB_QUEUE_HEALTH_ADMIN_API_KEY" not in env


def test_local_env_loads_dotenv_before_applying_defaults(monkeypatch, tmp_path):
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    (tmp_path / ".env").write_text("FINDB_API_BASE_URL=http://localhost:8080\n")
    monkeypatch.setattr(dev, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dev, "BACKEND_ROOT", backend_root)

    env = dev._local_env()

    assert env["FINDB_API_BASE_URL"] == "http://localhost:8080"


def test_local_env_prefers_process_environment_over_dotenv(monkeypatch, tmp_path):
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    (tmp_path / ".env").write_text("FINDB_API_BASE_URL=http://dotenv\n")
    monkeypatch.setattr(dev, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dev, "BACKEND_ROOT", backend_root)
    monkeypatch.setenv("FINDB_API_BASE_URL", "http://exported")

    env = dev._local_env()

    assert env["FINDB_API_BASE_URL"] == "http://exported"


def test_local_env_ignores_backend_dotenv(monkeypatch, tmp_path):
    backend_root = tmp_path / "backend"
    backend_root.mkdir()
    (tmp_path / ".env").write_text("FINDB_API_BASE_URL=http://root\n")
    (backend_root / ".env").write_text("FINDB_API_BASE_URL=http://backend\n")
    monkeypatch.setattr(dev, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dev, "BACKEND_ROOT", backend_root)

    env = dev._local_env()

    assert env["FINDB_API_BASE_URL"] == "http://root"


def test_up_stops_before_migration_when_dependency_start_fails(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])

    def fake_run(cmd, env=None):
        calls.append(list(cmd))
        return 9

    monkeypatch.setattr(dev, "_run", fake_run)

    assert dev.cmd_up(argparse.Namespace()) == 9
    assert calls == [["docker", "compose", "up", "-d", "--wait", "db", "rabbitmq"]]


def test_start_uses_existing_images_in_dependency_order(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])
    monkeypatch.setattr(
        dev,
        "_run",
        lambda cmd, env=None: calls.append(list(cmd)) or 0,
    )

    result = dev.cmd_start(argparse.Namespace())

    assert result == 0
    assert calls == [
        ["docker", "compose", "up", "-d", "--wait", "db", "rabbitmq"],
        [
            "docker",
            "compose",
            "--profile",
            "queue",
            "up",
            "-d",
            "--no-build",
            "--wait",
            "app",
            "dispatcher",
            "worker",
        ],
    ]


def test_restart_stops_and_starts_every_container_in_dependency_order(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])
    monkeypatch.setattr(
        dev,
        "_run",
        lambda cmd, env=None: calls.append(list(cmd)) or 0,
    )

    result = dev.cmd_restart(argparse.Namespace())

    assert result == 0
    assert calls == [
        [
            "docker",
            "compose",
            "--profile",
            "queue",
            "stop",
            "app",
            "dispatcher",
            "worker",
        ],
        ["docker", "compose", "stop", "db", "rabbitmq"],
        ["docker", "compose", "up", "-d", "--wait", "db", "rabbitmq"],
        [
            "docker",
            "compose",
            "--profile",
            "queue",
            "up",
            "-d",
            "--no-build",
            "--wait",
            "app",
            "dispatcher",
            "worker",
        ],
    ]


def test_build_builds_every_runtime_image(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])
    monkeypatch.setattr(
        dev,
        "_run",
        lambda cmd, env=None: calls.append(list(cmd)) or 0,
    )

    result = dev.cmd_build(argparse.Namespace())

    assert result == 0
    assert calls == [
        [
            "docker",
            "compose",
            "--profile",
            "queue",
            "build",
            "app",
            "dispatcher",
            "worker",
        ]
    ]


def test_down_includes_queue_profile(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])
    monkeypatch.setattr(
        dev,
        "_run",
        lambda cmd, env=None: calls.append(list(cmd)) or 0,
    )

    result = dev.cmd_down(argparse.Namespace())

    assert result == 0
    assert calls == [["docker", "compose", "--profile", "queue", "down"]]


def test_queue_logs_supports_tail_and_follow(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])
    monkeypatch.setattr(
        dev,
        "_run",
        lambda cmd, env=None: calls.append(list(cmd)) or 0,
    )

    result = dev.cmd_queue_logs(argparse.Namespace(tail=50, follow=True))

    assert result == 0
    assert calls == [
        [
            "docker",
            "compose",
            "--profile",
            "queue",
            "logs",
            "--tail=50",
            "--follow",
            "rabbitmq",
            "dispatcher",
            "worker",
        ]
    ]


@pytest.mark.parametrize(("workers", "no_start_db"), [(0, False), (2, False), (4, True)])
def test_test_db_partitions_parallel_backend_and_serial_migrations(
    monkeypatch, tmp_path, workers, no_start_db
):
    backend_root = tmp_path / "backend"
    tests = backend_root / "tests"
    tests.mkdir(parents=True)
    for name in (
        "test_full_market_migration.py",
        "test_migration_upgrade.py",
        "test_full_market.py",
    ):
        (tests / name).touch()
    monkeypatch.setattr(dev, "BACKEND_ROOT", backend_root)
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])
    calls = []
    monkeypatch.setattr(dev, "_run", lambda cmd, env=None: calls.append((cmd, env)) or 0)

    args = argparse.Namespace(workers=workers, no_start_db=no_start_db)
    assert dev.cmd_test_db(args) == 0
    if not no_start_db:
        assert calls.pop(0)[0] == ["docker", "compose", "up", "-d", "db"]
    assert len(calls) == 2
    backend, env = calls[0]
    assert backend[backend.index("-n") + 1] == str(workers)
    assert backend[backend.index("--dist") + 1] == "worksteal"
    assert "--ignore-glob=tests/test_*migration*.py" in backend
    assert env["DEBUG"] == "true"
    migrations, migration_env = calls[1]
    assert migration_env == env
    assert "-n" not in migrations
    assert migrations[-2:] == [
        "tests/test_full_market_migration.py",
        "tests/test_migration_upgrade.py",
    ]


@pytest.mark.parametrize("failed_call", [1, 2, 3])
def test_test_db_propagates_database_backend_and_migration_failures(monkeypatch, failed_call):
    calls = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])

    def fake_run(cmd, env=None):
        calls.append(cmd)
        return 17 if len(calls) == failed_call else 0

    monkeypatch.setattr(dev, "_run", fake_run)
    assert dev.cmd_test_db(argparse.Namespace(workers=2, no_start_db=False)) == 17
    assert len(calls) == failed_call


@pytest.mark.parametrize("command", ["test-db", "test-infra", "test-parallel"])
def test_test_suite_workers_default_and_serial_override(command):
    parser = dev._build_parser()
    assert parser.parse_args([command]).workers == 2
    assert parser.parse_args([command, "--workers", "0"]).workers == 0
    with pytest.raises(SystemExit):
        parser.parse_args([command, "--workers", "-1"])


def test_test_infra_uses_selected_workers_and_forwards_pytest_options(monkeypatch):
    calls = []
    monkeypatch.setattr(dev, "_run", lambda cmd: calls.append(cmd) or 7)
    monkeypatch.setattr(sys, "argv", ["dev.py", "test-infra", "--workers", "4", "-k", "deployment"])

    assert dev.main() == 7
    assert calls[0][-8:] == [
        "../infra/tests",
        "-n",
        "4",
        "--dist",
        "worksteal",
        "--timeout=300",
        "-k",
        "deployment",
    ]


@pytest.fixture
def parallel(monkeypatch):
    """The parallel command with Compose stubbed, callable from inside its own Backend suite."""
    monkeypatch.delenv(dev.TEST_PARALLEL_ACTIVE, raising=False)
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])
    started = []
    monkeypatch.setattr(dev, "_run", lambda cmd, env=None: started.append(cmd) or 0)

    def parse(*options):
        return dev._build_parser().parse_args(["test-parallel", *options])

    return SimpleNamespace(parse=parse, started=started)


def test_test_parallel_covers_every_level_of_pnpm_test(parallel):
    def steps(package):
        scripts = json.loads((dev.REPO_ROOT / package).read_text())["scripts"]
        return [step.strip().removeprefix("pnpm ") for step in scripts["test"].split("&&")]

    dashboard = [step.removeprefix("test:") for step in steps("dashboard/package.json")]
    # Each level of `pnpm test` and the suites that stand for it here.
    levels = {
        "test:contracts": ["contracts"],
        "test:backend": ["backend", "migrations"],
        "test:infra": ["infra"],
        "test:fetcher": ["fetcher"],
        "test:dashboard": [f"dashboard-{level}" for level in dashboard],
    }
    assert sorted(steps("package.json")) == sorted(levels)

    suites = {suite.name: suite for suite in dev._test_suites(parallel.parse("--workers", "4"))}

    assert tuple(suites) == dev.TEST_SUITES
    assert sorted(suites) == sorted(name for names in levels.values() for name in names)
    scripts = {"contracts": "test:contracts", "fetcher": "test:fetcher"} | {
        f"dashboard-{level}": f"test:dashboard:{level}" for level in dashboard
    }
    for name, script in scripts.items():
        assert suites[name].command[1:] == (script,)
        assert suites[name].cwd == dev.REPO_ROOT
        assert "VIRTUAL_ENV" not in suites[name].env
    backend, migrations, infra = (
        suites[name].command for name in ("backend", "migrations", "infra")
    )
    assert backend[backend.index("-n") + 1] == "4"
    assert "--ignore-glob=tests/test_*migration*.py" in backend
    assert "-n" not in migrations
    paths = [argument for argument in migrations if argument.startswith("tests/")]
    assert paths and all("migration" in path for path in paths)
    assert infra[infra.index("../infra/tests") + 1 : infra.index("../infra/tests") + 3] == (
        "-n",
        "4",
    )
    for name in ("backend", "migrations"):
        assert suites[name].env["DEBUG"] == "true"
        assert suites[name].env["TEST_DATABASE_URL"]


@pytest.mark.parametrize(
    ("options", "names", "starts_db"),
    [
        (["--only", "fetcher", "contracts"], ["contracts", "fetcher"], False),
        (["--only", "migrations"], ["migrations"], True),
        (["--only", "backend", "infra", "--no-start-db"], ["backend", "infra"], False),
        (
            ["--skip", "dashboard-e2e", "dashboard-browser"],
            ["contracts", "backend", "migrations", "infra", "fetcher", "dashboard-unit"],
            True,
        ),
    ],
)
def test_test_parallel_runs_the_selected_suites_and_starts_the_database_they_need(
    monkeypatch, parallel, options, names, starts_db
):
    ran = []

    def run_suite(suite, log_dir, running):
        ran.append(suite.name)
        log = log_dir / f"{suite.name}.log"
        log.write_text("")
        return dev._SuiteResult(suite, 0, 0.0, log)

    monkeypatch.setattr(dev, "_run_suite", run_suite)

    assert dev.cmd_test_parallel(parallel.parse(*options)) == 0
    assert sorted(ran) == sorted(names)
    assert parallel.started == ([["docker", "compose", "up", "-d", "db"]] if starts_db else [])


def test_test_parallel_rejects_unknown_or_empty_selections_and_a_failed_database(
    monkeypatch, parallel
):
    with pytest.raises(SystemExit):
        parallel.parse("--only", "frontend")
    with pytest.raises(RuntimeError, match="no test suite selected"):
        dev.cmd_test_parallel(parallel.parse("--only", "infra", "--skip", "infra"))
    monkeypatch.setattr(dev, "_run", lambda cmd, env=None: 17)
    monkeypatch.setattr(dev, "_run_suite", lambda *args: pytest.fail("suites must not start"))
    assert dev.cmd_test_parallel(parallel.parse()) == 17


def _fake_suite(name, tmp_path, script):
    return dev._Suite(name, (sys.executable, "-c", script), tmp_path, script_env())


def test_test_parallel_runs_suites_together_and_reports_only_failures_in_full(
    monkeypatch, capsys, tmp_path, parallel
):
    # Each waiter passes only while the other is running, so a serial run would fail.
    waiter = (
        "import os, pathlib, sys, time\n"
        "mine, other = sys.argv[1:]\n"
        f"assert os.environ[{dev.TEST_PARALLEL_ACTIVE!r}] == '1'\n"
        "pathlib.Path(mine).touch()\n"
        "deadline = time.monotonic() + 20\n"
        "while not pathlib.Path(other).exists():\n"
        "    assert time.monotonic() < deadline\n"
        "    time.sleep(0.01)\n"
        "print('waiter output')\n"
        "print('==== 3 passed, 1 skipped in 0.10s ====')\n"
    )
    suites = [
        dev._Suite(name, (sys.executable, "-c", waiter, mine, other), tmp_path, script_env())
        for name, mine, other in (("infra", "a", "b"), ("fetcher", "b", "a"))
    ] + [_fake_suite("backend", tmp_path, "print('boom'); print('2 failed, 5 passed'); exit(3)")]
    monkeypatch.setattr(dev, "_test_suites", lambda args: suites)

    assert dev.cmd_test_parallel(parallel.parse("--no-start-db")) == 1

    output = capsys.readouterr().out
    assert "[infra] passed in " in output and "[fetcher] passed in " in output
    assert "[backend] FAILED (exit 3) in " in output
    # The failed suite's output is shown; a passing suite's stays in its log file.
    assert "boom" in output and "waiter output" not in output
    summary = output.splitlines()[-5:]
    assert [line.split()[:2] for line in summary[:4]] == [
        ["infra", "passed"],
        ["fetcher", "passed"],
        ["backend", "FAILED"],
        ["total", "FAILED"],
    ]
    assert summary[0].endswith("3 passed, 1 skipped in 0.10s")
    assert summary[2].endswith("2 failed, 5 passed")
    log_dir = Path(summary[4].removeprefix("failed: backend; logs in "))
    assert (log_dir / "infra.log").read_text().startswith("waiter output")


def test_test_parallel_passes_quietly_and_refuses_to_start_inside_itself(
    monkeypatch, capsys, tmp_path, parallel
):
    monkeypatch.setattr(
        dev, "_test_suites", lambda args: [_fake_suite("contracts", tmp_path, "print('1 passed')")]
    )
    assert dev.cmd_test_parallel(parallel.parse("--no-start-db")) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[-2].split() == ["contracts", "passed", lines[-2].split()[2], "1", "passed"]
    assert lines[-1].split()[:2] == ["total", "passed"]

    monkeypatch.setenv(dev.TEST_PARALLEL_ACTIVE, "1")
    monkeypatch.setattr(dev, "_run_suite", lambda *args: pytest.fail("suites must not start"))
    with pytest.raises(RuntimeError, match="already running"):
        dev.cmd_test_parallel(parallel.parse("--no-start-db"))


@pytest.mark.parametrize(("jobs", "at_once"), [("1", 1), ("3", 3), (None, 8)])
def test_test_parallel_limits_suites_running_at_once_and_starts_the_longest_first(
    monkeypatch, parallel, jobs, at_once
):
    lock, started, active, peak = Lock(), [], [0], [0]
    everyone = Barrier(at_once)

    def run_suite(suite, log_dir, running):
        with lock:
            started.append(suite.name)
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        # Returns only once as many suites as allowed are inside together.
        if len(started) <= at_once:
            everyone.wait(timeout=10)
        with lock:
            active[0] -= 1
        log = log_dir / f"{suite.name}.log"
        log.write_text("")
        return dev._SuiteResult(suite, 0, 0.0, log)

    monkeypatch.setattr(dev, "_run_suite", run_suite)
    options = ["--no-start-db", *(["--jobs", jobs] if jobs else [])]

    assert dev.cmd_test_parallel(parallel.parse(*options)) == 0
    assert peak[0] == at_once
    assert sorted(started) == sorted(dev.TEST_SUITES)
    if at_once == 1:
        assert started == list(dev.TEST_SUITE_START_ORDER)
    with pytest.raises(SystemExit):
        parallel.parse("--jobs", "0")


def test_stopping_interrupts_whole_suites_and_kills_the_ones_that_ignore_it(tmp_path):
    polite = (
        "import pathlib, signal, subprocess, sys, time\n"
        # The suite's own child is in its process group and is interrupted with it.
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path('ready-polite').touch()\n"
        "try:\n"
        "    time.sleep(60)\n"
        "except KeyboardInterrupt:\n"
        "    pathlib.Path('cleaned').write_text(str(child.wait(timeout=5)))\n"
    )
    stubborn = (
        "import pathlib, signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path('ready-stubborn').touch()\n"
        "time.sleep(60)\n"
    )
    running = [
        subprocess.Popen(
            [sys.executable, "-c", script], cwd=tmp_path, env=script_env(), start_new_session=True
        )
        for script in (polite, stubborn)
    ]
    try:
        deadline = monotonic() + 20
        while not all((tmp_path / f"ready-{name}").exists() for name in ("polite", "stubborn")):
            assert monotonic() < deadline
            sleep(0.01)

        dev._stop_suites(running, grace=0.5)

        assert [process.poll() is not None for process in running] == [True, True]
        assert (tmp_path / "cleaned").read_text() == str(-signal.SIGINT)
        assert running[1].returncode == -signal.SIGKILL
    finally:
        for process in running:
            process.kill()


def test_terminated_test_parallel_stops_its_suites_and_starts_no_more(
    monkeypatch, capsys, parallel
):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal handlers need the main thread")
    started, stopped, released = [], [], threading.Event()

    def run_suite(suite, log_dir, running):
        started.append(suite.name)
        # The runner is waiting on this suite when it is told to terminate.
        signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)
        assert released.wait(timeout=10)
        log = log_dir / f"{suite.name}.log"
        log.write_text("")
        return dev._SuiteResult(suite, 143, 0.0, log)

    def stop_suites(running):
        stopped.append(list(running))
        released.set()

    monkeypatch.setattr(dev, "_run_suite", run_suite)
    monkeypatch.setattr(dev, "_stop_suites", stop_suites)
    handlers = [signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)]

    assert dev.cmd_test_parallel(parallel.parse("--no-start-db", "--jobs", "1")) == 130
    # The queued suites are dropped before the running one is stopped.
    assert started == [dev.TEST_SUITE_START_ORDER[0]]
    assert stopped == [[]]
    assert "interrupted; partial output in " in capsys.readouterr().err
    assert [signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)] == handlers
