"""Read-only Fetcher host eligibility, including gracefully drained releases.

The workflow supplies this guard from its own trusted SHA and an independently
checksum-verified manifest validator. Container environment values stay in memory.
"""

import argparse
import importlib.util
import json
import re
import stat
import subprocess
from pathlib import Path

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
    with validator.SourceBundleReader(root) as source:
        manifest = validator._load_manifest_bytes(source.read("release-manifest.json"))
        if type(manifest.get("fetcher_bundle_version")) is not int or manifest[
            "fetcher_bundle_version"
        ] not in {3, 4, 5}:
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


def runtime_command(target, provider, profile, bundle_version=4):
    if profile == "historical":
        return [
            "findb-fetch-historical-backfill",
            "--provider",
            provider,
            "--run-forever",
        ]
    state_dir = f"/var/lib/findb-{profile.replace('_', '-')}/{target}"
    if profile == "full_market" and target == "production":
        state_dir = "/var/lib/findb-full-market"
    return [
        "findb-fetch-runtime",
        "--execution-profile",
        profile,
        "--config",
        f"/app/configs/{'bootstrap' if bundle_version >= 4 else 'full_market'}.{target}.v1.json",
        "--state-path",
        f"{state_dir}/state.sqlite3",
        "--provider",
        provider,
        "--run-forever",
    ]


def committed_runtime(row, base=Path("/opt/fetcher")):
    labels = row["Config"].get("Labels") or {}
    transaction = labels.get("com.findb.deployment.transaction")
    if transaction is None:
        return labels.get("com.findb.fetcher.accepted") == "true"
    if not re.fullmatch(r"[0-9a-f]{64}-[0-9]+-[0-9]+", transaction):
        return False
    directory = base / "transactions" / transaction
    try:
        for path in (directory, directory / "journal.json", directory / "receipt.json"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
                return False
        journal = json.loads((directory / "journal.json").read_text())
        receipt = json.loads((directory / "receipt.json").read_text())
        return (
            journal["phase"] == "committed"
            and receipt["transaction_id"] == transaction
            and receipt["schema_version"] == 4
            and receipt["phase"] == "healthy"
            and receipt["state"] == "accepted"
            and any(
                item["id"] == row["Id"] and item["image"] == row["Config"]["Image"]
                for item in journal["healthy"]
            )
        )
    except (OSError, ValueError, KeyError):
        return False


def require_runtime(row, image, target, provider, profile, bundle_version=4):
    command = runtime_command(target, provider, profile, bundle_version)[0]
    config, state = row["Config"], row["State"]
    fields = [item.split("=", 1) for item in config["Env"] if "=" in item]
    identity = [value for key, value in fields if key == "APP_ENVIRONMENT"]
    legacy = [value for key, value in fields if key == "DEPLOYMENT_TARGET"]
    if identity != [target] or any(value != target for value in legacy):
        raise ValueError("installed runtime environment mismatch")
    args = config["Cmd"]
    if (
        not committed_runtime(row)
        or config["Image"] != image
        or not isinstance(args, list)
        or args != runtime_command(target, provider, profile, bundle_version)
        or "Entrypoint" not in config
        or config["Entrypoint"] not in (None, [])
        or row.get("Path") != command
        or not isinstance(row.get("Args"), list)
        or row["Args"] != args[1:]
        or type(state.get("Running")) is not bool
        or state.get("Status") != ("running" if state.get("Running") else "exited")
        or state.get("ExitCode") != 0
        or state.get("OOMKilled") is not False
        or state.get("Error") != ""
    ):
        raise ValueError("installed runtime is not an accepted gracefully drained consumer")


PILOT_RESIDUE = tuple(f"findb-pilot-{provider.replace('_', '-')}" for provider in PROVIDERS)


def require_pilot_residue(rows, bundle_version):
    """Allow only stopped, gracefully drained Pilot consumers of a pre-v5 install.

    Bundle v5 archives and removes them before installing; it never stops,
    force-removes or reuses a Pilot runtime. A running one blocks the release.
    """
    for row in rows:
        state = row["State"]
        if bundle_version >= 5:
            raise ValueError("retired pilot consumer remains after its retirement")
        if state.get("Running") is not False or state.get("Status") != "exited":
            print("fetcher_host_eligibility=failed reason=pilot_residue_running")
            raise ValueError("retired pilot consumer must be stopped before its retirement")
        if (
            state.get("ExitCode") != 0
            or state.get("OOMKilled") is not False
            or state.get("Error") != ""
        ):
            print("fetcher_host_eligibility=failed reason=pilot_residue_not_drained")
            raise ValueError("retired pilot consumer did not drain gracefully")


def check_host(target, registry, validator, base=Path("/opt/fetcher")):
    inventory = subprocess.check_output(
        ["docker", "container", "ls", "--all", "--format", "{{.ID}}"], text=True
    ).splitlines()
    if not inventory:
        if (base / "current").exists() or (base / "current").is_symlink():
            raise ValueError("first bootstrap requires an empty host with no current release")
        return "bootstrap"
    rows = json.loads(subprocess.check_output(["docker", "inspect", *inventory], text=True))
    if len(rows) != len(inventory):
        raise ValueError("container inventory incomplete")
    manifest = installed_manifest(base, validator, target, registry)
    names = {row["Name"].removeprefix("/"): row for row in rows}
    if any("shioaji" in name for name in names):
        raise ValueError("retired provider container remains on host")
    residue = [names.pop(name) for name in PILOT_RESIDUE if name in names]
    require_pilot_residue(residue, manifest["fetcher_bundle_version"])
    if set(names) != set(EXPECTED_RUNTIMES) or len(names) + len(residue) != len(rows):
        print("fetcher_host_eligibility=failed reason=consumer_inventory_mismatch")
        raise ValueError("missing, duplicate or unknown consumer")
    for name, provider, profile, image_key in CONSUMERS:
        image = manifest["images"][image_key]
        try:
            require_runtime(
                names[name], image, target, provider, profile, manifest["fetcher_bundle_version"]
            )
        except (ValueError, KeyError):
            print(
                "fetcher_host_blocked reason=runtime_not_eligible "
                f"execution_profile={profile} provider={provider}"
            )
            raise
    return "running" if any(row["State"]["Running"] for row in rows) else "drained"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("staging", "production"), required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--installed-validator", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "verified_release_validator", args.installed_validator
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    try:
        mode = check_host(args.target, args.registry, validator)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        raise SystemExit("fetcher_host_eligibility=failed") from None
    print("fetcher_host_eligibility=" + mode)


if __name__ == "__main__":
    main()
