"""CLI for the staging-only Shioaji coordinator."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Never

MAX_OUTPUT = 4096


class _Parser(argparse.ArgumentParser):
    """Argument failures are returned as the same bounded JSON envelope."""

    def error(self, _message: str) -> Never:
        raise ValueError("invalid arguments")


def main(argv: list[str] | None = None) -> int:
    """Retired acquiring entrypoint; retained state must use the queue runtime."""
    from findb_fetcher.legacy_cli import retired

    return retired("findb-fetch-shioaji-staging")


def _emit(value: dict[str, object], status: int = 0) -> int:
    # Never slice JSON: a bounded result must remain a valid JSON document.
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    if len(raw) > MAX_OUTPUT:
        raw = json.dumps({"code": "SAFE_FAILURE", "stage": "output", "count": 0})
    sys.stdout.write(raw + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
