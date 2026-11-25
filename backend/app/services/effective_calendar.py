"""Read-only published-date overlays and deterministic weekday defaults."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision
from app.services.provider_metadata import provider_catalog

# The only Backend market -> timezone authority; catalog feeds are checked against it.
MARKETS = {
    "TW": ("Asia/Taipei", time(9), time(13, 30)),
    "US": ("America/New_York", time(9, 30), time(16)),
    "HK": ("Asia/Hong_Kong", time(9, 30), time(16, 10)),
    "CN": ("Asia/Shanghai", time(9, 30), time(15)),
    "TAIFEX": ("Asia/Taipei", time(8, 45), time(13, 45)),
}


def market_timezone(market: str) -> ZoneInfo:
    """Fail closed instead of guessing a timezone for an unconfigured market."""
    if market not in MARKETS:
        raise ValueError("unsupported calendar market")
    return ZoneInfo(MARKETS[market][0])


PROVIDER_CALENDAR_SWITCHES = {
    provider.provider: provider.calendar_switch_env for provider in provider_catalog().providers
}


def execution_policy(provider: str) -> dict[str, Any]:
    if provider not in PROVIDER_CALENDAR_SWITCHES and provider != "shioaji":
        raise ValueError("unsupported execution provider")
    policy: dict[str, Any] = {
        "version": 1,
        # Historical Shioaji snapshots remain readable after removal of its runtime setting.
        "skip_non_trading_days": True
        if provider == "shioaji"
        else getattr(get_settings(), PROVIDER_CALENDAR_SWITCHES[provider]),
    }
    policy["sha256"] = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return policy


def _market_defaults(market: str, configured: CalendarMarket | None):
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
    return zone, weekend_days, normal_open, normal_close


def _check_range(start: date, end: date) -> None:
    if end < start or start.year < 1900 or end.year > 2200:
        raise ValueError("invalid calendar range")


def _published_days_statement(markets: list[str], start: date, end: date):
    return (
        select(CalendarRevisionDay, CalendarYearRevision)
        .join(
            CalendarYearRevision,
            CalendarRevisionDay.calendar_revision_id == CalendarYearRevision.id,
        )
        .where(
            CalendarYearRevision.market.in_(markets),
            CalendarYearRevision.status == "published",
            CalendarRevisionDay.trade_date >= start,
            CalendarRevisionDay.trade_date <= end,
        )
    )


def _build_days(
    market: str,
    configured: CalendarMarket | None,
    rows: list[tuple[CalendarRevisionDay, CalendarYearRevision]],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    zone, weekend_days, normal_open, normal_close = _market_defaults(market, configured)
    published: dict[date, tuple[Any, Any]] = {}
    for row_day, row_revision in rows:
        if (
            row_day.trade_date in published
            or row_day.trade_date.year != row_revision.year
            or row_revision.timezone != zone
            or row_day.day_status not in {"open", "closed", "settlement_only"}
            or row_day.is_open != (row_day.day_status == "open")
        ):
            raise ValueError("invalid published calendar data")
        published[row_day.trade_date] = (row_day, row_revision)
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


async def effective_days(
    db: AsyncSession, market: str, start: date, end: date
) -> list[dict[str, Any]]:
    market = market.upper()
    configured = await db.get(CalendarMarket, market)
    _market_defaults(market, configured)
    _check_range(start, end)
    rows = list((await db.execute(_published_days_statement([market], start, end))).tuples())
    return _build_days(market, configured, rows, start, end)


async def effective_days_many(
    db: AsyncSession, ranges: dict[str, tuple[date, date]]
) -> dict[str, list[dict[str, Any]]]:
    """`effective_days` for several markets in two reads, whatever the number of markets."""
    ranges = {market.upper(): bounds for market, bounds in ranges.items()}
    if not ranges:
        return {}
    for start, end in ranges.values():
        _check_range(start, end)
    configured = {
        row.market: row
        for row in await db.scalars(
            select(CalendarMarket).where(CalendarMarket.market.in_(sorted(ranges)))
        )
    }
    for market in ranges:
        _market_defaults(market, configured.get(market))
    start = min(bounds[0] for bounds in ranges.values())
    end = max(bounds[1] for bounds in ranges.values())
    by_market: dict[str, list[tuple[CalendarRevisionDay, CalendarYearRevision]]] = {
        market: [] for market in ranges
    }
    for day, revision in (
        await db.execute(_published_days_statement(sorted(ranges), start, end))
    ).tuples():
        low, high = ranges[revision.market]
        if low <= day.trade_date <= high:
            by_market[revision.market].append((day, revision))
    return {
        market: _build_days(market, configured.get(market), by_market[market], low, high)
        for market, (low, high) in ranges.items()
    }


async def published_calendar_year(db: AsyncSession, market: str, year: int) -> dict[str, Any]:
    """Return one year of effective days and whether it is published, defaulted or mixed."""
    if year < 1900 or year > 2200:
        raise ValueError("year must be between 1900 and 2200")
    market = market.upper()
    days = await effective_days(db, market, date(year, 1, 1), date(year, 12, 31))
    sources = {day["calendar_source"] for day in days}
    status: Literal["published", "defaulted", "mixed"] = (
        "published"
        if sources == {"published"}
        else "defaulted"
        if sources == {"weekday_default"}
        else "mixed"
    )
    # Only one published revision exists per market and year, so any day carries it.
    return {
        "market": market,
        "year": year,
        "revision": days[0]["calendar_revision"] if status == "published" else None,
        "status": status,
        "coverage_complete": True,
        "timezone": days[0]["timezone"],
        "expected_days": len(days),
        "actual_days": len(days),
        "days": days,
    }


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


async def _decisions(
    db: AsyncSession, *, dataset_key: str, provider: str, profile: str, start: date, end: date
) -> dict[date, Any]:
    from app.models.registry import DailyExecutionDecision

    return {
        row.trade_date: row
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


def _closed_candidates(
    decisions: dict[date, Any], days: list[dict[str, Any]]
) -> dict[Any, tuple[date, str]]:
    """Planned dates the published calendar now closes: plan_id -> (day, revision id)."""
    closed = {
        date.fromisoformat(day["trade_date"]): day["calendar_revision_id"]
        for day in days
        if not day["is_open"] and day["calendar_source"] == "published"
    }
    return {
        decision.plan_id: (day, closed[day])
        for day, decision in decisions.items()
        if day in closed and decision.kind == "planned" and decision.plan_id is not None
    }


async def _written_plans(db: AsyncSession, plan_ids: set[Any]) -> set[Any]:
    """Plans with any run, data or no-data proof among the given ones."""
    from app.models.registry import (
        DailyDeliveryPart,
        DailyDeliveryPlanSummary,
        IngestionRun,
    )

    if not plan_ids:
        return set()
    return set(
        await db.scalars(
            select(DailyDeliveryPart.plan_id)
            .join(IngestionRun, IngestionRun.delivery_part_id == DailyDeliveryPart.part_id)
            .where(DailyDeliveryPart.plan_id.in_(plan_ids))
        )
    ) | set(
        await db.scalars(
            select(DailyDeliveryPlanSummary.plan_id).where(
                DailyDeliveryPlanSummary.plan_id.in_(plan_ids),
                DailyDeliveryPlanSummary.data_count + DailyDeliveryPlanSummary.no_data_count > 0,
            )
        )
    )


def _retired(candidates: dict[Any, tuple[date, str]], written: set[Any]) -> dict[date, dict]:
    return {
        day: {"plan_id": str(plan_id), "calendar_revision_id": revision_id}
        for plan_id, (day, revision_id) in candidates.items()
        if plan_id not in written
    }


async def _unwritten_closed_plans(
    db: AsyncSession, decisions: dict[date, Any], days: list[dict[str, Any]]
) -> dict[date, dict[str, Any]]:
    """Plans frozen on a day the published calendar now closes, that never wrote anything.

    Early versions planned some weekends and holidays before their calendar was
    published; every member stayed blocked and no provider ever delivered. Such a
    date is not expected. A plan with any run, data or no-data proof stays expected:
    a write on a closed day means the calendar or the data needs a look.
    """
    candidates = _closed_candidates(decisions, days)
    if not candidates:
        return {}
    return _retired(candidates, await _written_plans(db, set(candidates)))


async def unwritten_closed_plans(
    db: AsyncSession,
    *,
    market: str,
    dataset_key: str,
    provider: str,
    profile: str,
    start: date,
    end: date,
) -> dict[date, dict[str, Any]]:
    """Planned dates no longer expected: closed by the published calendar, never written."""
    if end < start:
        return {}
    decisions = await _decisions(
        db, dataset_key=dataset_key, provider=provider, profile=profile, start=start, end=end
    )
    return await _unwritten_closed_plans(
        db, decisions, await effective_days(db, market, start, end)
    )


def _eligible_dates(
    provider: str,
    decisions: dict[date, Any],
    days: list[dict[str, Any]],
    retired: dict[date, dict[str, Any]],
) -> list[date]:
    skip = execution_policy(provider)["skip_non_trading_days"]
    result = []
    for day in days:
        current = date.fromisoformat(day["trade_date"])
        decision = decisions.get(current)
        if decision is not None and decision.kind == "planned":
            if current not in retired:
                result.append(current)
        elif decision is None and (not skip or day["is_open"]):
            result.append(current)
    return result


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
    if end < start:
        return []
    decisions = await _decisions(
        db, dataset_key=dataset_key, provider=provider, profile=profile, start=start, end=end
    )
    days = await effective_days(db, market, start, end)
    retired = await _unwritten_closed_plans(db, decisions, days)
    return _eligible_dates(provider, decisions, days, retired)


@dataclass(frozen=True)
class AcquisitionRequest:
    market: str
    dataset_key: str
    provider: str
    profile: str
    start: date
    end: date


async def acquisition_dates_many(
    db: AsyncSession, requests: list[AcquisitionRequest]
) -> list[list[date]]:
    """`acquisition_dates` for many feeds in a fixed number of reads, in request order."""
    from app.models.registry import DailyExecutionDecision

    live = [request for request in requests if request.end >= request.start]
    if not live:
        return [[] for _ in requests]
    feeds = sorted({(item.dataset_key, item.provider, item.profile) for item in live})
    decisions: dict[tuple[str, str, str], dict[date, Any]] = {feed: {} for feed in feeds}
    for row in await db.scalars(
        select(DailyExecutionDecision).where(
            tuple_(
                DailyExecutionDecision.dataset_key,
                DailyExecutionDecision.provider,
                DailyExecutionDecision.execution_profile,
            ).in_(feeds),
            DailyExecutionDecision.trade_date >= min(item.start for item in live),
            DailyExecutionDecision.trade_date <= max(item.end for item in live),
        )
    ):
        decisions[(row.dataset_key, row.provider, row.execution_profile)][row.trade_date] = row
    ranges: dict[str, tuple[date, date]] = {}
    for item in live:
        market = item.market.upper()
        low, high = ranges.get(market, (item.start, item.end))
        ranges[market] = (min(low, item.start), max(high, item.end))
    calendars = await effective_days_many(db, ranges)
    prepared = []
    for item in live:
        days = [
            day
            for day in calendars[item.market.upper()]
            if item.start <= date.fromisoformat(day["trade_date"]) <= item.end
        ]
        scoped = {
            day: row
            for day, row in decisions[(item.dataset_key, item.provider, item.profile)].items()
            if item.start <= day <= item.end
        }
        prepared.append((item, scoped, days, _closed_candidates(scoped, days)))
    written = await _written_plans(
        db, {plan_id for *_, candidates in prepared for plan_id in candidates}
    )
    results = {
        id(item): _eligible_dates(item.provider, scoped, days, _retired(candidates, written))
        for item, scoped, days, candidates in prepared
    }
    return [results.get(id(request), []) for request in requests]
