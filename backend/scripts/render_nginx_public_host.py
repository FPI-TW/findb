"""Render the public hostname into the Nginx configuration template."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

PUBLIC_HOST_PLACEHOLDER = "__FINDB_PUBLIC_HOST__"
HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)


def render_public_host(template: str, public_host: str) -> str:
    host = public_host.strip().lower().rstrip(".")
    if not HOSTNAME_PATTERN.fullmatch(host):
        raise ValueError("FINDB_PUBLIC_HOST must be a valid DNS hostname")
    if template.count(PUBLIC_HOST_PLACEHOLDER) != 2:
        raise ValueError("nginx template must contain exactly two public-host placeholders")
    return template.replace(PUBLIC_HOST_PLACEHOLDER, host)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="Public DNS hostname for this target.")
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.write_text(
        render_public_host(args.template.read_text(encoding="utf-8"), args.host),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
