"""Generate local installed-process evidence from a running full-market worker."""

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def installed_artifact_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.py")):
        if path.is_symlink():
            raise ValueError("installed source cannot contain symlinks")
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def process_environment(raw: str, *, allow_legacy: bool = False) -> tuple[str, str]:
    canonical = re.search(r"(?:^|\s)APP_ENVIRONMENT=([^\s]+)", raw)
    legacy = re.search(r"(?:^|\s)DEPLOYMENT_TARGET=([^\s]+)", raw)
    if canonical is not None:
        if legacy is not None and legacy.group(1) != canonical.group(1):
            raise ValueError("installed canonical and legacy environment metadata conflict")
        return canonical.group(1), "app-environment-v1"
    if allow_legacy and legacy is not None:
        return legacy.group(1), "legacy-deployment-target-v1"
    raise ValueError("installed APP_ENVIRONMENT unavailable; explicit legacy inspection required")


def main() -> None:
    from findb_fetcher import full_market_cli

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--governor", type=Path, required=True)
    parser.add_argument(
        "--consumer-pid",
        action="append",
        default=[],
        help="Actual installed extra consumer: pilot=PID or historical=PID",
    )
    parser.add_argument("--allocation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-legacy-environment", action="store_true")
    args = parser.parse_args()
    command = subprocess.check_output(
        ["ps", "-p", str(args.pid), "-o", "command="], text=True
    ).strip()
    if (
        "findb-fetch-full-market" not in command and "findb_fetcher.full_market_cli" not in command
    ) or str(args.config) not in command:
        raise ValueError("PID is not the configured installed full-market worker")
    config = full_market_cli.load_config(args.config)
    if config["deployment_target"] != "local" or not args.governor.exists():
        raise ValueError("local runtime/governor not installed")
    allocation = json.loads(args.allocation.read_bytes())
    environment = subprocess.check_output(
        ["ps", "eww", "-p", str(args.pid), "-o", "command="], text=True
    )
    key = re.search(r"(?:^|\s)SOURCE_CLIENT_KEY=([^\s]+)", environment)
    governor_env = re.search(r"(?:^|\s)FETCHER_ACCOUNT_STATE_PATH=([^\s]+)", environment)
    if governor_env is None or governor_env.group(1) != str(args.governor):
        raise ValueError("installed governor environment differs from receipt")
    target, environment_contract = process_environment(
        environment, allow_legacy=args.allow_legacy_environment
    )
    flag = re.search(r"(?:^|\s)FULL_MARKET_ENABLED=([^\s]+)", environment)
    if target != "local" or flag is None or flag.group(1).lower() != "true":
        raise ValueError("installed environment/flag differs from local enrollment")
    allocation_env = re.search(r"(?:^|\s)FETCHER_ACCOUNT_ALLOCATION_FILE=([^\s]+)", environment)
    readiness_env = re.search(r"(?:^|\s)FETCHER_ACCOUNT_READINESS_FILE=([^\s]+)", environment)
    installed_allocation = (
        Path(allocation_env.group(1))
        if allocation_env
        else Path(
            readiness_env.group(1)
            if readiness_env
            else f"/var/lib/findb-account/readiness/{allocation['provider']}.json"
        ).with_suffix(".account.json")
    )
    if installed_allocation.resolve() != args.allocation.resolve():
        raise ValueError("running allocation path differs from supplied allocation")
    if key is None:
        raise ValueError("installed Source identity unavailable")
    artifact = Path(full_market_cli.__file__).parent
    started = subprocess.check_output(
        ["ps", "-p", str(args.pid), "-o", "lstart="], text=True
    ).strip()
    start_time = datetime.strptime(started, "%a %b %d %H:%M:%S %Y").astimezone().timestamp()
    if any(
        path.stat().st_mtime > start_time + 1 for path in [args.config, *artifact.rglob("*.py")]
    ):
        raise ValueError(
            "installed source/config changed after process start; restart before inspection"
        )
    artifact_digest = installed_artifact_digest(artifact)
    from findb_fetcher.full_market_universe import canonical_bytes, checksum

    if allocation["environment"] != "local" or allocation["account"]["governor_identity"] != str(
        args.governor
    ):
        raise ValueError("installed allocation/governor identity mismatch")
    installed_consumers = {"full_market", "maintenance"}
    for supplied in args.consumer_pid:
        profile, pid = supplied.split("=", 1)
        if profile not in {"pilot", "historical"}:
            raise ValueError("unknown local consumer profile")
        process = subprocess.check_output(["ps", "-p", str(int(pid)), "-o", "command="], text=True)
        if "findb" not in process or (profile == "historical" and "historical" not in process):
            raise ValueError("consumer PID identity invalid")
        installed_env = subprocess.check_output(
            ["ps", "eww", "-p", str(int(pid)), "-o", "command="], text=True
        )
        extra_environment, _ = process_environment(
            installed_env, allow_legacy=args.allow_legacy_environment
        )
        if extra_environment != "local":
            raise ValueError("installed consumer environment differs from local enrollment")
        if f"FETCHER_ACCOUNT_STATE_PATH={args.governor}" not in installed_env or (
            f"FETCHER_ACCOUNT_ALLOCATION_FILE={args.allocation}" not in installed_env
            and f"FETCHER_ACCOUNT_READINESS_FILE={args.allocation.with_suffix('.json').with_suffix('.json')}"
            not in installed_env
        ):
            raise ValueError("installed consumer does not share the inspected governor/allocation")
        installed_consumers.add(profile)
    if set(allocation["account"]["consumers"]) != installed_consumers:
        raise ValueError("allocation must cover exactly inspected installed consumers")
    allocation["requests_per_second"] = float(allocation["requests_per_second"])
    allocation.setdefault("source_requests_per_minute", 120)
    receipt = {
        "kind": "local-installed",
        "source_key_sha256": hashlib.sha256(key.group(1).encode()).hexdigest(),
        "environment": "local",
        "environment_contract": environment_contract,
        "runtime_id": "local:"
        + hashlib.sha256(
            artifact_digest.encode() + args.config.read_bytes() + str(args.governor).encode()
        ).hexdigest(),
        "inspected_pid": args.pid,
        "artifact_sha256": artifact_digest,
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "governor_identity": str(args.governor),
        "account_allocation_sha256": checksum(canonical_bytes(allocation)),
        "consumers": allocation["account"]["consumers"],
    }
    args.output.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    args.output.chmod(0o600)


if __name__ == "__main__":
    main()
