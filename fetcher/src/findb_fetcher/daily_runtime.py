"""Pure daily acquisition policy; completion deadlines are independent."""

from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def validate_policies(policies: dict[str, Any]) -> None:
    if not isinstance(policies, dict) or set(policies) != {"shioaji"}:
        raise ValueError("daily runtime policy scope invalid")
    policy = policies["shioaji"]
    if not isinstance(policy, dict) or set(policy) != {
        "timezone",
        "start",
        "end",
        "max_members",
        "max_seconds",
    }:
        raise ValueError("daily runtime policy shape invalid")
    ZoneInfo(policy["timezone"])
    start, end = time.fromisoformat(policy["start"]), time.fromisoformat(policy["end"])
    if start.tzinfo or end.tzinfo or start == end:
        raise ValueError("daily runtime window invalid")
    for key in ("max_members", "max_seconds"):
        if type(policy[key]) is not int or policy[key] < 1:
            raise ValueError("daily runtime slice bound invalid")


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
