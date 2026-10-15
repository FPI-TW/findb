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
        if (
            type(manifest.get("fetcher_bundle_version")) is not int
            or manifest["fetcher_bundle_version"] != 1
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


def runtime_command(target, provider, profile):
    if profile == "historical":
        return ["findb-fetch-historical-backfill", "--provider", provider, "--run-forever"]
    state_dir = f"/var/lib/findb-{profile.replace('_', '-')}/{target}"
    if profile == "full_market" and target == "production":
        state_dir = "/var/lib/findb-full-market"
    return [
        "findb-fetch-runtime",
        "--execution-profile",
        profile,
        "--config",
        f"/app/configs/full_market.{target}.v1.json",
        "--state-path",
        f"{state_dir}/state.sqlite3",
        "--provider",
        provider,
        "--run-forever",
    ]


def require_runtime(row, image, target, provider, profile):
    command = runtime_command(target, provider, profile)[0]
    config, state = row["Config"], row["State"]
    fields = [item.split("=", 1) for item in config["Env"] if "=" in item]
    identity = [value for key, value in fields if key == "APP_ENVIRONMENT"]
    legacy = [value for key, value in fields if key == "DEPLOYMENT_TARGET"]
    if identity != [target] or any(value != target for value in legacy):
        raise ValueError("installed runtime environment mismatch")
    args = config["Cmd"]
    if (
        config.get("Labels", {}).get("com.findb.fetcher.accepted") != "true"
        or config["Image"] != image
        or not isinstance(args, list)
        or args != runtime_command(target, provider, profile)
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
    for profile in ("pilot", "full_market", "historical"):
        for provider in ("twelve_data", "finlab", "shioaji", "taifex"):
            if profile == "historical" and provider == "taifex":
                continue
            name = f"findb-{profile.replace('_', '-')}-{provider.replace('_', '-')}"
            image = manifest["images"]["twelve_data" if provider == "taifex" else provider]
            require_runtime(names[name], image, target, provider, profile)
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
