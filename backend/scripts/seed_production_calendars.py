"""Validate and seed reviewed production trading calendars.

The initial production release is intentionally bounded to the official TWSE
2025 and 2026 schedules already published and reviewed in this repository.
Future years are added by a later reviewed change; their absence must not
prevent the initial deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.canonical import CalendarMarket
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
CALENDAR_FILES = {
    2025: "holiday_schedule_114.csv",
    2026: "holiday_schedule_115.csv",
}
MARKET = "TW"
SOURCE_KIND = "twse_csv"
PRODUCTION_TARGET = "production"


class ProductionCalendarSeedError(RuntimeError):
    """Raised when reviewed calendar state cannot be seeded safely."""


@dataclass(frozen=True)
class ReviewedCalendar:
    year: int
    filename: str
    source_bytes: bytes
    source_sha256: str
    detected_encoding: str
    rows: list[dict[str, Any]]

    def validation_report(self) -> dict[str, Any]:
        return {
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
        market=MARKET,
        display_name="臺灣",
        timezone="Asia/Taipei",
        weekend_days=[5, 6],
        default_session_open=time(9, 0),
        default_session_close=time(13, 30),
        active=True,
    )


def load_reviewed_calendars(
    calendar_directory: Path = CALENDAR_DIRECTORY,
) -> list[ReviewedCalendar]:
    market = expected_tw_market()
    reviewed: list[ReviewedCalendar] = []
    for year, filename in CALENDAR_FILES.items():
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
                year=year,
                filename=filename,
                source_bytes=source_bytes,
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                detected_encoding=encoding,
                rows=rows,
            )
        )
    return reviewed


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


async def _get_or_create_market(db: AsyncSession) -> CalendarMarket:
    expected = expected_tw_market()
    actual = (
        await db.execute(
            select(CalendarMarket).where(CalendarMarket.market == MARKET).with_for_update()
        )
    ).scalar_one_or_none()
    if actual is None:
        db.add(expected)
        await db.flush()
        return expected
    if not _market_matches(actual, expected):
        raise ProductionCalendarSeedError("existing TW calendar market configuration differs")
    return actual


async def seed_reviewed_calendars(
    db: AsyncSession,
    *,
    reviewed: list[ReviewedCalendar],
    actor: str,
) -> dict[str, Any]:
    """Seed both reviewed years atomically and return a bounded audit report."""
    if [item.year for item in reviewed] != list(CALENDAR_FILES):
        raise ProductionCalendarSeedError("reviewed calendar years are incomplete or unordered")
    market = await _get_or_create_market(db)
    results: list[dict[str, Any]] = []
    for item in reviewed:
        current = await latest_revision(db, MARKET, item.year)
        if current is not None:
            days = await revision_days(db, current.id)
            exact = (
                current.status == "published"
                and current.source_kind == SOURCE_KIND
                and current.source_filename == item.filename
                and current.source_sha256 == item.source_sha256
                and current.actual_days == current.expected_days == len(item.rows)
                and len(days) == len(item.rows)
            )
            if not exact:
                raise ProductionCalendarSeedError(
                    f"existing TW {item.year} calendar differs from reviewed source"
                )
            results.append({"year": item.year, "action": "unchanged", "revision": current.revision})
            continue

        batch = await create_preview(
            db,
            market=market,
            year=item.year,
            input_format=SOURCE_KIND,
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
            expected_revision=0,
            actor=actor,
            commit=False,
        )
        published = await publish_revision(
            db,
            market=MARKET,
            year=item.year,
            expected_revision=revision.revision,
            published_by=actor,
            commit=False,
        )
        results.append({"year": item.year, "action": "created", "revision": published.revision})
    await db.commit()
    return {
        "deployment_target": PRODUCTION_TARGET,
        "market": MARKET,
        "coverage_start_year": min(CALENDAR_FILES),
        "coverage_end_year": max(CALENDAR_FILES),
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
                report = await seed_reviewed_calendars(db, reviewed=reviewed, actor=args.actor)
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
