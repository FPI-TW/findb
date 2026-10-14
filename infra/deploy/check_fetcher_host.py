"""Read-only Fetcher host eligibility, including gracefully drained releases.

The workflow supplies this guard from its own trusted SHA and an independently
checksum-verified manifest validator. Container environment values stay in memory.
"""

import argparse
import hashlib
import importlib.util
import json
import re
import stat
import subprocess
from pathlib import Path

FULL_SOURCE_FILES = frozenset(
    (
        "infra/deploy/inspect_full_market_installation.py",
        "fetcher/configs/full_market.local.v1.json",
        "fetcher/configs/full_market.staging.v1.json",
        "fetcher/configs/full_market.production.v1.json",
    )
)

STABLES = {
    "twelve_data": ("findb-fetcher-scheduler", "findb-fetch-scheduler"),
    "finlab": ("findb-fetcher-finlab-scheduler", "findb-fetch-finlab-scheduler"),
    "shioaji": ("findb-fetcher-shioaji-scheduler", "findb-fetch-shioaji-scheduler"),
}


def require_root_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0:
        raise ValueError("release directory identity invalid")
    if info.st_mode & 0o022:
        raise ValueError("release directory is writable by others")


def installed_source_reader(validator):
    # Never execute the installed release's validator. The workflow-provenance
    # validator supplies safe reads and strict schema/contract checks; only the
    # two known source allowlists differ across these immutable releases.
    class InstalledSourceReader(validator.SourceBundleReader):
        def deployment_source_bundle_sha256(self, unit, *, pilot_format=True):
            present = []
            for relative in FULL_SOURCE_FILES:
                try:
                    (self.repo_root / relative).lstat()
                except FileNotFoundError:
                    continue
                present.append(relative)
            if present and len(present) != len(FULL_SOURCE_FILES):
                raise ValueError("installed source layout is incomplete")
            paths = validator.bundle_paths(unit, pilot_format=pilot_format)
            if not present:
                paths = tuple(path for path in paths if path not in FULL_SOURCE_FILES)
            records = [
                {"path": path, "sha256": hashlib.sha256(self.read(path)).hexdigest()}
                for path in sorted(paths)
            ]
            canonical = json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
            return hashlib.sha256(canonical).hexdigest()

    return InstalledSourceReader


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
    with installed_source_reader(validator)(root) as source:
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


def production_command(command, provider=None):
    # Trusted helper argv contracts, also used by pre-Full v0.1.5.
    if command == "findb-fetch-historical-backfill":
        args = ["--provider", provider]
    elif command == "findb-fetch-full-market":
        args = [
            "--config",
            "/app/configs/full_market.production.v1.json",
            "--state-path",
            "/var/lib/findb-full-market/state.sqlite3",
            "--provider",
            provider,
            "--readiness-file",
            f"/var/lib/findb-account/readiness/{provider}.json",
        ]
    elif command == "findb-fetch-shioaji-scheduler":
        args = ["--manifest", "/app/configs/shioaji_tw50_2026_09_21.v2.json"]
    else:
        slot, dataset = {
            "findb-fetch-scheduler": ("western_markets_window", "us_equity_eod"),
            "findb-fetch-finlab-scheduler": ("taiwan_market_window", "tw_equity_eod"),
        }[command]
        args = [
            "--schedule-file",
            "/app/configs/daily_scheduler.production.v3.json",
            "--slot-id",
            slot,
            "--dataset-key",
            dataset,
        ]
    return [command, *args, "--run-forever"]


def require_runtime(row, image, target, command, provider=None):
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
        or args != production_command(command, provider)
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


def check_host(target, registry, validator, base=Path("/opt/fetcher")):
    inventory = subprocess.check_output(
        ["docker", "container", "ls", "--all", "--format", "{{.ID}}"], text=True
    ).splitlines()
    if not inventory:
        if target != "production" or (base / "current").exists() or (base / "current").is_symlink():
            raise ValueError("first bootstrap requires an empty production host")
        return "bootstrap"
    rows = json.loads(subprocess.check_output(["docker", "inspect", *inventory], text=True))
    if len(rows) != len(inventory):
        raise ValueError("container inventory incomplete")
    if any(row["State"]["Running"] and re.search("fetcher|rabbitmq", row["Name"]) for row in rows):
        return "running"
    if target != "production" or any(row["State"]["Running"] for row in rows):
        raise ValueError("host is not a drained production installation")
    manifest = installed_manifest(base, validator, target, registry)
    names = {row["Name"].removeprefix("/"): row for row in rows}
    for provider, (name, command) in STABLES.items():
        image = manifest["images"][provider]
        require_runtime(names[name], image, target, command)
        if name + "-historical" in names:
            require_runtime(
                names[name + "-historical"],
                image,
                target,
                "findb-fetch-historical-backfill",
                provider,
            )
    if manifest.get("runtime_profile", "bounded") == "full-market":
        for provider in (*STABLES, "taifex"):
            name = "findb-full-market-" + provider.replace("_", "-")
            image = manifest["images"]["twelve_data" if provider == "taifex" else provider]
            require_runtime(names[name], image, target, "findb-fetch-full-market", provider)
    return "drained"


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
