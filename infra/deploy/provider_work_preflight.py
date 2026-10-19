"""Read-only single-host/shared-inode/provider protocol cutover attestation."""

import argparse
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROVIDERS = ("twelve_data", "finlab", "shioaji", "taifex")


def inspect_cutover(*, environment, host_id, host_ids, containers):
    if environment not in {"local", "staging", "production"} or host_ids != [host_id]:
        raise ValueError("exactly one Fetcher host per environment is required")
    names = {row["Name"].removeprefix("/"): row for row in containers}
    expected = {
        f"findb-{profile.replace('_', '-')}-{provider.replace('_', '-')}"
        for profile in ("pilot", "full_market", "historical")
        for provider in PROVIDERS
    }
    if set(names) != expected:
        raise ValueError("all provider consumers must be present; unknown consumers are forbidden")
    mount_sources = set()
    for row in names.values():
        state, config = row["State"], row["Config"]
        env = dict(value.split("=", 1) for value in config["Env"] if "=" in value)
        if state["Running"] or state["ExitCode"] != 0 or state["OOMKilled"] or state.get("Error"):
            raise ValueError("all consumers must be stopped and gracefully drained")
        if (
            env.get("APP_ENVIRONMENT") != environment
            or env.get("PROVIDER_WORK_REQUIRED") != "true"
            or config.get("Labels", {}).get("com.findb.provider-work.protocol") != "v1"
        ):
            raise ValueError("every consumer must use the enforced provider-work protocol")
        target = f"/var/lib/findb-account/{environment}"
        if env.get("FETCHER_ACCOUNT_STATE_PATH") != target + "/governor.sqlite3":
            raise ValueError("provider account mount identity differs")
        mounts = [mount for mount in row["Mounts"] if mount["Destination"] == target]
        if len(mounts) != 1 or mounts[0]["Type"] != "bind" or not mounts[0]["RW"]:
            raise ValueError("every consumer needs the same writable host bind mount")
        mount_sources.add(mounts[0]["Source"])
    if len(mount_sources) != 1:
        raise ValueError("provider consumers do not share one account lock inode namespace")
    root = Path(next(iter(mount_sources)))
    descriptors = []
    identities = {}
    try:
        for provider in PROVIDERS:
            path = root / f"provider-work-{environment}-{provider}.lock"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            descriptors.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            identities[provider] = f"{environment}:{info.st_dev}:{info.st_ino}"
        digest = hashlib.sha256(
            json.dumps(
                {"host_ids": host_ids, "containers": containers},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return {
            "protocol": "provider-work-v1",
            "environment": environment,
            "host_id": host_id,
            "inventory_sha256": digest,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "host_lock_identities": identities,
        }
    finally:
        for fd in descriptors:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument(
        "--host-inventory",
        type=Path,
        required=True,
        help="Fresh trusted deployment inventory containing only this environment's Fetcher host IDs",
    )
    parser.add_argument(
        "--docker-inspect",
        type=Path,
        required=True,
        help="Complete docker inspect JSON for all provider consumers",
    )
    args = parser.parse_args()
    try:
        proof = inspect_cutover(
            environment=args.environment,
            host_id=args.host_id,
            host_ids=json.loads(args.host_inventory.read_text()),
            containers=json.loads(args.docker_inspect.read_text()),
        )
    except (ValueError, KeyError, OSError, TypeError):
        raise SystemExit("provider_work_cutover=failed") from None
    print(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
