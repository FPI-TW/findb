"""Unit tests for the dependency-free staging metric publisher."""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "monitoring" / "publish_staging_metrics.py"
INVENTORY = REPO_ROOT / "infra" / "deploy" / "generated" / "provider_inventory.v1.json"
DEPLOY = REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "deploy_fetcher_aws.sh"
RELEASE = REPO_ROOT / "infra" / "deploy" / "runtime-secrets" / "release_fetcher_provider.sh"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_staging_metrics", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expected_container_sets_are_bounded() -> None:
    module = _load_module()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))

    assert set(module.EXPECTED_CONTAINERS) == {"findb", "fetcher"}
    assert "findb-rabbitmq" in module.EXPECTED_CONTAINERS["findb"]
    assert module.EXPECTED_CONTAINERS["fetcher"] == tuple(
        consumer["name"] for consumer in inventory["consumers"]
    )
    assert module.CONSUMERS == tuple(
        (row["name"], row["provider"], row["role"], row["image_key"])
        for row in inventory["consumers"]
    )
    # Retired runtimes keep only notification-disabled alarm identities.
    for retired in (
        "findb-fetcher-finlab-scheduler",
        "findb-fetcher-scheduler",
        "findb-fetcher-shioaji-scheduler",
    ):
        assert retired not in SCRIPT.read_text(encoding="utf-8")


def _host_layouts(script: str) -> dict[str, tuple[str, str]]:
    body = script.split("provider_host_layout() {\n", 1)[1].split("\n}\n", 1)[0]
    pattern = re.compile(r"^\s*([a-z-]+)\) printf '%s %s' (\S+) (\S+) ;;$", re.MULTILINE)
    return {suffix: (state, cache) for suffix, state, cache in pattern.findall(body)}


def test_mount_contract_matches_fetcher_deployment_paths() -> None:
    module = _load_module()
    deploy = DEPLOY.read_text(encoding="utf-8")
    release = RELEASE.read_text(encoding="utf-8")
    suffixes = {
        row[0].removeprefix("findb-historical-")
        for row in module.CONSUMERS
        if row[2] == "historical"
    }

    # Every consumer loop reads the deploy script's single host layout table.
    assert deploy.count('layout="$(provider_host_layout "$provider")"') == 3
    paths = _host_layouts(deploy)
    assert set(paths) == suffixes
    for suffix, (state, cache) in paths.items():
        provider = suffix.replace("-", "_")
        assert module.HISTORICAL_STATE_DIRS[provider] == state
        assert module.PROVIDER_CACHE_DIRS.get(provider, "-") == cache
    assert set(module.HISTORICAL_STATE_DIRS) == set(module.PROVIDERS)
    assert "\nfull_market_state=/var/lib/findb-full-market/$APP_ENVIRONMENT\n" in deploy
    assert (
        '\n[ "$APP_ENVIRONMENT" != production ] || full_market_state=/var/lib/findb-full-market\n'
    ) in deploy
    assert module.FULL_MARKET_STATE_DIRS == {
        "staging": "/var/lib/findb-full-market/staging",
        "production": "/var/lib/findb-full-market",
    }
    assert 'account_dir="/var/lib/findb-account/$APP_ENVIRONMENT"' in release
    assert (
        'account_mount=(--mount "type=bind,src=$account_dir,dst=$account_dir" --mount '
        '"type=bind,src=/etc/findb-full-market/readiness,dst=/var/lib/findb-account/readiness,'
        'readonly")'
    ) in release
    assert "readiness_dir=/etc/findb-full-market/readiness" in release
    assert (
        'readiness_mount=(--mount "type=bind,src=$readiness_dir,dst=$state_dir/readiness,readonly")'
    ) in release
    assert '  --mount "type=bind,src=$state_dir,dst=$state_dir"\n' in release
    assert 'common_args+=(--mount "type=bind,src=$cache_dir,dst=/home/fetcher")' in release


