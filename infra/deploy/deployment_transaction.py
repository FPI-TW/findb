#!/usr/bin/env python3
"""Root-owned single-pass host transaction with an independent boot recovery timer.

Only nonsecret identities are journaled. The GitHub deployment role writes the
attempt-specific S3 decision; the instance role only reads it. Docker environment
values are never serialized, logged, or used as recovery instructions.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from deployment_receipt import validate as validate_receipt

PROVIDERS = ("twelve-data", "finlab", "taifex", "factset")
FETCHER_NAMES = tuple(
    f"findb-{profile}-{provider}"
    for provider in PROVIDERS
    for profile in ("full-market", "historical")
)
FINDB_NAMES = tuple(
    f"findb-{service}"
    for service in ("nginx", "dashboard", "serve", "ingest", "dispatcher", "worker", "raw-cleanup")
)
ENV_NAMES = (
    "AWS_REGION",
    "AWS_ACCOUNT_ID",
    "APP_ENVIRONMENT",
    "ECR_REGISTRY",
    "PROVIDER_WORK_REQUIRED",
    "FINDB_PUBLIC_HOST",
    "PREDEPLOY_EXPECTED_RDS_ENDPOINT",
    "PREDEPLOY_EXPECTED_ALEMBIC_REVISION",
    "FINDB_IMAGE_REF",
    "DASHBOARD_IMAGE_REF",
    "TWELVE_IMAGE_REF",
    "FINLAB_IMAGE_REF",
    "FACTSET_IMAGE_REF",
)
TERMINAL = {"committed", "aborted", "recovery_required"}
LABEL = "com.findb.deployment.transaction"


def run(*args: str, timeout: int = 120, env: dict | None = None) -> str:
    # Tool output can contain runtime secrets. Do not include it in exceptions.
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        raise RuntimeError(f"command_timed_out:{Path(args[0]).name}") from None
    if process.returncode:
        raise RuntimeError(f"command_failed:{Path(args[0]).name}")
    return stdout


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, sort_keys=True).encode(), 0o600)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def process_identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def inventory() -> list[dict]:
    ids = run("docker", "container", "ls", "--all", "--quiet").splitlines()
    return json.loads(run("docker", "inspect", *ids)) if ids else []


def summary(row: dict) -> dict:
    return {
        "id": row["Id"],
        "name": row["Name"].removeprefix("/"),
        "image": row["Config"]["Image"],
        "running": row["State"]["Running"],
        "restart": row["HostConfig"]["RestartPolicy"]["Name"],
        "started_at": row["State"].get("StartedAt"),
    }


def owned(row: dict, tx: str) -> bool:
    return (row["Config"].get("Labels") or {}).get(LABEL) == tx


def healthy(rows: list[dict], names: tuple[str, ...], tx: str) -> list[dict]:
    current = {row["Name"].removeprefix("/"): row for row in rows if owned(row, tx)}
    if set(current) != set(names):
        raise ValueError("transaction runtime inventory mismatch")
    result = []
    for name in names:
        row = current[name]
        state = row["State"]
        if (
            state["Running"] is not True
            or state["Status"] != "running"
            or state.get("OOMKilled") is not False
            or state.get("Error") != ""
            or row["RestartCount"] != 0
        ):
            raise ValueError("transaction runtime unhealthy")
        if "Health" in state and state["Health"]["Status"] != "healthy":
            raise ValueError("transaction runtime healthcheck not ready")
        result.append(summary(row))
    return result


def observe(names: tuple[str, ...], tx: str, seconds: float) -> None:
    baseline = healthy(inventory(), names, tx)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(5, deadline - time.monotonic()))
        if healthy(inventory(), names, tx) != baseline:
            raise ValueError("runtime identity changed during observation")


class Transaction:
    def __init__(self, root: Path):
        self.root = root
        match = re.fullmatch(
            r"/opt/(findb|fetcher)/releases/([0-9a-f]{64}-[0-9]+-[0-9]+)", str(root)
        )
        if not match:
            raise ValueError("release root invalid")
        for directory in (root, root.parent, root.parent.parent):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("release directory unsafe")
        self.unit, self.tx = match.groups()
        self.base = root.parent.parent
        self.directory = self.base / "transactions" / self.tx
        self.path = self.directory / "journal.json"
        self.manifest = json.loads((root / "release-manifest.json").read_text())
        if (
            self.manifest.get("schema_version") != 4
            or self.manifest.get("deployment_protocol") != "single-pass-v1"
            or self.manifest.get("unit") != self.unit
        ):
            raise ValueError("transaction protocol mismatch")
        self.names = FETCHER_NAMES if self.unit == "fetcher" else FINDB_NAMES
        self.timer = f"findb-deployment-recovery-{self.unit}"

    @contextlib.contextmanager
    def lock(self, blocking: bool = True):
        for directory in (self.base, self.base / "transactions"):
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("transaction directory unsafe")
        fd = os.open(
            self.base / "transactions" / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
        finally:
            os.close(fd)

    def load(self) -> dict:
        info = self.path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError("transaction journal unsafe")
        journal = json.loads(self.path.read_text())
        if journal["transaction_id"] != self.tx or journal["unit"] != self.unit:
            raise ValueError("transaction journal identity mismatch")
        return journal

    def save(self, journal: dict) -> None:
        atomic_json(self.path, journal)

    def install_watchdog(self) -> None:
        # Persistent units survive host reboot. Container restart=no prevents
        # provisional services coming back before this recovery runs.
        executable = self.root / "infra/deploy/deployment_transaction.py"
        units = Path("/etc/systemd/system")
        service = f"[Unit]\nAfter=docker.service network-online.target\nWants=docker.service network-online.target\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 {executable} recover --root {self.root}\nTimeoutStartSec=1200\n"
        timer = f"[Unit]\nDescription=Recover pending {self.unit} deployment\n[Timer]\nOnBootSec=15s\nOnUnitActiveSec=15s\nAccuracySec=1s\nUnit={self.timer}.service\n[Install]\nWantedBy=timers.target\n"
        for suffix, content in (("service", service), ("timer", timer)):
            path = units / f"{self.timer}.{suffix}"
            if path.is_symlink():
                raise ValueError("watchdog unit symlink")
            atomic_write(path, content.encode(), 0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", "--now", self.timer + ".timer")
        run("systemctl", "is-active", "--quiet", self.timer + ".timer")

    def apply(self, bucket: str, mode: str) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("bundle bucket invalid")
        with self.lock():
            for path in (self.base / "transactions").glob("*/journal.json"):
                previous = json.loads(path.read_text())
                if previous["phase"] not in {"committed", "aborted"}:
                    raise ValueError("unresolved deployment transaction")
            self.directory.mkdir(mode=0o700)
            sync_directory(self.directory.parent)
            sync_directory(self.base)
            pointer = self.base / "current"
            previous_root = str(pointer.readlink()) if pointer.is_symlink() else None
            if previous_root and not re.fullmatch(
                rf"/opt/{self.unit}/releases/[0-9a-f]{{64}}-[A-Za-z0-9._-]+", previous_root
            ):
                raise ValueError("previous pointer invalid")
            rows = inventory()
            # Entry guards run before apply. Record only original stable names;
            # retained candidates/backups are an unresolved transaction.
            for row in rows:
                name = row["Name"].removeprefix("/")
                if any(name.startswith(n + "-") for n in self.names):
                    raise ValueError("unresolved retained container")
            journal = {
                "schema_version": 1,
                "transaction_id": self.tx,
                "unit": self.unit,
                "phase": "applying",
                "boot_id": boot_id(),
                "deadline": time.time() + 1080,
                "previous_root": previous_root,
                "originals": [
                    summary(r) for r in rows if r["Name"].removeprefix("/") in self.names
                ],
                "environment": {n: os.environ[n] for n in ENV_NAMES if n in os.environ},
                "bucket": bucket,
                "mode": mode,
                "healthy": [],
            }
            self.save(journal)
            self.install_watchdog()
            environment = {
                **os.environ,
                "DEPLOYMENT_TRANSACTION_ID": self.tx,
                f"{self.unit.upper()}_RELEASE_ROOT": str(self.root),
                f"{self.unit.upper()}_DEPLOY_MODE": "apply",
                "DEPLOYMENT_REPLAY": str(mode == "replay").lower(),
            }
            if self.unit == "findb":
                override = {
                    "services": {
                        name.removeprefix("findb-"): {"restart": "no", "labels": {LABEL: self.tx}}
                        for name in self.names
                    }
                }
                atomic_json(self.directory / "compose.json", override)
                environment["FINDB_TRANSACTION_COMPOSE_FILE"] = str(self.directory / "compose.json")
            command = self.root / f"infra/deploy/runtime-secrets/deploy_{self.unit}_aws.sh"
            try:
                self.execute_host(command, environment, journal)
                deadline = time.monotonic() + 60
                while True:
                    try:
                        journal["healthy"] = healthy(inventory(), self.names, self.tx)
                        break
                    except ValueError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(2)
                journal["phase"] = "healthy"
                journal["deadline"] = time.time() + 120
                self.save(journal)
            except BaseException:
                self.abort_locked(journal)
                raise
        print("deployment_transaction=healthy", flush=True)

    def execute_host(self, command: Path, environment: dict, journal: dict) -> None:
        # Apply and long-running FinDB recovery both need a durable process
        # identity. A lost SSM parent must not leave an untracked schema writer.
        child = subprocess.Popen(
            [
                "bash",
                "-c",
                'read -r start; [ "$start" = apply ] || exit 1; exec bash "$1"',
                "transaction",
                str(command),
            ],
            stdin=subprocess.PIPE,
            start_new_session=True,
            env=environment,
        )
        try:
            journal.update(
                child_pid=child.pid,
                child_start=process_identity(child.pid),
                boot_id=boot_id(),
                deadline=time.time() + 1080,
            )
            self.save(journal)
            assert child.stdin is not None
            child.stdin.write(b"apply\n")
            child.stdin.close()
            if child.wait(timeout=900):
                raise RuntimeError("host command failed")
        except BaseException:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=180)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            raise

    def decision(self, journal: dict) -> dict:
        target = self.manifest["deployment_target"]
        prefix = self.unit + ("/production" if target == "production" else "")
        key = f"{prefix}/transactions/{self.tx}.json"
        # AWS error/absence is never a success or permission to commit.
        raw = run(
            "aws",
            "s3",
            "cp",
            f"s3://{journal['bucket']}/{key}",
            "-",
            "--only-show-errors",
            timeout=20,
        )
        receipt = json.loads(raw)
        validate_receipt(
            receipt, self.manifest, bundle_sha=self.tx.split("-", 1)[0], transaction_id=self.tx
        )
        return receipt

    def finalize_locked(self, journal: dict) -> None:
        if journal["phase"] == "committed":
            self.cleanup_committed(journal)
            return
        if journal["phase"] not in ("healthy", "committing"):
            raise ValueError("transaction not healthy")
        receipt = self.decision(journal)
        current = healthy(inventory(), self.names, self.tx)
        if [(r["id"], r["image"], r["started_at"]) for r in current] != [
            (r["id"], r["image"], r["started_at"]) for r in journal["healthy"]
        ]:
            raise ValueError("healthy runtime identity changed")
        journal["phase"] = "committing"
        self.save(journal)
        atomic_json(self.directory / "receipt.json", receipt) if not (
            self.directory / "receipt.json"
        ).exists() else None
        if self.unit == "findb":
            env = journal["environment"]
            run(
                "bash",
                str(self.root / "infra/deploy/runtime-secrets/install_findb_bootstrap.sh"),
                env["AWS_REGION"],
                env["FINDB_PUBLIC_HOST"],
                str(self.root),
                env["APP_ENVIRONMENT"],
                env["AWS_ACCOUNT_ID"],
            )
        temporary = self.base / ".current-transaction"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(self.root)
        os.replace(temporary, self.base / "current")
        sync_directory(self.base)
        run("docker", "update", "--restart", "unless-stopped", *[r["id"] for r in current])
        journal["phase"] = "committed"
        self.save(journal)
        self.cleanup_committed(journal)
        print("deployment_transaction=committed", flush=True)

    def cleanup_committed(self, journal: dict) -> None:
        if journal.get("cleanup_completed"):
            return
        # Destructive cleanup follows durable commit and uses captured IDs only.
        if self.unit == "fetcher":
            present = {r["Id"]: r for r in inventory()}
            for original in journal["originals"]:
                row = present.get(original["id"])
                if row is not None:
                    if (
                        row["Config"]["Image"] != original["image"]
                        or row["State"]["Running"]
                        or row["State"]["ExitCode"] != 0
                        or row["State"]["OOMKilled"]
                    ):
                        raise ValueError("original cleanup identity unsafe")
                    run("docker", "rm", original["id"])
        journal["cleanup_completed"] = True
        self.save(journal)

    def abort_locked(self, journal: dict) -> None:
        try:
            self._abort_locked(journal)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
            journal["phase"] = "recovery_required"
            self.save(journal)
            raise

    def _abort_locked(self, journal: dict) -> None:
        if journal["phase"] in TERMINAL:
            return
        journal["phase"] = "aborting"
        self.save(journal)
        rows = inventory()
        for row in rows:
            if owned(row, self.tx):
                run("docker", "update", "--restart", "no", row["Id"])
                run("docker", "stop", "--time", "90" if self.unit == "fetcher" else "30", row["Id"])
                stopped = json.loads(run("docker", "inspect", row["Id"]))[0]
                state = stopped["State"]
                allowed_exit_codes = (0,) if self.unit == "fetcher" else (0, 143)
                if (
                    state["Running"]
                    or state["ExitCode"] not in allowed_exit_codes
                    or state["OOMKilled"]
                    or state.get("Error")
                ):
                    raise ValueError("provisional runtime shutdown not graceful")
                run("docker", "rm", row["Id"])
        if self.unit == "fetcher":
            present = {row["Id"]: row for row in inventory()}
            for original in journal["originals"]:
                row = present.get(original["id"])
                if row is None or row["Config"]["Image"] != original["image"]:
                    raise ValueError("original runtime missing")
                if (
                    row["State"]["ExitCode"] != 0
                    or row["State"]["OOMKilled"]
                    or row["State"].get("Error")
                ):
                    raise ValueError("original runtime shutdown not graceful")
                if row["Name"].removeprefix("/") != original["name"]:
                    run("docker", "rename", original["id"], original["name"])
                run("docker", "update", "--restart", original["restart"], original["id"])
                if original["running"] and not row["State"]["Running"]:
                    run("docker", "start", original["id"])
            journal["phase"] = "aborted"
        else:
            # Reuse the old release's check-only activation. It checks the exact
            # DB revision before starting any service and never downgrades schema.
            old_root = journal["previous_root"]
            journal["phase"] = "recovery_required"
            if old_root:
                previous = json.loads((Path(old_root) / "release-manifest.json").read_text())
                env = {
                    **os.environ,
                    **journal["environment"],
                    "FINDB_RELEASE_ROOT": old_root,
                    "FINDB_DEPLOY_MODE": "activate",
                    "FINDB_IMAGE_REF": previous["images"]["backend"],
                    "DASHBOARD_IMAGE_REF": previous["images"]["dashboard"],
                    "PREDEPLOY_EXPECTED_ALEMBIC_REVISION": previous["migration_revision"],
                }
                for key in (
                    "DEPLOYMENT_TRANSACTION_ID",
                    "FINDB_TRANSACTION_COMPOSE_FILE",
                    "DEPLOYMENT_REPLAY",
                ):
                    env.pop(key, None)
                try:
                    journal["phase"] = "restoring"
                    self.execute_host(
                        Path(old_root) / "infra/deploy/runtime-secrets/deploy_findb_aws.sh",
                        env,
                        journal,
                    )
                    journal["phase"] = "aborted"
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    # Fail stopped; recovery is explicitly not reported success.
                    journal["phase"] = "recovery_required"
        if journal["previous_root"]:
            temporary = self.base / ".current-restore"
            temporary.unlink(missing_ok=True)
            temporary.symlink_to(journal["previous_root"])
            os.replace(temporary, self.base / "current")
        elif (self.base / "current").is_symlink() and (
            self.base / "current"
        ).readlink() == self.root:
            (self.base / "current").unlink()
        sync_directory(self.base)
        self.save(journal)
        print("deployment_transaction=" + journal["phase"], flush=True)

    def recover(self, force: bool = False) -> None:
        journal = self.load()
        if force and journal["phase"] == "recovery_required":
            with self.lock():
                journal = self.load()
                if journal["phase"] == "recovery_required":
                    journal["phase"] = "aborting"
                    self.save(journal)
                    self.abort_locked(journal)
            return
        if journal["phase"] == "committed" and not journal.get("cleanup_completed"):
            with self.lock():
                self.cleanup_committed(self.load())
            return
        if journal["phase"] in TERMINAL:
            return
        if not force and journal["boot_id"] == boot_id() and time.time() < journal["deadline"]:
            return
        # An orphan child must not keep writing after recovery takes the lock.
        pid = journal.get("child_pid")
        if (
            pid
            and journal["boot_id"] == boot_id()
            and process_identity(pid) == journal.get("child_start")
        ):
            os.killpg(pid, signal.SIGTERM)
            end = time.monotonic() + 180
            while process_identity(pid) == journal["child_start"] and time.monotonic() < end:
                time.sleep(1)
            if process_identity(pid) == journal["child_start"]:
                os.killpg(pid, signal.SIGKILL)
        with self.lock():
            journal = self.load()
            if journal["phase"] in TERMINAL:
                return
            if journal["phase"] in ("healthy", "committing"):
                try:
                    self.finalize_locked(journal)
                    return
                except (ValueError, RuntimeError, subprocess.SubprocessError):
                    if journal["phase"] == "committed":
                        raise
            self.abort_locked(journal)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply", "finalize", "abort", "recover", "health"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--observe-seconds", type=float, default=0)
    parser.add_argument("--mode", choices=("candidate", "replay"), default="candidate")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("root required")
    tx = Transaction(args.root)
    if args.action == "apply":
        tx.apply(args.bucket or "", args.mode)
    elif args.action == "finalize":
        with tx.lock():
            tx.finalize_locked(tx.load())
    elif args.action in ("abort", "recover"):
        tx.recover(force=args.action == "abort")
    else:
        if args.observe_seconds < 0:
            raise ValueError("observation duration invalid")
        observe(tx.names, tx.tx, args.observe_seconds)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
        print("deployment_transaction=failed reason=transaction_incomplete", file=sys.stderr)
        raise SystemExit(1) from None
