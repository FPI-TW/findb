"""Inspect actual installed consumers and write a fresh, secret-free receipt.

Run after activation on the Fetcher host, with accepted evidence copied from
its immutable object. Docker inspect values are processed in memory, never logged.
"""

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect(name: str) -> dict:
    return json.loads(subprocess.check_output(["docker", "inspect", name], text=True))[0]


def build(
    manifest_path: Path,
    accepted_path: Path,
    config_path: Path,
    provider: str,
    governor: str,
    allocation_path: Path,
    *,
    allow_legacy_environment: bool = False,
) -> dict:
    manifest, accepted = (
        json.loads(manifest_path.read_bytes()),
        json.loads(accepted_path.read_bytes()),
    )
    target = manifest["deployment_target"]
    if (
        accepted.get("state") != "accepted"
        or accepted.get("deployment_target") != target
        or accepted.get("unit") != "fetcher"
        or accepted.get("runtime_profile") != "full-market"
        or accepted.get("commit_sha") != manifest["commit_sha"]
        or accepted.get("images") != manifest["images"]
    ):
        raise ValueError("immutable acceptance does not bind this installation")
    names = {
        "twelve_data": "findb-fetcher-scheduler",
        "finlab": "findb-fetcher-finlab-scheduler",
        "shioaji": "findb-fetcher-shioaji-scheduler",
        "taifex": "findb-fetcher-taifex-scheduler",
    }
    full_name = "findb-full-market-" + provider.replace("_", "-")
    actual = [("full_market", inspect(full_name))]
    if provider != "taifex" or target == "staging":
        actual.append(("pilot", inspect(names[provider])))
    if target == "production" and provider != "taifex":
        actual.append(("historical", inspect(names[provider] + "-historical")))
    allocation = json.loads(allocation_path.read_bytes())
    if (
        allocation["environment"] != target
        or allocation["account"]["governor_identity"] != governor
    ):
        raise ValueError("account allocation identity invalid")
    allocation["requests_per_second"] = float(allocation["requests_per_second"])
    allocation.setdefault("source_requests_per_minute", 120)
    allocation_digest = hashlib.sha256(
        json.dumps(allocation, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    identities = []
    source_key_sha256 = None
    environment_contracts = set()
    for consumer, row in actual:
        env = dict(item.split("=", 1) for item in row["Config"]["Env"] if "=" in item)
        canonical, legacy = env.get("APP_ENVIRONMENT"), env.get("DEPLOYMENT_TARGET")
        if canonical is not None and legacy is not None and canonical != legacy:
            raise ValueError("installed canonical and legacy environment metadata conflict")
        if canonical is None:
            if not allow_legacy_environment or legacy != target:
                raise ValueError(
                    "installed APP_ENVIRONMENT unavailable; explicit legacy inspection required"
                )
            installed_environment = legacy
            environment_contracts.add("legacy-deployment-target-v1")
        else:
            installed_environment = canonical
            environment_contracts.add("app-environment-v1")
        if (
            row["Config"]["Labels"].get("com.findb.fetcher.accepted") != "true"
            or row["Config"]["Image"]
            != manifest["images"][provider if provider != "taifex" else "twelve_data"]
            or installed_environment != target
            or env.get("FULL_MARKET_ENABLED") != "true"
            or env.get("FETCHER_ACCOUNT_STATE_PATH") != governor
            or not any(
                mount.get("Destination") == str(Path(governor).parent) and mount.get("RW")
                for mount in row["Mounts"]
            )
        ):
            raise ValueError("installed consumer image/environment/governor binding invalid")
        if env.get("FETCHER_CONSUMER_PROFILE") != consumer:
            raise ValueError("installed consumer profile differs from allocation")
        installed_allocation = env.get(
            "FETCHER_ACCOUNT_ALLOCATION_FILE",
            str(
                Path(
                    env.get(
                        "FETCHER_ACCOUNT_READINESS_FILE",
                        f"/var/lib/findb-account/readiness/{provider}.json",
                    )
                ).with_suffix(".account.json")
            ),
        )
        installed_digest = subprocess.check_output(
            [
                "docker",
                "exec",
                row["Id"],
                "python",
                "-c",
                'import hashlib,json,sys; v=json.load(open(sys.argv[1])); v["requests_per_second"]=float(v["requests_per_second"]); v.setdefault("source_requests_per_minute",120); print(hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest())',
                installed_allocation,
            ],
            text=True,
        ).strip()
        if installed_digest != allocation_digest:
            raise ValueError("running consumer allocation differs from supplied configuration")
        if consumer == "full_market":
            source_key_sha256 = hashlib.sha256(env["SOURCE_CLIENT_KEY"].encode()).hexdigest()
        if not row["State"]["Running"]:
            raise ValueError("installed consumer is not running")
        identities.append(
            {"consumer": consumer, "container_id": row["Id"], "image": row["Config"]["Image"]}
        )
    command = actual[0][1]["Config"]["Cmd"]
    installed_config = command[command.index("--config") + 1]
    actual_digest = subprocess.check_output(
        [
            "docker",
            "exec",
            full_name,
            "python",
            "-c",
            'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())',
            installed_config,
        ],
        text=True,
    ).strip()
    if actual_digest != digest(config_path):
        raise ValueError("running config differs from supplied immutable config")
    if set(allocation["account"]["consumers"]) != {consumer for consumer, _ in actual} | {
        "maintenance"
    }:
        raise ValueError("account allocation must cover actual installed consumers")
    config = json.loads(config_path.read_bytes())
    if config["deployment_target"] != target or config["desired_state"] != "stopped":
        raise ValueError("installed config identity invalid")
    return {
        "kind": "deployed-installed",
        "environment": target,
        "environment_contracts": sorted(environment_contracts),
        "runtime_id": actual[0][1]["Id"],
        "source_key_sha256": source_key_sha256,
        "artifact_sha256": digest(manifest_path),
        "config_sha256": digest(config_path),
        "manifest_sha256": digest(manifest_path),
        "accepted_sha256": digest(accepted_path),
        "bundle_sha256": accepted["bundle_sha256"],
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "governor_identity": governor,
        "account_allocation_sha256": allocation_digest,
        "consumers": [consumer for consumer, _ in actual] + ["maintenance"],
        "installed_consumers": identities,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "accepted", "config", "allocation", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--provider", required=True, choices=("twelve_data", "finlab", "shioaji", "taifex")
    )
    parser.add_argument("--governor", required=True)
    parser.add_argument(
        "--allow-legacy-environment",
        action="store_true",
        help="Inspect verified accepted pre-APP_ENVIRONMENT consumers; never permits conflicting metadata",
    )
    args = parser.parse_args()
    receipt = build(
        args.manifest,
        args.accepted,
        args.config,
        args.provider,
        args.governor,
        args.allocation,
        allow_legacy_environment=args.allow_legacy_environment,
    )
    args.output.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    args.output.chmod(0o600)


if __name__ == "__main__":
    main()
