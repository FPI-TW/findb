"""Wait until the deployed durable-ingestion control plane is ready."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.request import Request, urlopen


def validate_queue_health(
    payload: dict[str, Any],
    *,
    maximum_heartbeat_age: float,
) -> list[str]:
    """Return readiness errors from an Admin queue-health response."""
    errors: list[str] = []
    heartbeat_age = payload.get("worker_heartbeat_age_seconds")
    if heartbeat_age is None:
        errors.append("worker heartbeat has not been recorded")
    elif float(heartbeat_age) > maximum_heartbeat_age:
        errors.append(
            f"worker heartbeat is {float(heartbeat_age):.1f}s old "
            f"(maximum {maximum_heartbeat_age:.1f}s)"
        )
    expired_leases = int(payload.get("expired_leases") or 0)
    if expired_leases:
        errors.append(f"{expired_leases} normalization execution leases are expired")
    return errors


def fetch_queue_health(url: str, *, api_key_header: str, api_key: str) -> dict[str, Any]:
    request = Request(url, headers={api_key_header: api_key})
    with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed internal deploy URL
        return json.load(response)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=f"http://127.0.0.1:{os.getenv('PORT', '8080')}/api/v1/admin/queue/health",
    )
    parser.add_argument("--attempts", type=int, default=24)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--maximum-heartbeat-age", type=float, default=90.0)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    api_key = os.getenv("ADMIN_API_KEY", "").strip()
    if not api_key:
        print("error: ADMIN_API_KEY is required", file=sys.stderr)
        return 2
    api_key_header = os.getenv("API_KEY_HEADER", "X-API-Key")

    last_errors: list[str] = []
    for attempt in range(1, max(1, args.attempts) + 1):
        try:
            payload = fetch_queue_health(
                args.url,
                api_key_header=api_key_header,
                api_key=api_key,
            )
            last_errors = validate_queue_health(
                payload,
                maximum_heartbeat_age=args.maximum_heartbeat_age,
            )
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
            if not last_errors:
                return 0
        except Exception as exc:
            last_errors = [f"queue-health request failed: {type(exc).__name__}: {exc}"]

        print(
            f"queue control plane not ready (attempt {attempt}/{args.attempts}): "
            + "; ".join(last_errors),
            file=sys.stderr,
        )
        if attempt < args.attempts:
            time.sleep(max(0.0, args.interval))

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
