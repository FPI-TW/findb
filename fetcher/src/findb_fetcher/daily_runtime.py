"""Pure daily acquisition policy; completion deadlines are independent."""

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def window(policy: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("daily runtime clock must be timezone-aware")
    if policy is None:
        return {"open": True, "next_open_at": None, "closes_at": None}
    local = now.astimezone(ZoneInfo(policy["timezone"]))
    start = datetime.combine(local.date(), time.fromisoformat(policy["start"]), local.tzinfo)
    end = datetime.combine(local.date(), time.fromisoformat(policy["end"]), local.tzinfo)
    if end < start:
        if local < end:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    opened = start <= local < end
    next_start = start if local < start else start + timedelta(days=1)
    return {
        "open": opened,
        "next_open_at": None if opened else next_start.isoformat(),
        "closes_at": end.isoformat() if opened else None,
    }
