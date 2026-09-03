#!/usr/bin/env python3
"""Publish bounded, non-secret FinDB staging operational metrics."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NAMESPACE = "FinDB/Staging"
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
    "fetcher": (
        "findb-fetcher-finlab-scheduler",
        "findb-fetcher-scheduler",
        "findb-fetcher-shioaji-scheduler",
    ),
}


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
                _metric(
                    "DockerContainerHealthy", 0, "Count", deployment_unit, container
                )
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
        metrics = [
            _metric(
                "SchedulerHeartbeatAgeSeconds",
                float(scheduler["heartbeat_age_seconds"]),
                "Seconds",
                "fetcher",
                str(scheduler["scheduler_key"]),
            )
            for scheduler in schedulers
            if scheduler.get("heartbeat_age_seconds") is not None
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [], ["scheduler heartbeat response was invalid"]
    if not metrics:
        return [], ["scheduler heartbeat response had no measurements"]
    return metrics, []


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


def collect_metrics(
    unit: str, region: str, db_identifier: str
) -> tuple[list[dict[str, Any]], list[str]]:
    metrics = _filesystem_metrics(unit, Path("/"))
    container_metrics, errors = _container_metrics(unit)
    metrics.extend(container_metrics)
    if unit == "findb":
        for collector in (_rabbitmq_metrics, _scheduler_metrics):
            collected, collector_errors = collector()
            metrics.extend(collected)
            errors.extend(collector_errors)
        rds_metrics, rds_errors = _rds_backup_lag_metric(region, db_identifier)
        metrics.extend(rds_metrics)
        errors.extend(rds_errors)
    metrics.append(_metric("CollectorSuccess", int(not errors), "Count", unit, "host"))
    return metrics, errors


def publish_metrics(metrics: list[dict[str, Any]], region: str) -> None:
    result = _run(
        [
            "aws",
            "cloudwatch",
            "put-metric-data",
            "--region",
            region,
            "--namespace",
            NAMESPACE,
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
    args = parser.parse_args()

    metrics, errors = collect_metrics(
        args.unit, args.region, args.rds_instance_identifier
    )
    publish_metrics(metrics, args.region)
    for error in errors:
        print(error, file=sys.stderr)
    print(f"published_metrics={len(metrics)} unit={args.unit}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
