"""Safe aggregate metrics and structured events for rejected credentials."""

import logging
from collections import Counter
from threading import Lock

logger = logging.getLogger(__name__)
_lock = Lock()
_auth_failures: Counter[str] = Counter()


def record_invalid_credential(
    credential_kind: str,
    *,
    endpoint: str,
    client_ip: str | None,
) -> None:
    """Record rejection metadata without accepting or retaining the supplied key."""
    with _lock:
        _auth_failures[credential_kind] += 1
    logger.warning(
        "API credential rejected",
        extra={
            "security_event": "invalid_api_credential",
            "credential_kind": credential_kind,
            "endpoint": endpoint,
            "client_ip": client_ip or "unknown",
        },
    )


def auth_failure_counts() -> dict[str, int]:
    with _lock:
        return dict(_auth_failures)
