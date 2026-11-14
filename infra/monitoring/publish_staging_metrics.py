#!/usr/bin/env python3
"""Publish bounded, non-secret FinDB staging and production operational metrics."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_NAMESPACE = "FinDB/Staging"
ALLOWED_NAMESPACES = (DEFAULT_NAMESPACE, "FinDB/Production")
ENVIRONMENT_BY_NAMESPACE = {DEFAULT_NAMESPACE: "staging", "FinDB/Production": "production"}

# The Fetcher consumer inventory is generated from the provider catalog by
# backend/scripts/export_provider_catalog.py; never edit it by hand. OpenTofu
# derives the matching alarm identities from the same generated inventory.
# BEGIN GENERATED PROVIDER INVENTORY
PROVIDERS = ("factset", "finlab", "taifex", "twelve_data")
CONSUMERS = (
    ("findb-full-market-factset", "factset", "full_market", "factset"),
    ("findb-full-market-finlab", "finlab", "full_market", "finlab"),
    ("findb-full-market-taifex", "taifex", "full_market", "twelve_data"),
    ("findb-full-market-twelve-data", "twelve_data", "full_market", "twelve_data"),
    ("findb-historical-factset", "factset", "historical", "factset"),
    ("findb-historical-finlab", "finlab", "historical", "finlab"),
    ("findb-historical-taifex", "taifex", "historical", "twelve_data"),
    ("findb-historical-twelve-data", "twelve_data", "historical", "twelve_data"),
)
EXPECTED_RUNTIMES = tuple(row[0] for row in CONSUMERS)
# END GENERATED PROVIDER INVENTORY

EXPECTED_CONTAINERS = {
    "findb": (
        "findb-dashboard",
        "findb-dispatcher",
        "findb-ingest",
        "findb-nginx",
        "findb-rabbitmq",
        "findb-raw-cleanup",
        "findb-serve",
        "findb-worker",
    ),
    "fetcher": EXPECTED_RUNTIMES,
}

# Host paths mirror infra/deploy/runtime-secrets/deploy_fetcher_aws.sh and
# release_fetcher_provider.sh; infra tests pin both sides together.
FULL_MARKET_STATE_DIRS = {
    "staging": "/var/lib/findb-full-market/staging",
    "production": "/var/lib/findb-full-market",
}
HISTORICAL_STATE_DIRS = {
    "factset": "/var/lib/findb-factset-fetcher/historical",
    "finlab": "/var/lib/findb-finlab-fetcher/historical",
    "taifex": "/var/lib/findb-taifex-fetcher/historical",
    "twelve_data": "/var/lib/findb-fetcher/historical",
}
PROVIDER_CACHE_DIRS = {"finlab": "/var/lib/findb-finlab-fetcher/cache"}
ACCOUNT_STATE_ROOT = "/var/lib/findb-account"
READINESS_DIR = "/etc/findb-full-market/readiness"
# Image VOLUME paths (fetcher/Dockerfile*) that no bind covers exactly receive
# Docker-managed anonymous volumes. They are container-private and never expose
# a host path, so they are the only permitted non-bind mounts.
IMAGE_ANONYMOUS_VOLUMES = {
    "factset": frozenset({"/var/lib/findb-fetcher"}),
    "finlab": frozenset({"/home/fetcher", "/var/lib/findb-finlab-fetcher"}),
    "twelve_data": frozenset({"/var/lib/findb-fetcher"}),
}
_ANONYMOUS_VOLUME_NAME = re.compile(r"[0-9a-f]{64}")
INVALID_CREDENTIAL_LOG_CONTAINERS = (
    "findb-ingest",
    "findb-serve",
)


def _run(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _metric(
    name: str, value: float, unit: str, deployment_unit: str, resource: str
) -> dict[str, Any]:
    return {
        "MetricName": name,
        "Dimensions": [
            {"Name": "DeploymentUnit", "Value": deployment_unit},
            {"Name": "Resource", "Value": resource},
        ],
        "Value": value,
        "Unit": unit,
    }


def _filesystem_metrics(deployment_unit: str, path: Path) -> list[dict[str, Any]]:
    stats = os.statvfs(path)
    used_blocks = stats.f_blocks - stats.f_bfree
    used_inodes = stats.f_files - stats.f_ffree
    disk_used = 100.0 * used_blocks / stats.f_blocks if stats.f_blocks else 0.0
    inode_used = 100.0 * used_inodes / stats.f_files if stats.f_files else 0.0
    return [
        _metric("DiskUsedPercent", disk_used, "Percent", deployment_unit, str(path)),
        _metric("InodeUsedPercent", inode_used, "Percent", deployment_unit, str(path)),
    ]


def _container_metrics(deployment_unit: str) -> tuple[list[dict[str, Any]], list[str]]:
    metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    for container in EXPECTED_CONTAINERS[deployment_unit]:
        result = _run(["docker", "inspect", container])
        if result.returncode != 0:
            metrics.append(
                _metric("DockerContainerHealthy", 0, "Count", deployment_unit, container)
            )
            continue
        try:
            state = json.loads(result.stdout)[0]
            container_state = state["State"]
            running = bool(container_state["Running"])
            health = container_state.get("Health", {}).get("Status")
            healthy = running and health not in {"starting", "unhealthy"}
            restart_count = int(state["RestartCount"])
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(f"docker inspect parse failed for {container}: {exc}")
            continue
        metrics.extend(
            [
                _metric(
                    "DockerContainerHealthy",
                    int(healthy),
                    "Count",
                    deployment_unit,
                    container,
                ),
                _metric(
                    "DockerRestartCount",
                    restart_count,
                    "Count",
                    deployment_unit,
                    container,
                ),
            ]
        )
    return metrics, errors


def expected_consumer_mounts(
    environment: str, provider: str, role: str, image_key: str
) -> tuple[frozenset[tuple[str, str, str, bool]], frozenset[str]]:
    """Return one consumer's exact bind mounts and permitted anonymous volumes."""
    state_dir = (
        FULL_MARKET_STATE_DIRS[environment]
        if role == "full_market"
        else HISTORICAL_STATE_DIRS[provider]
    )
    account_dir = f"{ACCOUNT_STATE_ROOT}/{environment}"
    binds = {
        ("bind", state_dir, state_dir, True),
        ("bind", account_dir, account_dir, True),
        ("bind", READINESS_DIR, f"{ACCOUNT_STATE_ROOT}/readiness", False),
    }
    if role == "full_market":
        binds.add(("bind", READINESS_DIR, f"{state_dir}/readiness", False))
    if provider in PROVIDER_CACHE_DIRS:
        binds.add(("bind", PROVIDER_CACHE_DIRS[provider], "/home/fetcher", True))
    bound = {destination for _, _, destination, _ in binds}
    return frozenset(binds), IMAGE_ANONYMOUS_VOLUMES[image_key] - bound


