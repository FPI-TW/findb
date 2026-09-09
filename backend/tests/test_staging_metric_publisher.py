"""Unit tests for the dependency-free staging metric publisher."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "monitoring" / "publish_staging_metrics.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_staging_metrics", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expected_container_sets_are_bounded() -> None:
    module = _load_module()

    assert set(module.EXPECTED_CONTAINERS) == {"findb", "fetcher"}
    assert "findb-rabbitmq" in module.EXPECTED_CONTAINERS["findb"]
    assert module.EXPECTED_CONTAINERS["fetcher"] == (
        "findb-fetcher-finlab-scheduler",
        "findb-fetcher-scheduler",
        "findb-fetcher-shioaji-scheduler",
    )


def test_missing_container_publishes_unhealthy_without_exposing_command_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "EXPECTED_CONTAINERS",
        {"findb": ("missing-container",), "fetcher": ()},
    )
    monkeypatch.setattr(module, "_run", Mock(return_value=Mock(returncode=1)))

    metrics, errors = module._container_metrics("findb")

    assert errors == []
    assert metrics == [
        module._metric("DockerContainerHealthy", 0, "Count", "findb", "missing-container")
    ]


def test_scheduler_metrics_use_only_safe_low_cardinality_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    payload = [
        {
            "scheduler_key": "finlab_tw_equity_eod_v1",
            "heartbeat_age_seconds": 12.5,
            "last_error": "must not become a metric dimension",
        }
    ]
    monkeypatch.setattr(
        module,
        "_run",
        Mock(return_value=Mock(returncode=0, stdout=json.dumps(payload))),
    )

    metrics, errors = module._scheduler_metrics()

    assert errors == []
    assert metrics == [
        module._metric(
            "SchedulerHeartbeatAgeSeconds",
            12.5,
            "Seconds",
            "fetcher",
            "finlab_tw_equity_eod_v1",
        )
    ]
    assert "last_error" not in json.dumps(metrics)


def test_collector_failure_is_published_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_filesystem_metrics", Mock(return_value=[]))
    monkeypatch.setattr(module, "_container_metrics", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_rabbitmq_metrics", Mock(return_value=([], ["rabbit failed"])))
    monkeypatch.setattr(module, "_scheduler_metrics", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_rds_backup_lag_metric", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_dlm_policy_health_metric", Mock(return_value=([], [])))

    metrics, errors = module.collect_metrics(
        "findb", "ap-southeast-1", "fin-db", "policy-0123456789abcdef0"
    )

    assert errors == ["rabbit failed"]
    assert metrics[-1] == module._metric("CollectorSuccess", 0, "Count", "findb", "host")


def test_dlm_policy_health_requires_enabled_state_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_run",
        Mock(
            return_value=Mock(
                returncode=0,
                stdout=json.dumps({"Policy": {"State": "ENABLED", "StatusMessage": "ENABLED"}}),
            )
        ),
    )

    metrics, errors = module._dlm_policy_health_metric("ap-southeast-1", "policy-0123456789abcdef0")

    assert errors == []
    assert metrics == [
        module._metric("DLMPolicyHealthy", 1, "Count", "findb", "policy-0123456789abcdef0")
    ]
