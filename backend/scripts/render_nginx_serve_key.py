"""Render nginx map directive that injects a dedicated same-origin lookup key."""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_REFERER_REGEX = (
    r"^https?://findb\.tingfong\.com/"
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
        default=DEFAULT_REFERER_REGEX,
        help="nginx-flavoured regex; Referers matching this get the injected key.",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.write_text(
        render_serve_key(args.key, args.allowed_referer_regex),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
