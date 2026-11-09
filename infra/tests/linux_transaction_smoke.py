#!/usr/bin/env python3
"""Disposable real-Docker transaction smoke, plus real systemd recovery on Linux.

Run as root on an isolated CI runner. Refuse an existing /opt/fetcher installation.
With --verify-units-only, validate systemd units without claiming daemon recovery.
"""

import argparse
import hashlib
import json
import multiprocessing
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import deployment_transaction as deployment  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-units-only", action="store_true")
    args = parser.parse_args()
    base = Path("/opt/fetcher")
    if os.geteuid() != 0 or base.exists():
        raise RuntimeError("isolated root runner with no Fetcher installation required")
    image = os.environ.get("FINDB_TRANSACTION_TEST_IMAGE", "postgres:16")
    subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    token = secrets.token_hex(6)
    names = tuple(f"findb-ci-{token}-{n}" for n in range(12))
    timer = "findb-deployment-recovery-fetcher"
    unit_paths = [Path(f"/etc/systemd/system/{timer}.{suffix}") for suffix in ("service", "timer")]
    if any(path.exists() for path in unit_paths):
        raise RuntimeError("existing recovery service must not be overwritten")
    before = {}
    source = Path(__file__).resolve().parents[1] / "deploy"
    real_run = deployment.run
    try:
        for name in names:
            before[name] = real_run(
                "docker",
                "run",
                "-d",
                "--init",
                "--stop-signal",
                "SIGTERM",
                "--name",
                name,
                "--restart",
                "unless-stopped",
                image,
                "sh",
                "-c",
                'trap "exit 0" TERM; while :; do sleep 1 & wait; done',
            ).strip()
        for attempt in (1, 2):
            sha = hashlib.sha256(token.encode()).hexdigest()
            txid = f"{sha}-12-{attempt}"
            root = base / "releases" / txid
            runtime = root / "infra/deploy/runtime-secrets"
            runtime.mkdir(parents=True, mode=0o700)
            root.chmod(0o700)
            for name in ("deployment_transaction.py", "deployment_receipt.py"):
                shutil.copyfile(source / name, root / "infra/deploy" / name)
            manifest = {
                "schema_version": 4,
                "deployment_protocol": "single-pass-v1",
                "unit": "fetcher",
                "deployment_target": "staging",
                "commit_sha": "b" * 40,
                "images": {"test": image},
                "migration_revision": "none",
                "release_tag": "",
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest))
            script = runtime / "deploy_fetcher_aws.sh"
            script.write_text(
                "set -euo pipefail\nfor name in "
                + " ".join(names)
                + """; do
              docker stop --time 10 "$name" >/dev/null
              docker rename "$name" "$name-previous"
              docker run -d --init --stop-signal SIGTERM --name "$name" --restart no --label "com.findb.deployment.transaction=$DEPLOYMENT_TRANSACTION_ID" """
                + image
                + """ sh -c 'trap "exit 0" TERM; while :; do sleep 1 & wait; done' >/dev/null
done
"""
            )
            tx = deployment.Transaction(root)
            tx.names = names
            if args.verify_units_only:

                def commands(*command, **kwargs):
                    if command[0] == "systemctl":
                        return ""
                    return real_run(*command, **kwargs)

                deployment.run = commands
            tx.apply("test-bucket", "candidate")
            if args.verify_units_only:
                subprocess.run(["systemd-analyze", "verify", *map(str, unit_paths)], check=True)
            journal = tx.load()
            assert journal["phase"] == "healthy"
            if attempt == 1:
                tx.decision = lambda _: {
                    "transaction_id": txid,
                    "state": "accepted",
                    "phase": "healthy",
                    "schema_version": 4,
                }

                def commit():
                    with tx.lock():
                        tx.finalize_locked(tx.load())

                context = multiprocessing.get_context("fork")
                racers = [
                    context.Process(target=commit),
                    context.Process(target=tx.recover, args=(True,)),
                ]
                for process in racers:
                    process.start()
                for process in racers:
                    process.join(timeout=120)
                    if process.is_alive():
                        process.kill()
                        process.join()
                    assert process.exitcode == 0
                assert tx.load()["phase"] == "committed"
                before = {r["name"]: r["id"] for r in journal["healthy"]}
            else:
                # No remote decision exists. Simulate a lost runner while applying;
                # the persistent systemd service, not the test process, recovers it.
                journal.update(phase="applying", deadline=0)
                tx.save(journal)
                if args.verify_units_only:
                    tx.recover()
                else:
                    real_run("systemctl", "start", timer + ".service", timeout=300)
                assert tx.load()["phase"] == "aborted"
                actual = {
                    r["Name"].removeprefix("/"): r["Id"]
                    for r in deployment.inventory()
                    if r["Name"].removeprefix("/") in names
                }
                assert actual == before
        print(
            "single_pass_docker=passed systemd="
            + ("units_verified_only" if args.verify_units_only else "recovery_passed")
        )
    finally:
        deployment.run = real_run
        if not args.verify_units_only:
            subprocess.run(
                ["systemctl", "disable", "--now", timer + ".timer"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["systemctl", "stop", timer + ".service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for path in unit_paths:
            path.unlink(missing_ok=True)
        if not args.verify_units_only:
            subprocess.run(["systemctl", "daemon-reload"], check=True)
        for name in names:
            for suffix in ("", "-previous"):
                subprocess.run(
                    ["docker", "rm", "-f", name + suffix],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
