"""Render nginx allow directives for Source API ingress."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path


def parse_cidrs(raw_value: str) -> list[str]:
    cidrs: list[str] = []
    for raw_cidr in raw_value.split(","):
        value = raw_cidr.strip()
        if not value:
            continue
        cidrs.append(str(ipaddress.ip_network(value, strict=False)))
    return cidrs


def render_source_allowlist(raw_value: str) -> str:
    cidrs = parse_cidrs(raw_value)
    if not cidrs:
        raise ValueError("SOURCE_ALLOWLIST_CIDRS must contain at least one CIDR")

    lines = [
        "# Generated during deployment from SOURCE_ALLOWLIST_CIDRS.",
        "# Only /api/v1/source/* includes this file.",
    ]
    lines.extend(f"allow {cidr};" for cidr in cidrs)
    lines.append("deny all;")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cidrs", required=True, help="Comma-separated source CIDRs.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.write_text(render_source_allowlist(args.cidrs), encoding="utf-8")


if __name__ == "__main__":
    main()
