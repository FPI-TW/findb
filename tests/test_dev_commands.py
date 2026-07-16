"""Tests for the cross-platform local development command wrapper."""

import argparse

from scripts import dev


def test_up_starts_complete_durable_ingestion_stack(monkeypatch):
    calls: list[tuple[list[str], dict[str, str] | None]] = []

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
        assert env["SOURCE_API_KEY"] == "dev-source-key"
        assert env["ADMIN_API_KEY"] == "dev-admin-key"


def test_up_stops_before_migration_when_dependency_start_fails(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(dev, "_docker_compose_cmd", lambda: ["docker", "compose"])

    def fake_run(cmd, env=None):
        calls.append(list(cmd))
        return 9

    monkeypatch.setattr(dev, "_run", fake_run)

    assert dev.cmd_up(argparse.Namespace()) == 9
    assert calls == [["docker", "compose", "up", "-d", "--wait", "db", "rabbitmq"]]


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
