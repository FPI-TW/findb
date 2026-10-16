#!/usr/bin/env python3
"""Wrap a trusted script so AWS-RunShellScript executes it with Bash."""

from __future__ import annotations

import argparse
import shlex
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=int)
    args = parser.parse_args()
    if args.timeout_seconds is not None and not 1 <= args.timeout_seconds <= 1800:
        return 2
    script = sys.stdin.read()
    if not script or "\0" in script:
        return 2
    prefix = "exec "
    if args.timeout_seconds is not None:
        # GNU timeout signals the entire command group. Allow the deployment's
        # TERM traps to restore its journal before a bounded forced termination.
        prefix += f"timeout --signal=TERM --kill-after=180s {args.timeout_seconds}s "
    sys.stdout.write(f"{prefix}bash -c {shlex.quote(script)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
