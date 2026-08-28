#!/usr/bin/env python3
"""Render the lookup proxy key from stdin into the host tmpfs."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

HOSTNAME_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z"
)


def _render(key: str, public_host: str) -> str:
    if key.endswith("\n"):
        key = key[:-1]
    if not key or any(character in key for character in ('"', "\\", "\n", "\r", "\x00")):
        raise ValueError("lookup_key_invalid")
    host = public_host.strip().lower().rstrip(".")
    if HOSTNAME_PATTERN.fullmatch(host) is None:
        raise ValueError("public_host_invalid")
    referer = rf"^https?://{re.escape(host)}/dashboard/lookup(?:[/?#]|$)"
    return (
        "# Generated during deployment from the runtime lookup consumer.\n"
        "# Only same-origin Dashboard lookup Referers receive the injected key.\n"
        "map $http_referer $findb_serve_proxy_key {\n"
        "    default                                                $http_x_api_key;\n"
        f'    "~{referer}"  "{key}";\n'
        "}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        rendered = _render(sys.stdin.read(), args.public_host)
        output = args.output
        run_root = Path("/run/findb-runtime-secrets")
        try:
            relative_output = output.relative_to(run_root)
        except ValueError as exc:
            raise ValueError("output_path_invalid") from exc
        if relative_output.parts != ("nginx", "serve-key.conf"):
            raise ValueError("output_path_invalid")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        root_fd: int | None = None
        directory_fd: int | None = None
        fd: int | None = None
        try:
            root_fd = os.open(run_root, directory_flags)
            directory_fd = os.open("nginx", directory_flags, dir_fd=root_fd)
            fd = os.open("serve-key.conf", file_flags, 0o600, dir_fd=directory_fd)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                fd = None
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
                metadata = os.fstat(handle.fileno())
                if stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise ValueError("output_mode_invalid")
        finally:
            if fd is not None:
                os.close(fd)
            if directory_fd is not None:
                os.close(directory_fd)
            if root_fd is not None:
                os.close(root_fd)
        return 0
    except (OSError, ValueError) as exc:
        print(f"render_serve_key=failed reason={exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
