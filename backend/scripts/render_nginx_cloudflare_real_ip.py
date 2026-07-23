"""Render nginx real-IP directives for Cloudflare-proxied traffic."""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CLOUDFLARE_IP_URLS = (
    "https://www.cloudflare.com/ips-v4",
    "https://www.cloudflare.com/ips-v6",
)

FALLBACK_CIDRS = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)


def parse_cidr_lines(raw_value: str) -> list[str]:
    cidrs: list[str] = []
    for raw_cidr in raw_value.splitlines():
        value = raw_cidr.strip()
        if not value or value.startswith("#"):
            continue
        cidrs.append(str(ipaddress.ip_network(value, strict=False)))
    return cidrs


def _dedupe(cidrs: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for cidr in cidrs:
        if cidr in seen:
            continue
        seen.add(cidr)
        unique.append(cidr)
    return unique


def fetch_url(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": "findb-deploy/1.0 (+https://github.com/FPI-TW/findb)"},
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310 - trusted Cloudflare URL constants.
        return response.read().decode("utf-8")


def load_cloudflare_cidrs(fetch_text: Callable[[str], str] = fetch_url) -> tuple[list[str], str]:
    cidrs: list[str] = []
    for url in CLOUDFLARE_IP_URLS:
        raw_value = fetch_text(url)
        parsed = parse_cidr_lines(raw_value)
        if not parsed:
            raise ValueError(f"{url} did not contain any CIDRs")
        cidrs.extend(parsed)
    return _dedupe(cidrs), "official Cloudflare IP lists"


def load_cidrs_with_fallback(
    fetch_text: Callable[[str], str] = fetch_url,
) -> tuple[list[str], str]:
    try:
        return load_cloudflare_cidrs(fetch_text)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        print(
            f"Warning: failed to fetch Cloudflare IP ranges ({exc}); using fallback CIDRs.",
            file=sys.stderr,
        )
        return list(FALLBACK_CIDRS), "fallback Cloudflare IPv4 CIDRs"


def render_cloudflare_real_ip(fetch_text: Callable[[str], str] = fetch_url) -> str:
    cidrs, source = load_cidrs_with_fallback(fetch_text)

    lines = [
        "# Generated during deployment from Cloudflare IP ranges.",
        f"# Source: {source}.",
        "real_ip_header CF-Connecting-IP;",
        "real_ip_recursive on;",
    ]
    lines.extend(f"set_real_ip_from {cidr};" for cidr in cidrs)
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.write_text(render_cloudflare_real_ip(), encoding="utf-8")


if __name__ == "__main__":
    main()
