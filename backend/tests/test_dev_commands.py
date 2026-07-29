"""Tests for the cross-platform local development command wrapper."""

import argparse

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
