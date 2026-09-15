"""Unit tests for the dependency-free staging metric publisher."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock

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


def test_fetcher_runtime_security_requires_exact_flags_and_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    container = "findb-fetcher-scheduler"
    expected_mount = {("bind", "/var/lib/findb-fetcher", "/var/lib/findb-fetcher", True)}
    valid = {
        "Config": {"User": "10001:10001"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16777216"},
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/var/lib/findb-fetcher",
                "Destination": "/var/lib/findb-fetcher",
                "RW": True,
            }
        ],
    }
    invalid = json.loads(json.dumps(valid))
    invalid["HostConfig"]["Privileged"] = True
    monkeypatch.setattr(module, "EXPECTED_FETCHER_MOUNTS", {container: expected_mount})
    monkeypatch.setattr(
        module,
        "_run",
        Mock(
            side_effect=[
                Mock(returncode=0, stdout=json.dumps([valid])),
                Mock(returncode=0, stdout=json.dumps([invalid])),
            ]
        ),
    )

    valid_metrics, valid_errors = module._fetcher_runtime_security_metrics()
    invalid_metrics, invalid_errors = module._fetcher_runtime_security_metrics()

    assert valid_errors == []
    assert valid_metrics == [
        module._metric("DockerRuntimeSecurityHealthy", 1, "Count", "fetcher", container)
    ]
    assert invalid_errors == []
    assert invalid_metrics == [
        module._metric("DockerRuntimeSecurityHealthy", 0, "Count", "fetcher", container)
    ]


def test_tls_certificate_metric_verifies_hostname_and_publishes_remaining_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    tls = MagicMock()
    tls.__enter__.return_value.getpeercert.return_value = {"notAfter": "Oct 30 00:00:00 2026 GMT"}
    context = MagicMock()
    context.wrap_socket.return_value = tls
    expires_at = datetime.now(timezone.utc).timestamp() + 45 * 86400
    monkeypatch.setattr(module.socket, "create_connection", Mock(return_value=connection))
    monkeypatch.setattr(module.ssl, "create_default_context", Mock(return_value=context))
    monkeypatch.setattr(module.ssl, "cert_time_to_seconds", Mock(return_value=expires_at))

    metrics, errors = module._tls_certificate_metrics("findb-staging.example.com")

    assert errors == []
    assert metrics[0]["MetricName"] == "TLSCertificateDaysRemaining"
    assert metrics[0]["Value"] == pytest.approx(45, abs=0.01)
    context.wrap_socket.assert_called_once_with(
        connection, server_hostname="findb-staging.example.com"
    )


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


def test_active_feed_ingestion_metrics_are_bounded_and_low_cardinality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_run",
        Mock(
            return_value=Mock(
                returncode=0,
                stdout=json.dumps({"rejected_attempts": 2, "empty_snapshots": 1, "dq_errors": 3}),
            )
        ),
    )

    metrics, errors = module._active_feed_ingestion_metrics()

    assert errors == []
    assert metrics == [
        module._metric("ActiveFeedRejectedAttempts", 2, "Count", "findb", "active-feeds"),
        module._metric("ActiveFeedEmptySnapshots", 1, "Count", "findb", "active-feeds"),
        module._metric("ActiveFeedDQErrors", 3, "Count", "findb", "active-feeds"),
    ]
    assert "request" not in json.dumps(metrics)


def test_database_governance_metrics_are_aggregate_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_run",
        Mock(
            return_value=Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "lineage_orphans": 2,
                        "eod_default_rows": 3,
                        "credential_usage_mismatches": 4,
                    }
                ),
            )
        ),
    )

    metrics, errors = module._database_governance_metrics()

    assert errors == []
    assert metrics == [
        module._metric("LineageOrphanRows", 2, "Count", "findb", "canonical-and-raw"),
        module._metric(
            "EODDefaultPartitionRows",
            3,
            "Count",
            "findb",
            "market_data_eod_default",
        ),
        module._metric(
            "CredentialUsageAggregateMismatches",
            4,
            "Count",
            "findb",
            "api-credentials",
        ),
    ]
    command = module._run.call_args.args[0]
    assert command[:4] == ["docker", "exec", "findb-ingest", "python"]
    assert "raw.market_payload" in command[-1]
    assert "market_data_eod_default" in command[-1]
    assert "credential_usage_rollup" in command[-1]
    assert "COALESCE(sum(orphan_rows), 0)::bigint" in command[-1]


def test_invalid_credential_events_are_counted_without_log_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module,
        "_run",
        Mock(
            side_effect=[
                Mock(returncode=0, stdout="API credential rejected\n", stderr=""),
                Mock(
                    returncode=0,
                    stdout="",
                    stderr="API credential rejected\nAPI credential rejected\n",
                ),
            ]
        ),
    )

    metrics, errors = module._invalid_credential_event_metrics()

    assert errors == []
    assert metrics == [
        module._metric("InvalidCredentialEvents", 3, "Count", "findb", "api-credentials")
    ]
    assert module._run.call_args_list == [
        ((["docker", "logs", "--since", "10m", "findb-ingest"],),),
        ((["docker", "logs", "--since", "10m", "findb-serve"],),),
    ]


def test_collector_failure_is_published_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_module()
    monkeypatch.setattr(module, "_filesystem_metrics", Mock(return_value=[]))
    monkeypatch.setattr(module, "_container_metrics", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_rabbitmq_metrics", Mock(return_value=([], ["rabbit failed"])))
    monkeypatch.setattr(module, "_scheduler_metrics", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_active_feed_ingestion_metrics", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_database_governance_metrics", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_invalid_credential_event_metrics", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_rds_backup_lag_metric", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_dlm_policy_health_metric", Mock(return_value=([], [])))
    monkeypatch.setattr(module, "_tls_certificate_metrics", Mock(return_value=([], [])))

    metrics, errors = module.collect_metrics(
        "findb",
        "ap-southeast-1",
        "fin-db",
        "policy-0123456789abcdef0",
        "findb-staging.example.com",
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
