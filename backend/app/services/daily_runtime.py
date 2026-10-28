"""Authoritative daily runtime policy from the shared execution catalog."""

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.utils import utc_now


def daily_window(provider: str, now: datetime | None = None) -> dict:
    catalog = json.loads(
        (Path(__file__).resolve().parents[2] / "configs/execution_feeds.v1.json").read_text()
    )
    policy = catalog["daily_runtime_policies"].get(provider)
    if policy is None:
        return {"open": True, "next_open_at": None, "closes_at": None}
    clock = now or utc_now()
    if clock.tzinfo is None:
        raise ValueError("daily runtime clock must be timezone-aware")
    local = clock.astimezone(ZoneInfo(policy["timezone"]))
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