def _mounts_match(
    mounts: list[dict[str, Any]],
    expected_binds: frozenset[tuple[str, str, str, bool]],
    anonymous_volumes: frozenset[str],
) -> bool:
    binds = set()
    for mount in mounts:
        if mount["Type"] == "bind":
            binds.add(
                (
                    "bind",
                    str(mount["Source"]),
                    str(mount["Destination"]),
                    bool(mount["RW"]),
                )
            )
        elif not (
            mount["Type"] == "volume"
            and mount.get("Driver") == "local"
            and _ANONYMOUS_VOLUME_NAME.fullmatch(str(mount.get("Name", "")))
            and str(mount["Destination"]) in anonymous_volumes
        ):
            return False
    return binds == expected_binds


def _fetcher_runtime_security_metrics(
    environment: str = "staging",
) -> tuple[list[dict[str, Any]], list[str]]:
    metrics: list[dict[str, Any]] = []
    errors: list[str] = []
    for container, provider, role, image_key in CONSUMERS:
        try:
            expected_binds, anonymous_volumes = expected_consumer_mounts(
                environment, provider, role, image_key
            )
        except KeyError:
            errors.append(f"no runtime mount contract for {container}")
            metrics.append(
                _metric("DockerRuntimeSecurityHealthy", 0, "Count", "fetcher", container)
            )
            continue
        result = _run(["docker", "inspect", container])
        if result.returncode != 0:
            metrics.append(
                _metric("DockerRuntimeSecurityHealthy", 0, "Count", "fetcher", container)
            )
            continue
        try:
            state = json.loads(result.stdout)[0]
            config = state["Config"]
            host_config = state["HostConfig"]
            tmpfs = host_config.get("Tmpfs") or {}
            tmp_options = set(str(tmpfs.get("/tmp", "")).split(","))
            security_options = set(host_config.get("SecurityOpt") or [])
            secure = (
                config["User"] == "10001:10001"
                and host_config["ReadonlyRootfs"] is True
                and host_config["Privileged"] is False
                and set(host_config.get("CapDrop") or []) == {"ALL"}
                and not (host_config.get("CapAdd") or [])
                and security_options in ({"no-new-privileges"}, {"no-new-privileges:true"})
                and set(tmpfs) == {"/tmp"}
                and {"rw", "noexec", "nosuid"}.issubset(tmp_options)
                and _mounts_match(state["Mounts"], expected_binds, anonymous_volumes)
            )
        except (
            KeyError,
            IndexError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            errors.append(f"docker security inspect parse failed for {container}: {exc}")
            continue
        metrics.append(
            _metric(
                "DockerRuntimeSecurityHealthy",
                int(secure),
                "Count",
                "fetcher",
                container,
            )
        )
    return metrics, errors


def _tls_certificate_metrics(hostname: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        context = ssl.create_default_context()
        with socket.create_connection((hostname, 443), timeout=10) as connection:
            with context.wrap_socket(connection, server_hostname=hostname) as tls:
                certificate = tls.getpeercert()
        not_after = str(certificate["notAfter"])
        expires_at = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), timezone.utc)
        days_remaining = (expires_at - datetime.now(timezone.utc)).total_seconds() / 86400
    except (KeyError, OSError, TypeError, ValueError, ssl.SSLError):
        return [], ["TLS certificate query or verification failed"]
    return [
        _metric(
            "TLSCertificateDaysRemaining",
            days_remaining,
            "Count",
            "findb",
            hostname,
        )
    ], []


