#!/usr/bin/env python3
"""Prune old unused layers only while no deployment transaction owns the host."""

import argparse
import fcntl
import json
import os
import stat
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unit", required=True, choices=("findb", "fetcher"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("root required")
    base = Path("/opt") / args.unit / "transactions"
    base.mkdir(mode=0o700, exist_ok=True)
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("transaction directory unsafe")
    fd = os.open(base / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for path in base.glob("*/journal.json"):
            journal = json.loads(path.read_text())
            if journal["phase"] not in ("committed", "aborted"):
                raise ValueError("deployment requires recovery")
        subprocess.run(
            ["docker", "image", "prune", "-af", "--filter", "until=168h"],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=240,
        )
    finally:
        os.close(fd)
    print("deployment_image_maintenance=success")


if __name__ == "__main__":
    main()