def test_anonymous_volume_allowlist_matches_image_declarations() -> None:
    module = _load_module()
    dockerfiles = {
        "factset": "Dockerfile.factset",
        "finlab": "Dockerfile.finlab",
        "twelve_data": "Dockerfile",
    }
    assert set(module.IMAGE_ANONYMOUS_VOLUMES) == {row[3] for row in module.CONSUMERS}
    for image_key, name in dockerfiles.items():
        text = (REPO_ROOT / "fetcher" / name).read_text(encoding="utf-8")
        declared = re.findall(r"^VOLUME (\[.*\])$", text, re.MULTILINE)
        assert len(declared) == 1
        assert module.IMAGE_ANONYMOUS_VOLUMES[image_key] == frozenset(json.loads(declared[0]))


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_every_consumer_has_an_exact_mount_contract(environment: str) -> None:
    module = _load_module()
    full_state = module.FULL_MARKET_STATE_DIRS[environment]
    account = f"/var/lib/findb-account/{environment}"
    readiness = "/etc/findb-full-market/readiness"

    contracts = {
        row[0]: module.expected_consumer_mounts(environment, *row[1:]) for row in module.CONSUMERS
    }

    assert set(contracts) == set(module.EXPECTED_RUNTIMES)
    assert contracts["findb-full-market-finlab"] == (
        frozenset(
            {
                ("bind", full_state, full_state, True),
                ("bind", account, account, True),
                ("bind", readiness, "/var/lib/findb-account/readiness", False),
                ("bind", readiness, f"{full_state}/readiness", False),
                ("bind", "/var/lib/findb-finlab-fetcher/cache", "/home/fetcher", True),
            }
        ),
        frozenset({"/var/lib/findb-finlab-fetcher"}),
    )
    history = "/var/lib/findb-fetcher/historical"
    assert contracts["findb-historical-twelve-data"] == (
        frozenset(
            {
                ("bind", history, history, True),
                ("bind", account, account, True),
                ("bind", readiness, "/var/lib/findb-account/readiness", False),
            }
        ),
        frozenset({"/var/lib/findb-fetcher"}),
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


def _secure_consumer(mounts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "Config": {"User": "10001:10001"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=16777216"},
        },
        "Mounts": mounts,
    }


def _bind(source: str, destination: str, rw: bool = True) -> dict[str, object]:
    return {"Type": "bind", "Source": source, "Destination": destination, "RW": rw}


def _anonymous(destination: str, name: str = "a" * 64) -> dict[str, object]:
    return {
        "Type": "volume",
        "Name": name,
        "Source": f"/var/lib/docker/volumes/{name}/_data",
        "Destination": destination,
        "Driver": "local",
        "RW": True,
    }


def test_fetcher_runtime_security_requires_exact_flags_and_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    container = "findb-historical-finlab"
    monkeypatch.setattr(module, "CONSUMERS", ((container, "finlab", "historical", "finlab"),))
    history = "/var/lib/findb-finlab-fetcher/historical"
    mounts = [
        _bind(history, history),
        _bind("/var/lib/findb-account/staging", "/var/lib/findb-account/staging"),
        _bind("/etc/findb-full-market/readiness", "/var/lib/findb-account/readiness", rw=False),
        _bind("/var/lib/findb-finlab-fetcher/cache", "/home/fetcher"),
        _anonymous("/var/lib/findb-finlab-fetcher"),
    ]
    valid = _secure_consumer(mounts)
    privileged = json.loads(json.dumps(valid))
    privileged["HostConfig"]["Privileged"] = True
    writable_readiness = json.loads(json.dumps(valid))
    writable_readiness["Mounts"][2]["RW"] = True
    cases = [
        (valid, 1),
        (_secure_consumer(mounts[:4]), 1),
        (privileged, 0),
        (writable_readiness, 0),
        (_secure_consumer([*mounts, _bind("/srv", "/srv")]), 0),
        (_secure_consumer(mounts[1:]), 0),
        (_secure_consumer([*mounts[:4], _anonymous("/var/lib/findb-finlab-fetcher", "data")]), 0),
        (_secure_consumer([*mounts, _anonymous("/home/fetcher/other")]), 0),
    ]
    monkeypatch.setattr(
        module,
        "_run",
        Mock(side_effect=[Mock(returncode=0, stdout=json.dumps([case])) for case, _ in cases]),
    )

    for _, healthy in cases:
        metrics, errors = module._fetcher_runtime_security_metrics("staging")
        assert errors == []
        assert metrics == [
            module._metric("DockerRuntimeSecurityHealthy", healthy, "Count", "fetcher", container)
        ]


def test_fetcher_runtime_security_uses_the_environment_full_market_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    container = "findb-full-market-taifex"
    monkeypatch.setattr(module, "CONSUMERS", ((container, "taifex", "full_market", "twelve_data"),))
    state = "/var/lib/findb-full-market"
    consumer = _secure_consumer(
        [
            _bind(state, state),
            _bind("/var/lib/findb-account/production", "/var/lib/findb-account/production"),
            _bind(
                "/etc/findb-full-market/readiness",
                "/var/lib/findb-account/readiness",
                rw=False,
            ),
            _bind("/etc/findb-full-market/readiness", f"{state}/readiness", rw=False),
            _anonymous("/var/lib/findb-fetcher"),
        ]
    )
    monkeypatch.setattr(
        module, "_run", Mock(return_value=Mock(returncode=0, stdout=json.dumps([consumer])))
    )

    production, _ = module._fetcher_runtime_security_metrics("production")
    staging, _ = module._fetcher_runtime_security_metrics("staging")

    assert production[0]["Value"] == 1
    assert staging[0]["Value"] == 0


def test_fetcher_runtime_security_fails_closed_without_a_mount_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    monkeypatch.setattr(
        module, "CONSUMERS", (("findb-historical-new", "new", "historical", "new"),)
    )
    monkeypatch.setattr(module, "_run", Mock())

    metrics, errors = module._fetcher_runtime_security_metrics("staging")

    assert metrics == [
        module._metric(
            "DockerRuntimeSecurityHealthy", 0, "Count", "fetcher", "findb-historical-new"
        )
    ]
    assert errors == ["no runtime mount contract for findb-historical-new"]
    module._run.assert_not_called()


@pytest.mark.parametrize(
    ("namespace", "environment"),
    [("FinDB/Staging", "staging"), ("FinDB/Production", "production")],
)
def test_main_derives_the_environment_from_the_namespace(
    monkeypatch: pytest.MonkeyPatch, namespace: str, environment: str
) -> None:
    module = _load_module()
    collect = Mock(return_value=([], []))
    monkeypatch.setattr(module, "collect_metrics", collect)
    monkeypatch.setattr(module, "publish_metrics", Mock())
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "publish_metrics.py",
            "--unit",
            "fetcher",
            "--region",
            "ap-southeast-1",
            "--dlm-policy-id",
            "policy-0123456789abcdef0",
            "--tls-host",
            "findb.example.com",
            "--namespace",
            namespace,
        ],
    )

    assert module.main() == 0
    assert collect.call_args.args[-1] == environment


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
            "scheduler_key": "full_market_finlab_v1",
            "heartbeat_age_seconds": 12.5,
            "last_error": "must not become a metric dimension",
        },
        # A stopped control publishes nothing, so its heartbeat alarm sees
        # missing data instead of a stale age.
        {
            "scheduler_key": "full_market_factset_v1",
            "desired_state": "stopped",
            "heartbeat_age_seconds": 9999.0,
        },
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
            "full_market_finlab_v1",
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
                    stderr=(
                        "API credential rejected credential_kind=serve endpoint="
                        "'/API credential rejected' client_ip='203.0.113.7'\n"
                        "API credential rejected\n"
                    ),
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
