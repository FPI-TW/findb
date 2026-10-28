"""Bounded waits for provider data that has not been published yet."""

PUBLICATION_RETRY_SECONDS = 15 * 60


class ProviderPublicationPendingError(RuntimeError):
    """A successful provider query has not exposed the target date/scope yet."""

    def __init__(
        self, reason: str, *, retry_after_seconds: int = PUBLICATION_RETRY_SECONDS
    ) -> None:
        super().__init__(reason)
        self.retry_after_seconds = max(60, retry_after_seconds)


class CalendarDecisionRepairRequiredError(RuntimeError):
    """The current published calendar conflicts with one frozen planned decision."""
