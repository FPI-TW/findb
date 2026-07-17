"""Tests for production deployment gates."""

from pathlib import Path

import yaml

from scripts.check_queue_health import validate_queue_health
from scripts.predeploy_db_check import validate_predeploy_state


def test_deploy_does_not_gate_on_ec2_cpu_or_memory() -> None:
    deploy = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")

    assert "_NPROCESSORS_ONLN" not in deploy
    assert "/proc/meminfo" not in deploy
    assert "EC2 must have at least" not in deploy


def test_rabbitmq_consumer_timeout_has_one_source_and_is_verified() -> None:
    compose = yaml.safe_load(Path("docker-compose.prod.yml").read_text(encoding="utf-8"))
    expected = compose["x-normalization-consumer-timeout"]

    assert compose["x-app-environment"]["NORMALIZATION_CONSUMER_TIMEOUT_MS"] == expected
    assert (
        compose["services"]["rabbitmq-policy"]["environment"]["NORMALIZATION_CONSUMER_TIMEOUT_MS"]
        == expected
    )

    deploy = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "expected_consumer_timeout" in deploy
    assert "actual_consumer_timeout" in deploy
    assert "RabbitMQ consumer timeout policy mismatch" in deploy


def test_predeploy_database_state_accepts_safe_capacity() -> None:
    state = {
        "duplicate_raw_run_ids": 0,
        "long_transactions_over_5m": 0,
        "connection_headroom": 120,
    }

    assert validate_predeploy_state(state, minimum_connection_headroom=80) == []


def test_predeploy_database_state_reports_every_blocker() -> None:
    state = {
        "duplicate_raw_run_ids": 2,
        "long_transactions_over_5m": 1,
        "connection_headroom": 30,
    }

    errors = validate_predeploy_state(state, minimum_connection_headroom=80)

    assert len(errors) == 3
    assert any("duplicate" in error for error in errors)
    assert any("older than five minutes" in error for error in errors)
    assert any("connection headroom" in error for error in errors)


def test_queue_health_requires_recent_worker_and_no_expired_leases() -> None:
    assert (
        validate_queue_health(
            {"worker_heartbeat_age_seconds": 12.5, "expired_leases": 0},
            maximum_heartbeat_age=90,
        )
        == []
    )

    errors = validate_queue_health(
        {"worker_heartbeat_age_seconds": None, "expired_leases": 3},
        maximum_heartbeat_age=90,
    )
    assert errors == [
        "worker heartbeat has not been recorded",
        "3 normalization execution leases are expired",
    ]