def _rabbitmq_metrics() -> tuple[list[dict[str, Any]], list[str]]:
    result = _run(
        [
            "docker",
            "exec",
            "findb-rabbitmq",
            "rabbitmq-diagnostics",
            "-q",
            "alarms",
            "--formatter",
            "json",
        ]
    )
    if result.returncode != 0:
        return [], ["RabbitMQ alarm query failed"]
    try:
        alarms = json.loads(result.stdout).get("alarms", [])
    except (AttributeError, json.JSONDecodeError):
        return [], ["RabbitMQ alarm response was invalid"]
    alarm_text = " ".join(str(alarm).lower() for alarm in alarms)
    return [
        _metric(
            "RabbitMQDiskAlarm",
            int("disk" in alarm_text),
            "Count",
            "findb",
            "findb-rabbitmq",
        ),
        _metric(
            "RabbitMQMemoryAlarm",
            int("memory" in alarm_text),
            "Count",
            "findb",
            "findb-rabbitmq",
        ),
    ], []


def _scheduler_metrics() -> tuple[list[dict[str, Any]], list[str]]:
    script = (
        "import json,os; from urllib.request import Request,urlopen; "
        "r=Request('http://127.0.0.1:8080/api/v1/admin/market-freshness?include_feeds=true',"
        "headers={'X-API-Key':os.environ['FINDB_QUEUE_HEALTH_ADMIN_API_KEY']}); "
        "print(json.dumps(json.load(urlopen(r,timeout=15)).get('data',[])))"
    )
    result = _run(["docker", "exec", "findb-ingest", "python", "-c", script])
    if result.returncode != 0:
        return [], ["scheduler heartbeat query failed"]
    try:
        schedulers = json.loads(result.stdout)
        metrics = []
        for scheduler in schedulers:
            if scheduler.get("desired_state") == "stopped":
                continue
            scheduler_key = str(scheduler["scheduler_key"])
            heartbeat_age = scheduler.get("heartbeat_age_seconds")
            if heartbeat_age is not None:
                metrics.append(
                    _metric(
                        "SchedulerHeartbeatAgeSeconds",
                        float(heartbeat_age),
                        "Seconds",
                        "fetcher",
                        scheduler_key,
                    )
                )
            if "status" in scheduler:
                metrics.append(
                    _metric(
                        "FeedFreshnessHealthy",
                        int(scheduler.get("status") in {"not_due", "fresh"}),
                        "Count",
                        "fetcher",
                        scheduler_key,
                    )
                )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [], ["scheduler heartbeat response was invalid"]
    if not metrics and any(row.get("desired_state") != "stopped" for row in schedulers):
        return [], ["active scheduler response had no measurements"]
    return metrics, []


