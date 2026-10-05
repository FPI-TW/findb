"""Validate and seed reviewed production trading calendars.

Production uses reviewed TWSE 2025-2026, NYSE 2025-2028, and explicit 2026
HK, CN and TAIFEX calendars committed in this repository. Coverage remains
bounded. Known legacy US 2026 sessions require an explicit reviewed upgrade.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision
from app.services.calendar_management import (
    CalendarError,
    apply_preview,
    create_preview,
    latest_revision,
    parse_twse_csv,
    publish_revision,
    revision_days,
)

CALENDAR_DIRECTORY = Path(__file__).resolve().parents[1] / "configs" / "calendars"
TW_CALENDAR_FILES = {
    2025: "holiday_schedule_114.csv",
    2026: "holiday_schedule_115.csv",
}
CALENDAR_FILES = TW_CALENDAR_FILES
US_CALENDAR_FILE = "us_equity_2025_2028.v1.json"
US_CALENDAR_YEARS = (2025, 2026, 2027, 2028)
REVIEWED_2026_FILES = {
    "US": "us_equity_2026.v2.json",
    "HK": "hk_equity_2026.v1.json",
    "CN": "cn_equity_2026.v1.json",
    "TAIFEX": "taifex_index_futures_2026.v1.json",
}
REVIEWED_TIMEZONES = {
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
    "CN": "Asia/Shanghai",
    "TAIFEX": "Asia/Taipei",
}
PRODUCTION_TARGET = "production"


class ProductionCalendarSeedError(RuntimeError):
    """Raised when reviewed calendar state cannot be seeded safely."""


@dataclass(frozen=True)
class ReviewedCalendar:
    market: str
    year: int
    filename: str
    source_kind: str
    source_bytes: bytes
    source_sha256: str
    detected_encoding: str
    rows: list[dict[str, Any]]

    def validation_report(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "year": self.year,
            "filename": self.filename,
            "sha256": self.source_sha256,
            "encoding": self.detected_encoding,
            "days": len(self.rows),
            "open": sum(row["status"] == "open" for row in self.rows),
            "closed": sum(row["status"] == "closed" for row in self.rows),
            "settlement_only": sum(row["status"] == "settlement_only" for row in self.rows),
        }


def expected_tw_market() -> CalendarMarket:
    return CalendarMarket(
        market="TW",
        display_name="臺灣",
        timezone="Asia/Taipei",
        weekend_days=[5, 6],
        default_session_open=time(9, 0),
        default_session_close=time(13, 30),
        active=True,
    )


def expected_us_market() -> CalendarMarket:
    return CalendarMarket(
        market="US",
        display_name="美國",
        timezone="America/New_York",
        weekend_days=[5, 6],
        default_session_open=None,
        default_session_close=None,
        active=True,
    )


def _expected_market(market: str) -> CalendarMarket:
    if market == "TW":
        return expected_tw_market()
    if market == "US":
        return expected_us_market()
    if market in {"HK", "CN", "TAIFEX"}:
        return CalendarMarket(
            market=market,
            display_name={"HK": "香港", "CN": "中國", "TAIFEX": "臺灣期貨"}[market],
            timezone=REVIEWED_TIMEZONES[market],
            weekend_days=[5, 6],
            default_session_open=None,
            default_session_close=None,
            active=True,
        )
    raise ProductionCalendarSeedError(f"unsupported reviewed calendar market: {market}")


def _load_reviewed_tw_calendars(calendar_directory: Path) -> list[ReviewedCalendar]:
    market = expected_tw_market()
    reviewed: list[ReviewedCalendar] = []
    for year, filename in TW_CALENDAR_FILES.items():
        path = calendar_directory / filename
        try:
            source_bytes = path.read_bytes()
        except OSError as exc:
            raise ProductionCalendarSeedError(
                f"reviewed calendar file is unavailable: {filename}"
            ) from exc
        try:
            rows, encoding = parse_twse_csv(source_bytes, year=year, market=market)
        except CalendarError as exc:
            raise ProductionCalendarSeedError(
                f"reviewed calendar file is invalid: {filename}"
            ) from exc
        reviewed.append(
            ReviewedCalendar(
                market="TW",
                year=year,
                filename=filename,
                source_kind="twse_csv",
                source_bytes=source_bytes,
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_encoding=encoding,
                rows=rows,
            )
        )
    return reviewed


def _load_reviewed_us_calendars(calendar_directory: Path) -> list[ReviewedCalendar]:
    path = calendar_directory / US_CALENDAR_FILE
    try:
        source_bytes = path.read_bytes()
        payload = json.loads(source_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionCalendarSeedError(
            f"reviewed calendar file is unavailable or invalid: {US_CALENDAR_FILE}"
        ) from exc
    expected_metadata = {
        "calendar_version": 1,
        "calendar_id": "us_equity_2025_2028",
        "market": "US",
        "timezone": "America/New_York",
        "coverage_start_date": "2025-01-01",
        "coverage_end_date": "2028-12-31",
        "source_url": "https://www.nyse.com/trade/hours-calendars",
        "reviewed_at": "2026-09-16",
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected_metadata.items()
    ):
        raise ProductionCalendarSeedError("reviewed US calendar metadata differs")
    raw_holidays = payload.get("non_trading_dates")
    if not isinstance(raw_holidays, list) or not all(
        isinstance(value, str) for value in raw_holidays
    ):
        raise ProductionCalendarSeedError("reviewed US non-trading dates are invalid")
    try:
        holidays = [date.fromisoformat(value) for value in raw_holidays]
    except ValueError as exc:
        raise ProductionCalendarSeedError("reviewed US non-trading date is invalid") from exc
    if holidays != sorted(set(holidays)) or any(
        holiday.year not in US_CALENDAR_YEARS or holiday.weekday() >= 5 for holiday in holidays
    ):
        raise ProductionCalendarSeedError("reviewed US non-trading dates are not canonical")

    holiday_set = set(holidays)
    reviewed: list[ReviewedCalendar] = []
    for year in US_CALENDAR_YEARS:
        cursor = date(year, 1, 1)
        end = date(year + 1, 1, 1)
        rows: list[dict[str, Any]] = []
        while cursor < end:
            is_holiday = cursor in holiday_set
            is_open = cursor.weekday() < 5 and not is_holiday
            rows.append(
                {
                    "trade_date": cursor.isoformat(),
                    "status": "open" if is_open else "closed",
                    "holiday_name": "NYSE non-trading day" if is_holiday else None,
                    "description": ("Reviewed NYSE holiday calendar" if is_holiday else None),
                    "session_open": None,
                    "session_close": None,
                }
            )
            cursor += timedelta(days=1)
        reviewed.append(
            ReviewedCalendar(
                market="US",
                year=year,
                filename=US_CALENDAR_FILE,
                source_kind="nyse_json",
                source_bytes=source_bytes,
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_encoding="utf-8",
                rows=rows,
            )
        )
    return reviewed


def _load_reviewed_2026_calendar(directory: Path, market: str) -> ReviewedCalendar:
    filename = REVIEWED_2026_FILES[market]
    try:
        source_bytes = (directory / filename).read_bytes()
        payload = json.loads(source_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionCalendarSeedError(
            f"reviewed calendar file is unavailable or invalid: {filename}"
        ) from exc
    if not isinstance(payload, dict) or any(
        payload.get(key) != value
        for key, value in {
            "market": market,
            "year": 2026,
            "timezone": REVIEWED_TIMEZONES[market],
            "calendar_version": 2 if market == "US" else 1,
            "coverage_start_date": "2026-01-01",
            "coverage_end_date": "2026-12-31",
        }.items()
    ):
        raise ProductionCalendarSeedError(f"reviewed {market} 2026 calendar metadata differs")
    sources = payload.get("sources")
    if (
        not isinstance(sources, list)
        or not sources
        or not all(
            isinstance(source, dict)
            and isinstance(source.get("url"), str)
            and source["url"].startswith("https://")
            for source in sources
        )
        or not isinstance(payload.get("reviewed_at"), str)
    ):
        raise ProductionCalendarSeedError(f"reviewed {market} 2026 calendar provenance is invalid")
    rows = payload.get("days")
    required_dates = [
        (date(2026, 1, 1) + timedelta(days=offset)).isoformat() for offset in range(365)
    ]
    if not isinstance(rows, list) or len(rows) != 365:
        raise ProductionCalendarSeedError(f"reviewed {market} 2026 calendar is incomplete")
    for expected_date, row in zip(required_dates, rows, strict=True):
        if (
            not isinstance(row, dict)
            or row.get("trade_date") != expected_date
            or not isinstance(row.get("status"), str)
            or row.get("status") not in {"open", "closed"}
        ):
            raise ProductionCalendarSeedError(f"reviewed {market} 2026 calendar dates are invalid")
        is_open = row["status"] == "open"
        if date.fromisoformat(expected_date).weekday() >= 5 and is_open:
            raise ProductionCalendarSeedError(f"reviewed {market} 2026 weekend cannot be open")
        try:
            opened = time.fromisoformat(row["session_open"]) if is_open else None
            closed = time.fromisoformat(row["session_close"]) if is_open else None
            if is_open and (
                opened is None
                or closed is None
                or opened >= closed
                or opened.tzinfo
                or closed.tzinfo
            ):
                raise ValueError("Invalid session")
            if not is_open and (
                row.get("session_open") is not None or row.get("session_close") is not None
            ):
                raise ValueError("Closed session")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductionCalendarSeedError(f"reviewed {market} 2026 session is invalid") from exc
    return ReviewedCalendar(
        market=market,
        year=2026,
        filename=filename,
        source_kind="reviewed_exchange_json",
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        detected_encoding="utf-8",
        rows=rows,
    )


def load_reviewed_calendars(
    calendar_directory: Path = CALENDAR_DIRECTORY,
) -> list[ReviewedCalendar]:
    legacy_us = _load_reviewed_us_calendars(calendar_directory)
    return (
        _load_reviewed_tw_calendars(calendar_directory)
        + [
            _load_reviewed_2026_calendar(calendar_directory, "US") if item.year == 2026 else item
            for item in legacy_us
        ]
        + [
            _load_reviewed_2026_calendar(calendar_directory, market)
            for market in ("HK", "CN", "TAIFEX")
        ]
    )


def _market_matches(actual: CalendarMarket, expected: CalendarMarket) -> bool:
    return all(
        (
            actual.display_name == expected.display_name,
            actual.timezone == expected.timezone,
            actual.weekend_days == expected.weekend_days,
            actual.default_session_open == expected.default_session_open,
            actual.default_session_close == expected.default_session_close,
            actual.active is expected.active,
        )
    )


async def _get_or_create_market(db: AsyncSession, market: str) -> CalendarMarket:
    expected = _expected_market(market)
    actual = (
        await db.execute(
            select(CalendarMarket).where(CalendarMarket.market == market).with_for_update()
        )
    ).scalar_one_or_none()
    if actual is None:
        db.add(expected)
        await db.flush()
        return expected
    if not _market_matches(actual, expected):
        raise ProductionCalendarSeedError(
            f"existing {market} calendar market configuration differs"
        )
    return actual


def _matches_reviewed_revision(
    current: CalendarYearRevision, days: list[CalendarRevisionDay], item: ReviewedCalendar
) -> bool:
    if not (
        current.status == "published"
        and current.timezone == REVIEWED_TIMEZONES.get(item.market, "Asia/Taipei")
        and current.source_kind == item.source_kind
        and current.source_filename == item.filename
        and current.source_sha256 == item.source_sha256
        and current.actual_days == current.expected_days == len(item.rows)
        and len(days) == len(item.rows)
    ):
        return False
    return all(
        day.trade_date.isoformat() == row["trade_date"]
        and day.day_status == row["status"]
        and day.is_open == (row["status"] == "open")
        and day.source_kind == item.source_kind
        and day.holiday_name == row.get("holiday_name")
        and day.description == row.get("description")
        and (day.session_open.isoformat() if day.session_open else None) == row.get("session_open")
        and (day.session_close.isoformat() if day.session_close else None)
        == row.get("session_close")
        for day, row in zip(days, item.rows, strict=True)
    )


async def seed_reviewed_calendars(
    db: AsyncSession,
    *,
    reviewed: list[ReviewedCalendar],
    actor: str,
    upgrade_reviewed_2026: bool = False,
) -> dict[str, Any]:
    """Seed all reviewed market years atomically and return a bounded audit report."""
    expected_identity = [
        *(("TW", year) for year in TW_CALENDAR_FILES),
        *(("US", year) for year in US_CALENDAR_YEARS),
        *((market, 2026) for market in ("HK", "CN", "TAIFEX")),
    ]
    if [(item.market, item.year) for item in reviewed] != expected_identity:
        raise ProductionCalendarSeedError("reviewed calendars are incomplete or unordered")
    markets = {
        market: await _get_or_create_market(db, market)
        for market in ("TW", "US", "HK", "CN", "TAIFEX")
    }
    legacy_us_2026 = next(
        item for item in _load_reviewed_us_calendars(CALENDAR_DIRECTORY) if item.year == 2026
    )
    results: list[dict[str, Any]] = []
    for item in reviewed:
        market = markets[item.market]
        current = await latest_revision(db, item.market, item.year)
        if current is not None:
            days = await revision_days(db, current.id)
            exact = _matches_reviewed_revision(current, days, item)
            known_legacy = (
                item.market == "US"
                and item.year == 2026
                and _matches_reviewed_revision(current, days, legacy_us_2026)
            )
            if not exact and not known_legacy:
                raise ProductionCalendarSeedError(
                    f"existing {item.market} {item.year} calendar differs from reviewed source"
                )
            if exact or not upgrade_reviewed_2026:
                result = {
                    "market": item.market,
                    "year": item.year,
                    "action": "unchanged" if exact else "pending_reviewed_update",
                    "revision": current.revision,
                }
                if not exact:
                    result["upgrade_required"] = True
                results.append(result)
                continue

        batch = await create_preview(
            db,
            market=market,
            year=item.year,
            input_format=item.source_kind,
            rows=item.rows,
            source_bytes=item.source_bytes,
            source_filename=item.filename,
            detected_encoding=item.detected_encoding,
            created_by=actor,
            commit=False,
        )
        revision = await apply_preview(
            db,
            batch_id=batch.id,
            expected_revision=current.revision if current is not None else 0,
            actor=actor,
            commit=False,
        )
        published = await publish_revision(
            db,
            market=item.market,
            year=item.year,
            expected_revision=revision.revision,
            published_by=actor,
            commit=False,
        )
        results.append(
            {
                "market": item.market,
                "year": item.year,
                "action": "upgraded" if current is not None else "created",
                "revision": published.revision,
            }
        )
    await db.commit()
    return {
        "deployment_target": PRODUCTION_TARGET,
        "coverage": {
            "TW": [min(TW_CALENDAR_FILES), max(TW_CALENDAR_FILES)],
            "US": [min(US_CALENDAR_YEARS), max(US_CALENDAR_YEARS)],
            "HK": [2026, 2026],
            "CN": [2026, 2026],
            "TAIFEX": [2026, 2026],
        },
        "future_years_required_for_initial_deploy": False,
        "results": results,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-target",
        choices=(PRODUCTION_TARGET,),
        required=True,
        help="Exact deployment target; this seed is production-only",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the reviewed calendars; without this flag only validate files",
    )
    parser.add_argument("--actor", default="production-bootstrap")
    parser.add_argument(
        "--upgrade-reviewed-2026",
        action="store_true",
        help="Publish US 2026 session-time successor only over the exact known legacy bootstrap",
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    try:
        reviewed = load_reviewed_calendars()
    except ProductionCalendarSeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.apply:
        print(
            json.dumps(
                {
                    "deployment_target": args.deployment_target,
                    "mode": "dry-run",
                    "future_years_required_for_initial_deploy": False,
                    "calendars": [item.validation_report() for item in reviewed],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("error: DATABASE_URL is required with --apply", file=sys.stderr)
        return 2
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            try:
                report = await seed_reviewed_calendars(
                    db,
                    reviewed=reviewed,
                    actor=args.actor,
                    upgrade_reviewed_2026=args.upgrade_reviewed_2026,
                )
            except (CalendarError, ProductionCalendarSeedError) as exc:
                await db.rollback()
                print(f"error: {exc}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001 - bounded, secret-free deployment output
                await db.rollback()
                print(
                    f"error: production calendar seed failed ({type(exc).__name__})",
                    file=sys.stderr,
                )
                return 1
    finally:
        await engine.dispose()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
