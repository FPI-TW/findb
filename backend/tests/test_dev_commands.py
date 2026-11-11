"""Tests for the cross-platform local development command wrapper."""

import argparse
import sys
from threading import Barrier

import pytest

from scripts import dev


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


@pytest.mark.parametrize(("backend_status", "infra_status"), [(0, 0), (3, 0), (0, 7)])
def test_test_parallel_runs_both_suites_concurrently(
    monkeypatch, capsys, backend_status, infra_status
):
    both_started = Barrier(2)
    args = dev._build_parser().parse_args(["test-parallel", "--workers", "4", "--no-start-db"])

    def run_suite(suite_args, status):
        assert suite_args.workers == 4
        assert suite_args.no_start_db
        both_started.wait(timeout=5)
        return status

    monkeypatch.setattr(
        dev, "cmd_test_db", lambda suite_args: run_suite(suite_args, backend_status)
    )
    monkeypatch.setattr(
        dev, "cmd_test_infra", lambda suite_args: run_suite(suite_args, infra_status)
    )

    assert dev.cmd_test_parallel(args) == (backend_status or infra_status)
    output = capsys.readouterr().out
    assert "[Backend]" in output
    assert "[Infra]" in output
    assert output.splitlines()[-1].startswith("[Total]")
