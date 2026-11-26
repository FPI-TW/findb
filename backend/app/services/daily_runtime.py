"""Provider-wide acquisition windows; active providers have no extra daily gate."""

from datetime import datetime


def daily_window(provider: str, now: datetime | None = None) -> dict:
    """Every active provider may acquire at any time; feed clocks come from feed_schedule.

    Callers keep honouring a closed window, so a provider-wide gate can return here
    without changing the queue, admission or authorization paths that read it.
    """
    return {"open": True, "next_open_at": None, "closes_at": None}
