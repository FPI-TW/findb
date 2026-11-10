"""Provider-neutral Full daily failures shared by the runtime and provider adapters."""

from __future__ import annotations


class ReadinessBlockedError(RuntimeError):
    pass


class ProviderHTTPError(ReadinessBlockedError):
    """Preserve the public universe error while retaining a dispatched HTTP status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.dispatched = True


class MemberUnavailableError(RuntimeError):
    """One member cannot be delivered while the shared provider response is sound.

    ``code`` is a bounded class (never a provider message); the member is blocked
    for this plan and the round continues with the other members.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DatedUniversePendingError(ReadinessBlockedError):
    """A date-local prerequisite; other frozen dates may still make progress."""

    def __init__(self, reason: str, *, retry_after_seconds: int = 3600) -> None:
        super().__init__(reason)
        self.retry_after_seconds = max(60, retry_after_seconds)


def retryable_http_status(status: int | None) -> bool:
    """Shared retry classification for provider HTTP status codes."""
    return status is None or status in {408, 429} or status >= 500
