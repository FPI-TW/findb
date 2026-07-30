"""Render nginx map directive that injects a dedicated same-origin lookup key."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

DEFAULT_REFERER_REGEX = (
    r"^https?://findb-staging\.tingfong\.com/"
    r"(?:instrument-lookup(?:[/?#]|$)|dashboard/lookup(?:[/?#]|$))"
)


def referer_regex_for_host(public_host: str) -> str:
    host = public_host.strip().lower().rstrip(".")
    if not re.fullmatch(
        r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
        host,
    ):
        raise ValueError("public host must be a valid DNS hostname")
    return (
        rf"^https?://{re.escape(host)}/"
        r"(?:instrument-lookup(?:[/?#]|$)|dashboard/lookup(?:[/?#]|$))"
    )


def render_serve_key(lookup_key: str, allowed_referer_regex: str) -> str:
    inject_key = lookup_key.strip()

    for ch in ('"', "\\", "\n", "\r"):
        if ch in inject_key:
            raise ValueError(
                "FINDB_LOOKUP_SERVE_API_KEY must not contain quote, backslash or newline"
            )

    header = (
        "# Generated during deployment from FINDB_LOOKUP_SERVE_API_KEY.\n"
        "# Injects X-API-Key into /api/v1/serve/* requests whose Referer matches the\n"
        "# public instrument lookup pages; external callers without a Referer match\n"
        "# fall through to passing their own X-API-Key header.\n"
    )

    if inject_key:
        body = (
            "map $http_referer $findb_serve_proxy_key {\n"
            "    default                                                $http_x_api_key;\n"
            f'    "~{allowed_referer_regex}"  "{inject_key}";\n'
            "}\n"
        )
    else:
        body = (
            "# FINDB_LOOKUP_SERVE_API_KEY is empty; falling back to passthrough only.\n"
            "map $http_referer $findb_serve_proxy_key {\n"
            "    default $http_x_api_key;\n"
            "}\n"
        )

    return header + body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--key",
        required=True,
        help="Dedicated FINDB_LOOKUP_SERVE_API_KEY injected for allowed Referers.",
    )
    parser.add_argument(
        "--allowed-referer-regex",
        help="nginx-flavoured regex; Referers matching this get the injected key.",
    )
    parser.add_argument(
        "--public-host",
        help="Public DNS hostname used to derive the allowed lookup Referer regex.",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if args.allowed_referer_regex and args.public_host:
        parser.error("use either --allowed-referer-regex or --public-host, not both")
    allowed_referer_regex = (
        referer_regex_for_host(args.public_host)
        if args.public_host
        else args.allowed_referer_regex or DEFAULT_REFERER_REGEX
    )

    args.output.write_text(
        render_serve_key(args.key, allowed_referer_regex),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
