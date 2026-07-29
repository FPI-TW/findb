"""Wait until the deployed durable-ingestion control plane is ready."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from typing import Any
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen


def validate_queue_health(
    payload: dict[str, Any],
    *,
    maximum_heartbeat_age: float,
    require_empty_dlq: bool = False,
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
    dlq_fields = ("dlq_ready", "dlq_unacked", "dlq_depth")
    if any(payload.get(field) is None for field in dlq_fields):
        errors.append("normalization DLQ metrics are unavailable")
        return errors
    dlq_ready = int(payload["dlq_ready"])
    dlq_unacked = int(payload["dlq_unacked"])
    dlq_depth = int(payload["dlq_depth"])
    if min(dlq_ready, dlq_unacked, dlq_depth) < 0:
        errors.append("normalization DLQ metrics must be non-negative")
    elif dlq_depth != dlq_ready + dlq_unacked:
        errors.append(
            "normalization DLQ metrics are inconsistent "
            f"(depth={dlq_depth}, ready={dlq_ready}, unacked={dlq_unacked})"
        )
    elif require_empty_dlq and dlq_depth:
        errors.append(
            "normalization DLQ is not empty "
            f"(depth={dlq_depth}, ready={dlq_ready}, unacked={dlq_unacked})"
        )
    return errors


def fetch_queue_health(url: str, *, api_key_header: str, api_key: str) -> dict[str, Any]:
    request = Request(url, headers={api_key_header: api_key})
    with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed internal deploy URL
        return json.load(response)


def fetch_dlq_health(
    management_url: str,
    *,
    broker_url: str,
    vhost: str,
    queue_name: str,
    urlopen_func: Any = urlopen,
) -> dict[str, int]:
    """Read passive DLQ counters from RabbitMQ's existing management API."""
    broker = urlparse(broker_url)
    if not broker.username or broker.password is None:
        raise ValueError("CELERY_BROKER_URL must contain RabbitMQ credentials")
    management = urlparse(management_url)
    if management.scheme not in {"http", "https"} or not management.hostname:
        raise ValueError("RabbitMQ management URL must be an HTTP(S) origin")
    if (
        management.username
        or management.password
        or management.path not in {"", "/"}
        or management.query
        or management.fragment
    ):
        raise ValueError("RabbitMQ management URL must not contain credentials or query data")

    credentials = f"{unquote(broker.username)}:{unquote(broker.password)}".encode()
    authorization = base64.b64encode(credentials).decode("ascii")
    endpoint = (
        f"{management_url.rstrip('/')}/api/queues/"
        f"{quote(vhost, safe='')}/{quote(queue_name, safe='')}"
    )
    request = Request(endpoint, headers={"Authorization": f"Basic {authorization}"})
    with urlopen_func(request, timeout=10) as response:  # noqa: S310 - internal RabbitMQ management origin
        payload = json.load(response)

    values: dict[str, int] = {}
    for target, source in (
        ("dlq_ready", "messages_ready"),
        ("dlq_unacked", "messages_unacknowledged"),
        ("dlq_depth", "messages"),
    ):
        value = payload.get(source)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"RabbitMQ response is missing integer field {source}")
        values[target] = value
    return values


def fetch_readiness_payload(
    url: str,
    *,
    api_key_header: str,
    api_key: str,
    management_url: str,
    broker_url: str,
    vhost: str,
    queue_name: str,
    queue_health_fetcher: Any = fetch_queue_health,
    dlq_health_fetcher: Any = fetch_dlq_health,
) -> dict[str, Any]:
    """Combine DB-authoritative health with passive broker DLQ counters."""
    payload = queue_health_fetcher(
        url,
        api_key_header=api_key_header,
        api_key=api_key,
    )
    payload.update(
        dlq_health_fetcher(
            management_url,
            broker_url=broker_url,
            vhost=vhost,
            queue_name=queue_name,
        )
    )
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=f"http://127.0.0.1:{os.getenv('PORT', '8080')}/api/v1/admin/queue/health",
    )
    parser.add_argument("--attempts", type=int, default=24)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--maximum-heartbeat-age", type=float, default=90.0)
    parser.add_argument(
        "--rabbitmq-management-url",
        default=os.getenv("RABBITMQ_MANAGEMENT_URL", "http://rabbitmq:15672"),
    )
    parser.add_argument(
        "--rabbitmq-vhost",
        default=os.getenv("RABBITMQ_VHOST", "/findb"),
    )
    parser.add_argument(
        "--normalization-dlq",
        default=os.getenv("NORMALIZATION_DLQ", "findb.normalize.dlq.v1"),
    )
    parser.add_argument(
        "--require-empty-dlq",
        action="store_true",
        help="Fail unless the normalization DLQ is empty; use before mass import.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    api_key = os.getenv("FINDB_QUEUE_HEALTH_ADMIN_API_KEY", "").strip()
    if not api_key:
        print("error: FINDB_QUEUE_HEALTH_ADMIN_API_KEY is required", file=sys.stderr)
        return 2
    broker_url = os.getenv("CELERY_BROKER_URL", "").strip()
    if not broker_url:
        print("error: CELERY_BROKER_URL is required", file=sys.stderr)
        return 2
    api_key_header = os.getenv("API_KEY_HEADER", "X-API-Key")

    last_errors: list[str] = []
    for attempt in range(1, max(1, args.attempts) + 1):
        try:
            payload = fetch_readiness_payload(
                args.url,
                api_key_header=api_key_header,
                api_key=api_key,
                management_url=args.rabbitmq_management_url,
                broker_url=broker_url,
                vhost=args.rabbitmq_vhost,
                queue_name=args.normalization_dlq,
            )
            last_errors = validate_queue_health(
                payload,
                maximum_heartbeat_age=args.maximum_heartbeat_age,
                require_empty_dlq=args.require_empty_dlq,
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