def _active_feed_ingestion_metrics() -> tuple[list[dict[str, Any]], list[str]]:
    """Publish recent, bounded failure signals for the five active feeds.

    The ten-minute lookback overlaps the five-minute publisher interval so a
    single event cannot fall between samples. Values are deliberately
    aggregated across the fixed active-feed scope; request IDs, symbols, and
    provider error text must never become CloudWatch dimensions.
    """
    script = """
import asyncio
import json

from sqlalchemy import text

from app.models.base import async_session_maker, engine

QUERY = text(\"\"\"
WITH active_feed(dataset_key, source) AS (
    VALUES
        ('us_equity_eod', 'twelve_data'),
        ('tw_equity_eod', 'finlab'),
        ('tw_etf_eod', 'finlab'),
        ('tw_futures_eod', 'taifex'),
        ('hk_equity_eod', 'factset')
)
SELECT
    (
        SELECT count(*)
        FROM ingestion_attempt a
        JOIN active_feed f USING (dataset_key, source)
        WHERE a.status = 'rejected'
          AND a.completed_at >= now() - interval '10 minutes'
          AND (
              a.failure_code LIKE 'INGRESS_%'
              OR a.failure_code IN (
                  'CURRENCY_REQUIRED',
                  'BATCH_RECORD_COUNT_BELOW_MINIMUM',
                  'BATCH_RECORD_COUNT_DROP'
              )
          )
    ) AS rejected_attempts,
    (
        SELECT count(*)
        FROM ingestion_run r
        JOIN active_feed f USING (dataset_key, source)
        WHERE r.is_rerun IS false
          AND r.total_records = 0
          AND COALESCE(r.completed_at, r.started_at) >= now() - interval '10 minutes'
    ) AS empty_snapshots,
    (
        SELECT count(*)
        FROM dq_issue d
        JOIN ingestion_run r ON r.run_id = d.run_id
        JOIN active_feed f USING (dataset_key, source)
        WHERE d.severity = 'error'
          AND d.created_at >= now() - interval '10 minutes'
    ) AS dq_errors
\"\"\")

async def main():
    async with async_session_maker() as session:
        row = (await session.execute(QUERY)).mappings().one()
        print(json.dumps(dict(row)))
    await engine.dispose()

asyncio.run(main())
"""
    result = _run(["docker", "exec", "findb-ingest", "python", "-c", script])
    if result.returncode != 0:
        return [], ["active-feed ingestion-quality query failed"]
    try:
        values = json.loads(result.stdout)
        metrics = [
            _metric(
                "ActiveFeedRejectedAttempts",
                float(values["rejected_attempts"]),
                "Count",
                "findb",
                "active-feeds",
            ),
            _metric(
                "ActiveFeedEmptySnapshots",
                float(values["empty_snapshots"]),
                "Count",
                "findb",
                "active-feeds",
            ),
            _metric(
                "ActiveFeedDQErrors",
                float(values["dq_errors"]),
                "Count",
                "findb",
                "active-feeds",
            ),
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [], ["active-feed ingestion-quality response was invalid"]
    return metrics, []


def _database_governance_metrics() -> tuple[list[dict[str, Any]], list[str]]:
    """Publish bounded integrity checks without exposing row identifiers."""
    script = """
import asyncio
import json

from sqlalchemy import text

from app.models.base import async_session_maker, engine

QUERY = text(\"\"\"
WITH lineage_orphans AS (
    SELECT count(*) AS orphan_rows
    FROM raw.market_payload c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
    UNION ALL
    SELECT count(*) FROM market_data_eod c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
    UNION ALL
    SELECT count(*) FROM market_data_minute c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
    UNION ALL
    SELECT count(*) FROM corporate_action c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
    UNION ALL
    SELECT count(*) FROM bond_eod c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
    UNION ALL
    SELECT count(*) FROM macro_observation c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
    UNION ALL
    SELECT count(*) FROM futures_contract c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
    UNION ALL
    SELECT count(*) FROM futures_continuous_eod c
    LEFT JOIN ingestion_run r ON r.run_id = c.run_id
    WHERE c.run_id IS NOT NULL AND r.run_id IS NULL
), credential_inventory AS (
    SELECT 'source'::text AS credential_kind, client_id AS credential_id, usage_count
    FROM source_client
    UNION ALL
    SELECT kind, key_id, usage_count
    FROM api_key
), credential_mismatches AS (
    SELECT count(*) AS mismatch_rows
    FROM credential_inventory credential
    FULL OUTER JOIN credential_usage_rollup rollup
      USING (credential_kind, credential_id)
    WHERE COALESCE(credential.usage_count, 0) <> COALESCE(rollup.request_count, 0)
)
SELECT
    (SELECT COALESCE(sum(orphan_rows), 0)::bigint FROM lineage_orphans) AS lineage_orphans,
    (SELECT count(*) FROM market_data_eod_default) AS eod_default_rows,
    (SELECT mismatch_rows FROM credential_mismatches) AS credential_usage_mismatches
\"\"\")

async def main():
    async with async_session_maker() as session:
        row = (await session.execute(QUERY)).mappings().one()
        print(json.dumps(dict(row)))
    await engine.dispose()

asyncio.run(main())
"""
    result = _run(["docker", "exec", "findb-ingest", "python", "-c", script])
    if result.returncode != 0:
        return [], ["database governance query failed"]
    try:
        values = json.loads(result.stdout)
        metrics = [
            _metric(
                "LineageOrphanRows",
                float(values["lineage_orphans"]),
                "Count",
                "findb",
                "canonical-and-raw",
            ),
            _metric(
                "EODDefaultPartitionRows",
                float(values["eod_default_rows"]),
                "Count",
                "findb",
                "market_data_eod_default",
            ),
            _metric(
                "CredentialUsageAggregateMismatches",
                float(values["credential_usage_mismatches"]),
                "Count",
                "findb",
                "api-credentials",
            ),
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [], ["database governance response was invalid"]
    return metrics, []


def _invalid_credential_event_metrics() -> tuple[list[dict[str, Any]], list[str]]:
    """Count only the stable rejection message in an overlapping time window."""
    events = 0
    for container in INVALID_CREDENTIAL_LOG_CONTAINERS:
        result = _run(["docker", "logs", "--since", "10m", container])
        if result.returncode != 0:
            return [], [f"invalid-credential log query failed for {container}"]
        # One event per line; the line also carries the request path, which may repeat
        # the message text.
        events += sum(
            "API credential rejected" in line
            for line in (result.stdout + result.stderr).splitlines()
        )
    return [
        _metric(
            "InvalidCredentialEvents",
            events,
            "Count",
            "findb",
            "api-credentials",
        )
    ], []


def _rds_backup_lag_metric(
    region: str, db_identifier: str
) -> tuple[list[dict[str, Any]], list[str]]:
    result = _run(
        [
            "aws",
            "rds",
            "describe-db-instances",
            "--region",
            region,
            "--db-instance-identifier",
            db_identifier,
            "--query",
            "DBInstances[0].LatestRestorableTime",
            "--output",
            "text",
        ]
    )
    if result.returncode != 0:
        return [], ["RDS latest-restorable-time query failed"]
    try:
        latest = datetime.fromisoformat(result.stdout.strip().replace("Z", "+00:00"))
        lag = max(0.0, (datetime.now(timezone.utc) - latest).total_seconds())
    except ValueError:
        return [], ["RDS latest-restorable-time response was invalid"]
    return [_metric("RDSBackupLagSeconds", lag, "Seconds", "findb", db_identifier)], []


def _dlm_policy_health_metric(
    region: str, policy_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    result = _run(
        [
            "aws",
            "dlm",
            "get-lifecycle-policy",
            "--region",
            region,
            "--policy-id",
            policy_id,
            "--output",
            "json",
        ]
    )
    if result.returncode != 0:
        return [], ["DLM lifecycle-policy query failed"]
    try:
        policy = json.loads(result.stdout)["Policy"]
        healthy = policy["State"] == "ENABLED" and policy["StatusMessage"] == "ENABLED"
    except (KeyError, TypeError, json.JSONDecodeError):
        return [], ["DLM lifecycle-policy response was invalid"]
    return [_metric("DLMPolicyHealthy", int(healthy), "Count", "findb", policy_id)], []


def collect_metrics(
    unit: str,
    region: str,
    db_identifier: str,
    dlm_policy_id: str,
    tls_host: str,
    environment: str = "staging",
) -> tuple[list[dict[str, Any]], list[str]]:
    metrics = _filesystem_metrics(unit, Path("/"))
    container_metrics, errors = _container_metrics(unit)
    metrics.extend(container_metrics)
    if unit == "findb":
        for collector in (
            _rabbitmq_metrics,
            _scheduler_metrics,
            _active_feed_ingestion_metrics,
            _database_governance_metrics,
            _invalid_credential_event_metrics,
        ):
            collected, collector_errors = collector()
            metrics.extend(collected)
            errors.extend(collector_errors)
        rds_metrics, rds_errors = _rds_backup_lag_metric(region, db_identifier)
        metrics.extend(rds_metrics)
        errors.extend(rds_errors)
        dlm_metrics, dlm_errors = _dlm_policy_health_metric(region, dlm_policy_id)
        metrics.extend(dlm_metrics)
        errors.extend(dlm_errors)
        tls_metrics, tls_errors = _tls_certificate_metrics(tls_host)
        metrics.extend(tls_metrics)
        errors.extend(tls_errors)
    else:
        security_metrics, security_errors = _fetcher_runtime_security_metrics(environment)
        metrics.extend(security_metrics)
        errors.extend(security_errors)
    metrics.append(_metric("CollectorSuccess", int(not errors), "Count", unit, "host"))
    return metrics, errors


def publish_metrics(
    metrics: list[dict[str, Any]], region: str, namespace: str = DEFAULT_NAMESPACE
) -> None:
    if namespace not in ALLOWED_NAMESPACES:
        raise ValueError("unsupported CloudWatch namespace")
    result = _run(
        [
            "aws",
            "cloudwatch",
            "put-metric-data",
            "--region",
            region,
            "--namespace",
            namespace,
            "--metric-data",
            json.dumps(metrics, separators=(",", ":")),
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("CloudWatch PutMetricData failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", choices=sorted(EXPECTED_CONTAINERS), required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--rds-instance-identifier", default="fin-db")
    parser.add_argument("--dlm-policy-id", required=True)
    parser.add_argument("--tls-host", required=True)
    parser.add_argument("--namespace", choices=ALLOWED_NAMESPACES, default=DEFAULT_NAMESPACE)
    args = parser.parse_args()

    metrics, errors = collect_metrics(
        args.unit,
        args.region,
        args.rds_instance_identifier,
        args.dlm_policy_id,
        args.tls_host,
        ENVIRONMENT_BY_NAMESPACE[args.namespace],
    )
    publish_metrics(metrics, args.region, args.namespace)
    for error in errors:
        print(error, file=sys.stderr)
    print(f"published_metrics={len(metrics)} unit={args.unit}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
