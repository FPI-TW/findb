"""Read-only published-date overlays and deterministic weekday defaults."""

import hashlib
import json
from datetime import date, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision

MARKETS = {
    "TW": ("Asia/Taipei", time(9), time(13, 30)),
    "US": ("America/New_York", time(9, 30), time(16)),
    "HK": ("Asia/Hong_Kong", time(9, 30), time(16, 10)),
    "CN": ("Asia/Shanghai", time(9, 30), time(15)),
    "TAIFEX": ("Asia/Taipei", time(8, 45), time(13, 45)),
}
PROVIDER_CALENDAR_SWITCHES = {
    "twelve_data": "TWELVE_DATA_SKIP_NON_TRADING_DAYS",
    "finlab": "FINLAB_SKIP_NON_TRADING_DAYS",
    "shioaji": "SHIOAJI_SKIP_NON_TRADING_DAYS",
    "taifex": "TAIFEX_SKIP_NON_TRADING_DAYS",
    "factset": "FACTSET_SKIP_NON_TRADING_DAYS",
}


def execution_policy(provider: str) -> dict[str, Any]:
    if provider not in PROVIDER_CALENDAR_SWITCHES:
        raise ValueError("unsupported execution provider")
    policy: dict[str, Any] = {
        "version": 1,
        "skip_non_trading_days": getattr(get_settings(), PROVIDER_CALENDAR_SWITCHES[provider]),
    }
    policy["sha256"] = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return policy


async def effective_days(
    db: AsyncSession, market: str, start: date, end: date
) -> list[dict[str, Any]]:
    market = market.upper()
    configured = await db.get(CalendarMarket, market)
    normal_open: time | None
    normal_close: time | None
    if market in MARKETS:
        zone, normal_open, normal_close = MARKETS[market]
        weekend_days = set(configured.weekend_days) if configured is not None else {5, 6}
    else:
        if configured is None:
            raise ValueError("unsupported calendar market")
        zone = configured.timezone
        weekend_days = set(configured.weekend_days)
        normal_open, normal_close = (
            configured.default_session_open,
            configured.default_session_close,
        )
    if end < start or start.year < 1900 or end.year > 2200:
        raise ValueError("invalid calendar range")
    rows = (
        await db.execute(
            select(CalendarRevisionDay, CalendarYearRevision)
            .join(
                CalendarYearRevision,
                CalendarRevisionDay.calendar_revision_id == CalendarYearRevision.id,
            )
            .where(
                CalendarYearRevision.market == market,
                CalendarYearRevision.status == "published",
                CalendarRevisionDay.trade_date >= start,
                CalendarRevisionDay.trade_date <= end,
            )
        )
    ).all()
    published = {}
    for day, revision in rows:
        if (
            day.trade_date in published
            or day.trade_date.year != revision.year
            or revision.timezone != zone
            or day.day_status not in {"open", "closed", "settlement_only"}
            or day.is_open != (day.day_status == "open")
        ):
            raise ValueError("invalid published calendar data")
        published[day.trade_date] = (day, revision)
    result = []
    for offset in range((end - start).days + 1):
        current = start + timedelta(days=offset)
        pair = published.get(current)
        day, revision = pair if pair else (None, None)
        is_open = day.is_open if day else current.weekday() not in weekend_days
        opening = (day.session_open if day and is_open else None) or normal_open
        closing = (day.session_close if day and is_open else None) or normal_close
        result.append(
            {
                "market": market,
                "trade_date": current.isoformat(),
                "is_open": is_open,
                "day_status": day.day_status if day else "open" if is_open else "closed",
                "timezone": zone,
                "session_open": opening.isoformat() if is_open and opening else None,
                "session_close": closing.isoformat() if is_open and closing else None,
                "regular_close": closing.isoformat() if closing else None,
                "after_hours_close": "05:00:00" if market == "TAIFEX" else None,
                "holiday_name": day.holiday_name if day else None,
                "description": day.description if day else None,
                "calendar_source": "published" if day else "weekday_default",
                "calendar_revision": revision.revision if revision else None,
                "calendar_revision_id": str(revision.id) if revision else None,
            }
        )
    return result


async def execution_snapshot(
    db: AsyncSession, market: str, trade_date: date, provider: str
) -> dict[str, Any]:
    day = (await effective_days(db, market, trade_date, trade_date))[0]
    return {**day, "policy": execution_policy(provider), "snapshot_origin": "creation"}


async def ensure_market_for_edit(db: AsyncSession, market: str):
    """Create fixed market metadata only during an authenticated write operation."""
    from sqlalchemy.dialects.postgresql import insert

    existing = await db.get(CalendarMarket, market)
    if existing is not None:
        return existing
    if market not in MARKETS:
        raise ValueError("unsupported calendar market")
    zone, opening, closing = MARKETS[market]
    await db.execute(
        insert(CalendarMarket)
        .values(
            market=market,
            display_name=market,
            timezone=zone,
            weekend_days=[5, 6],
            default_session_open=opening,
            default_session_close=closing,
            active=True,
        )
        .on_conflict_do_nothing(index_elements=[CalendarMarket.market])
    )
    return await db.get(CalendarMarket, market)


async def acquisition_dates(
    db: AsyncSession,
    *,
    market: str,
    dataset_key: str,
    provider: str,
    profile: str,
    start: date,
    end: date,
) -> list[date]:
    """Project eligible dates without changing frozen decisions or writing state."""
    from app.models.registry import DailyExecutionDecision

    if end < start:
        return []
    decisions = {
        row.trade_date: row.kind
        for row in (
            await db.scalars(
                select(DailyExecutionDecision).where(
                    DailyExecutionDecision.dataset_key == dataset_key,
                    DailyExecutionDecision.provider == provider,
                    DailyExecutionDecision.execution_profile == profile,
                    DailyExecutionDecision.trade_date >= start,
                    DailyExecutionDecision.trade_date <= end,
                )
            )
        ).all()
    }
    skip = execution_policy(provider)["skip_non_trading_days"]
    return [
        date.fromisoformat(day["trade_date"])
        for day in await effective_days(db, market, start, end)
        if decisions.get(date.fromisoformat(day["trade_date"])) == "planned"
        or (date.fromisoformat(day["trade_date"]) not in decisions and (not skip or day["is_open"]))
    ]
