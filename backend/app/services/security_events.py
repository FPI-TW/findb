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
    # Fields go in the message too: the API log formatter prints only the message.
    # Request-derived values use %r so control characters cannot forge log lines.
    logger.warning(
        "API credential rejected credential_kind=%s endpoint=%r client_ip=%r",
        credential_kind,
        endpoint,
        client_ip or "unknown",
        extra={
            "security_event": "invalid_api_credential",
            "credential_kind": credential_kind,
            "endpoint": endpoint,
            "client_ip": client_ip or "unknown",
        },
    )


def record_admin_session_revoked(
    reason: str,
    *,
    session_id: str,
    endpoint: str,
    client_ip: str | None,
) -> None:
    """Record that a session was revoked in response to suspicious use."""
    logger.warning(
        "Admin session revoked reason=%s session_id=%s endpoint=%r client_ip=%r",
        reason,
        session_id,
        endpoint,
        client_ip or "unknown",
        extra={
            "security_event": "admin_session_revoked",
            "reason": reason,
            "session_id": session_id,
            "endpoint": endpoint,
            "client_ip": client_ip or "unknown",
        },
    )


def auth_failure_counts() -> dict[str, int]:
    with _lock:
        return dict(_auth_failures)
