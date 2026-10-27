#!/usr/bin/env python3
"""Wait for installed Fetcher runtimes to recover after a FinDB rollout."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

PROFILES = ("pilot", "full-market", "historical")
PROVIDERS = ("twelve-data", "finlab", "shioaji", "taifex", "factset")
EXPECTED_RUNTIMES = tuple(
    f"findb-{profile}-{provider}" for profile in PROFILES for provider in PROVIDERS
)

RuntimeSnapshot = tuple[tuple[str, str, str, int, str, bool], ...]


class RecoveryTimeoutError(RuntimeError):
    """The installed runtimes did not remain healthy for the required window."""


def _parse_runtime_rows(rows: object) -> RuntimeSnapshot:
    if not isinstance(rows, list) or len(rows) != len(EXPECTED_RUNTIMES):
        raise ValueError("runtime inventory is incomplete")
    by_name: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("Name"), str):
            raise ValueError("runtime inventory row is invalid")
        name = row["Name"].removeprefix("/")
        if name in by_name:
            raise ValueError("runtime inventory contains duplicate names")
        by_name[name] = row
    if set(by_name) != set(EXPECTED_RUNTIMES):
        raise ValueError("runtime inventory identity differs")

    snapshot = []
    for name in EXPECTED_RUNTIMES:
        row = by_name[name]
        config, state = row.get("Config"), row.get("State")
        if not isinstance(config, dict) or not isinstance(state, dict):
            raise ValueError("runtime state is invalid")
        labels = config.get("Labels") or {}
        if not isinstance(labels, dict):
            raise ValueError("runtime labels are invalid")
        container_id = row.get("Id")
        image = config.get("Image")
        restart_count = row.get("RestartCount")
        started_at = state.get("StartedAt")
        if (
            not isinstance(container_id, str)
            or not container_id
            or not isinstance(image, str)
            or not image
            or type(restart_count) is not int
            or restart_count < 0
            or not isinstance(started_at, str)
            or not started_at
        ):
            raise ValueError("runtime recovery identity is invalid")
        healthy = (
            labels.get("com.findb.fetcher.accepted") == "true"
            and state.get("Running") is True
            and state.get("Status") == "running"
            and state.get("ExitCode") == 0
            and state.get("OOMKilled") is False
            and state.get("Error") == ""
        )
        snapshot.append((name, container_id, image, restart_count, started_at, healthy))
    return tuple(snapshot)


def read_runtime_snapshot() -> RuntimeSnapshot:
    inventory = subprocess.run(
        ["docker", "container", "ls", "--all", "--format", "{{.Names}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.splitlines()
    present = set(inventory) & set(EXPECTED_RUNTIMES)
    if not present:
        current = Path("/opt/fetcher/current")
        if current.exists() or current.is_symlink():
            raise ValueError("installed runtime inventory is missing")
        return ()
    if present != set(EXPECTED_RUNTIMES):
        raise ValueError("installed runtime inventory is incomplete")
    result = subprocess.run(
        ["docker", "inspect", *EXPECTED_RUNTIMES],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return _parse_runtime_rows(json.loads(result.stdout))


def wait_until_recovered(
    snapshot_reader: Callable[[], RuntimeSnapshot] = read_runtime_snapshot,
    *,
    stable_seconds: float,
    timeout_seconds: float,
    poll_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> RuntimeSnapshot:
    if (
        stable_seconds <= 0
        or timeout_seconds < stable_seconds
        or poll_seconds <= 0
        or poll_seconds > stable_seconds
    ):
        raise ValueError("runtime recovery timing is invalid")
    deadline = monotonic() + timeout_seconds
    baseline: RuntimeSnapshot | None = None
    stable_since: float | None = None
    while True:
        try:
            snapshot = snapshot_reader()
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            snapshot = None
        now = monotonic()
        if snapshot == ():
            return snapshot
        if snapshot is not None and all(row[-1] for row in snapshot):
            if snapshot != baseline:
                baseline = snapshot
                stable_since = now
            elif stable_since is not None and now - stable_since >= stable_seconds:
                return snapshot
        else:
            baseline = None
            stable_since = None
        remaining = deadline - now
        if remaining <= 0:
            raise RecoveryTimeoutError("runtime_not_recovered_after_findb_rollout")
        sleep(min(poll_seconds, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stable-seconds", type=float, default=60)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args()
    try:
        snapshot = wait_until_recovered(
            stable_seconds=args.stable_seconds,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except (RecoveryTimeoutError, ValueError):
        print(
            "fetcher_aws_deploy=failed reason=runtime_not_recovered_after_findb_rollout",
            flush=True,
        )
        return 1
    runtime_count = len(EXPECTED_RUNTIMES)
    stable_seconds = args.stable_seconds
    if not snapshot:
        runtime_count = 0
        stable_seconds = 0
    print(
        f"fetcher_runtime_recovery=ready runtimes={runtime_count} stable_seconds={stable_seconds:g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
