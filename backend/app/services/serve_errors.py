"""Coded error responses shared by every Serve route.

Every Serve error body is ``{"detail": {"code", "message", "details"?}}`` so a
client, or an agent calling Serve as a tool, can branch on ``code`` instead of
parsing prose.
"""

from typing import Any

from fastapi import HTTPException

SERVE_HTTP_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "API_KEY_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    422: "INVALID_PARAMETER",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def serve_error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> HTTPException:
    detail: dict[str, Any] = {"code": code, "message": message}
    if details:
        detail["details"] = details
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def fallback_code(status_code: int) -> str:
    """The code of an error raised without one, such as a router-level 404."""
    return SERVE_HTTP_STATUS_CODES.get(status_code, f"HTTP_{status_code}")
