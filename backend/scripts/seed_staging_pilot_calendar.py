"""Publish only the reviewed TAIFEX pilot calendar on staging.

Existing published operator calendars are preserved. US/TW/HK/CN revisions
are never read or written by this bootstrap.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.calendar_management import (
    apply_preview,
    create_preview,
    latest_revision,
    publish_revision,
    revision_days,
)
from scripts.seed_production_calendars import (
    CALENDAR_DIRECTORY,
    ProductionCalendarSeedError,
    _get_or_create_market,
    _load_reviewed_2026_calendar,
)


async def seed_staging_pilot_calendar(db: AsyncSession) -> dict[str, object]:
    item = _load_reviewed_2026_calendar(CALENDAR_DIRECTORY, "TAIFEX")
    market = await _get_or_create_market(db, "TAIFEX")
    current = await latest_revision(db, "TAIFEX", item.year)
    if current is not None:
        days = await revision_days(db, current.id)
        if (
            current.status != "published"
            or current.actual_days != current.expected_days
            or len(days) != 365
            or current.timezone != "Asia/Taipei"
        ):
            raise ProductionCalendarSeedError("existing TAIFEX calendar needs operator publication")
        await db.commit()
        return {
            "market": "TAIFEX",
            "year": item.year,
            "revision": current.revision,
            "action": "operator_revision_preserved",
        }
    preview = await create_preview(
        db,
        market=market,
        year=item.year,
        input_format=item.source_kind,
        rows=item.rows,
        source_bytes=item.source_bytes,
        source_filename=item.filename,
        detected_encoding=item.detected_encoding,
        created_by="staging-pilot-bootstrap",
        commit=False,
    )
    revision = await apply_preview(
        db, batch_id=preview.id, expected_revision=0, actor="staging-pilot-bootstrap", commit=False
    )
    published = await publish_revision(
        db,
        market="TAIFEX",
        year=item.year,
        expected_revision=revision.revision,
        published_by="staging-pilot-bootstrap",
        commit=False,
    )
    await db.commit()
    return {
        "market": "TAIFEX",
        "year": item.year,
        "revision": published.revision,
        "action": "created",
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-target", choices=("staging",), required=True)
    parser.parse_args()
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return 2
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            report = await seed_staging_pilot_calendar(db)
            print(json.dumps(report, sort_keys=True))
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
