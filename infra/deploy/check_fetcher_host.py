"""Read-only Fetcher host eligibility, including gracefully drained releases.

The workflow supplies this guard from its own trusted SHA and an independently
checksum-verified manifest validator. Container environment values stay in memory.
The same rules apply to staging and production: an empty host may bootstrap, a
running host is upgraded transactionally, and a drained host must prove its
installed accepted release before anything executes.
"""

import argparse
import importlib.util
import json
import re
import stat
import subprocess
from pathlib import Path

STABLES = {
    "twelve_data": ("findb-fetcher-scheduler", "findb-fetch-scheduler"),
    "finlab": ("findb-fetcher-finlab-scheduler", "findb-fetch-finlab-scheduler"),
    "shioaji": ("findb-fetcher-shioaji-scheduler", "findb-fetch-shioaji-scheduler"),
}
FULL_MARKET_PROVIDERS = ("twelve_data", "finlab", "shioaji", "taifex")
FULL_MARKET_STATE_DIR_RE = re.compile(r"/var/lib/findb-full-market(/[a-z0-9-]+)?")


def require_root_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise ValueError("release directory identity invalid")
    if info.st_mode & 0o022:
        raise ValueError("release directory is writable by others")


def installed_manifest(base, validator, target, registry):
    for path in (base, base / "releases"):
        require_root_directory(path)
    pointer = base / "current"
    if not pointer.is_symlink():
        raise ValueError("current pointer unavailable")
    root = Path(pointer.readlink())
    if (
        not root.is_absolute()
        or root.parent != base / "releases"
        or not re.fullmatch(r"[0-9a-f]{64}-[A-Za-z0-9._-]+", root.name)
    ):
        raise ValueError("current pointer invalid")
    require_root_directory(root)
    if stat.S_IMODE(root.lstat().st_mode) != 0o700:
        raise ValueError("release root mode invalid")
    # Never execute the installed release's validator. The workflow-provenance
    # validator re-hashes the installed layout recorded by its own manifest, so
    # every accepted bundle layout stays provable without running its code.
    with validator.SourceBundleReader(root) as source:
        manifest = validator._load_manifest_bytes(source.read("release-manifest.json"))
        if (
            type(manifest.get("fetcher_bundle_version")) is not int
            or manifest["fetcher_bundle_version"] not in validator.FETCHER_BUNDLE_LAYOUTS
        ):
            raise ValueError("installed fetcher bundle format identity invalid")
        contracts = source.load_selected_contract_manifest(root / "contracts/manifest.json")
        validator.validate_manifest(manifest, contracts, source)
    if (
        manifest["unit"] != "fetcher"
        or manifest["deployment_target"] != target
        or manifest.get("registry") != registry
    ):
        raise ValueError("installed release target/unit/registry mismatch")
    return manifest


def production_command(command, provider=None, *, target, state_dir):
    # Trusted helper argv contracts for every accepted release still in service.
    if command == "findb-fetch-historical-backfill":
        args = ["--provider", provider]
    elif command == "findb-fetch-full-market":
        args = [
            "--config",
            f"/app/configs/full_market.{target}.v1.json",
            "--state-path",
            f"{state_dir}/state.sqlite3",
            "--provider",
            provider,
            "--readiness-file",
            f"/var/lib/findb-account/readiness/{provider}.json",
        ]
    elif command == "findb-fetch-shioaji-scheduler":
        manifest = (
            "/app/configs/shioaji_tw50_2026_09_21.v2.json"
            if target == "production"
            else "/app/configs/shioaji_tw_staging_pilot.v3.json"
        )
        args = ["--manifest", manifest]
    else:
        slot, dataset = {
            "findb-fetch-scheduler": ("western_markets_window", "us_equity_eod"),
            "findb-fetch-finlab-scheduler": ("taiwan_market_window", "tw_equity_eod"),
        }[command]
        args = [
            "--schedule-file",
            f"/app/configs/daily_scheduler.{target}.v3.json",
            "--slot-id",
            slot,
            "--dataset-key",
            dataset,
        ]
    return [command, *args, "--run-forever"]


def require_runtime(row, image, target, command, provider=None, *, state_dir):
    config, state = row["Config"], row["State"]
    fields = [item.split("=", 1) for item in config["Env"] if "=" in item]
    identity = [value for key, value in fields if key == "APP_ENVIRONMENT"]
    if identity != [target]:
        raise ValueError("installed runtime environment mismatch")
    args = config["Cmd"]
    if (
        config.get("Labels", {}).get("com.findb.fetcher.accepted") != "true"
        or config["Image"] != image
        or not isinstance(args, list)
        or args != production_command(command, provider, target=target, state_dir=state_dir)
        or "Entrypoint" not in config
        or config["Entrypoint"] not in (None, [])
        or row.get("Path") != command
        or not isinstance(row.get("Args"), list)
        or row["Args"] != args[1:]
        or state.get("Status") != "exited"
        or state.get("Running") is not False
        or state.get("ExitCode") != 0
        or state.get("OOMKilled") is not False
        or state.get("Error") != ""
    ):
        raise ValueError("installed runtime is not an accepted gracefully drained consumer")


def check_host(target, registry, validator, state_dir, base=Path("/opt/fetcher")):
    if FULL_MARKET_STATE_DIR_RE.fullmatch(state_dir) is None:
        raise ValueError("full-market state directory invalid")
    inventory = subprocess.check_output(
        ["docker", "container", "ls", "--all", "--format", "{{.ID}}"], text=True
    ).splitlines()
    if not inventory:
        # A first bootstrap is proven by an empty inventory and no installed
        # release pointer, in any target.
        if (base / "current").exists() or (base / "current").is_symlink():
            raise ValueError("first bootstrap requires an empty host")
        return "bootstrap"
    rows = json.loads(subprocess.check_output(["docker", "inspect", *inventory], text=True))
    if len(rows) != len(inventory):
        raise ValueError("container inventory incomplete")
    if any(row["State"]["Running"] and re.search("fetcher|rabbitmq", row["Name"]) for row in rows):
        return "running"
    if any(row["State"]["Running"] for row in rows):
        raise ValueError("host is not a drained installation")
    manifest = installed_manifest(base, validator, target, registry)
    names = {row["Name"].removeprefix("/"): row for row in rows}
    for provider, (name, command) in STABLES.items():
        image = manifest["images"][provider]
        require_runtime(names[name], image, target, command, state_dir=state_dir)
        if name + "-historical" in names:
            require_runtime(
                names[name + "-historical"],
                image,
                target,
                "findb-fetch-historical-backfill",
                provider,
                state_dir=state_dir,
            )
    for provider in FULL_MARKET_PROVIDERS:
        name = "findb-full-market-" + provider.replace("_", "-")
        image = manifest["images"]["twelve_data" if provider == "taifex" else provider]
        require_runtime(
            names[name], image, target, "findb-fetch-full-market", provider, state_dir=state_dir
        )
    return "drained"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("staging", "production"), required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--installed-validator", type=Path, required=True)
    parser.add_argument("--full-market-state-dir", required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "verified_release_validator", args.installed_validator
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    try:
        mode = check_host(args.target, args.registry, validator, args.full_market_state_dir)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit("fetcher_host_eligibility=failed") from None
    print("fetcher_host_eligibility=" + mode)


if __name__ == "__main__":
    main()
