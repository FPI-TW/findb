#!/usr/bin/env python3
"""Wrap a trusted script so AWS-RunShellScript executes it with Bash."""

from __future__ import annotations

import shlex
import sys


def main() -> int:
    script = sys.stdin.read()
    if not script or "\0" in script:
        return 2
    sys.stdout.write(f"exec bash -c {shlex.quote(script)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
