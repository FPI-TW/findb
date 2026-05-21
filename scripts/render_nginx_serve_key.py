"""Render nginx map directive that injects the Serve API key for same-origin static pages."""

from __future__ import annotations

import argparse
from pathlib import Path

DEFAULT_REFERER_REGEX = r"^https?://findb\.tingfong\.com/instrument-lookup"


def render_serve_key(raw_keys: str, allowed_referer_regex: str) -> str:
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    inject_key = keys[0] if keys else ""

    for ch in ('"', "\\", "\n"):
        if ch in inject_key:
            raise ValueError(
                "SERVE_API_KEYS first entry must not contain quote, backslash or newline"
            )

    header = (
        "# Generated during deployment from SERVE_API_KEYS.\n"
        "# Injects X-API-Key into /api/v1/serve/* requests whose Referer matches the\n"
        "# instrument-lookup static page; external callers without a Referer match\n"
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
            "# SERVE_API_KEYS is empty; falling back to passthrough only.\n"
            "map $http_referer $findb_serve_proxy_key {\n"
            "    default $http_x_api_key;\n"
            "}\n"
        )

    return header + body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--keys",
        required=True,
        help="Comma-separated SERVE_API_KEYS; the first entry is injected.",
    )
    parser.add_argument(
        "--allowed-referer-regex",
        default=DEFAULT_REFERER_REGEX,
        help="nginx-flavoured regex; Referers matching this get the injected key.",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.write_text(
        render_serve_key(args.keys, args.allowed_referer_regex),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
