"""Intraday download manifests: presigned Parquet URLs for one instrument-month.

Research use reads the canonical Parquet objects directly instead of paging
JSON.  A manifest lists, for one instrument, record kind and month, every object
the index names, with the only trade dates to take from each: a month object can
still hold rows of a day that was republished after the merge, and that day is
listed under its own day object instead.

Only keys holding the ``intraday_download`` scope get a manifest, and each URL
counts against the key's daily limit.  The count is charged atomically in
``intraday_download_usage`` before any URL is returned; that table is the one
thing the Serve role writes.  Canonical data stays read-only.
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.canonical import IntradayPartition
from app.models.registry import APIKey, IntradayDownloadUsage
from app.schemas.ingress import INTRADAY_ROW_LAYOUTS
from app.schemas.serve import (
    IntradayDownloadColumn,
    IntradayDownloadDay,
    IntradayDownloadLayout,
    IntradayDownloadManifestResponse,
    IntradayDownloadObject,
    IntradayDownloadQuota,
    IntradayUnitsResponse,
)
from app.services.object_storage import ObjectStorageConfigError, ObjectStorageError
from app.services.serve import serve_error
from app.services.serve_intraday import SESSIONS, _instrument, _resolve, canonical_reader
from app.utils import utc_now

logger = logging.getLogger(__name__)

DOWNLOAD_SCOPE = "intraday_download"
_MONTH = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")
RECORD_KINDS: dict[str, Literal["bar_1m", "tick"]] = {"bars": "bar_1m", "ticks": "tick"}


@dataclass
class _Object:
    key: str
    layout: str
    etag: str
    days: list[IntradayDownloadDay]


def _month_bounds(month: str) -> tuple[date, date]:
    match = _MONTH.fullmatch(month)
    if match is None:
        raise serve_error(422, "INVALID_MONTH", "month must be YYYY-MM")
    year, number = int(match["year"]), int(match["month"])
    return date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])


def _layout(kind: Literal["bar_1m", "tick"]) -> IntradayDownloadLayout:
    return IntradayDownloadLayout(
        schema_version=1,
        record_kind=kind,
        columns=[
            IntradayDownloadColumn(
                name=column.name,
                type=column.type,
                nullable=column.nullable,
                description=column.description,
            )
            for column in INTRADAY_ROW_LAYOUTS[kind]
        ],
    )


async def _charge(
    db: AsyncSession, key_id: UUID, count: int, usage_date: date
) -> IntradayDownloadQuota:
    """Add ``count`` to today's usage if the key's limit allows; refuse otherwise.

    One statement both checks and adds, so concurrent manifests of one key (two
    Serve processes, two workers) can never exceed the limit together.
    """
    limit = await db.scalar(
        select(APIKey.intraday_download_daily_limit).where(APIKey.key_id == key_id)
    )
    if limit is None:  # pragma: no cover - the dependency authenticated this key
        raise HTTPException(status_code=403, detail="Invalid API key")
    used = None
    if count <= limit:
        now = utc_now()
        statement = (
            insert(IntradayDownloadUsage)
            .values(key_id=key_id, usage_date=usage_date, object_count=count, updated_at=now)
            .on_conflict_do_update(
                index_elements=[IntradayDownloadUsage.key_id, IntradayDownloadUsage.usage_date],
                set_={
                    "object_count": IntradayDownloadUsage.object_count + count,
                    "updated_at": now,
                },
                where=IntradayDownloadUsage.object_count + count <= limit,
            )
            .returning(IntradayDownloadUsage.object_count)
        )
        used = (await db.execute(statement)).scalar_one_or_none()
    if used is None:
        await db.rollback()
        current = await db.scalar(
            select(IntradayDownloadUsage.object_count).where(
                IntradayDownloadUsage.key_id == key_id,
                IntradayDownloadUsage.usage_date == usage_date,
            )
        )
        tomorrow = usage_date + timedelta(days=1)
        raise HTTPException(
            status_code=429,
            detail={
                "code": "DOWNLOAD_QUOTA_EXCEEDED",
                "message": (
                    f"This manifest needs {count} objects; the key has "
                    f"{max(0, limit - int(current or 0))} of {limit} left for {usage_date}"
                ),
            },
            headers={"Retry-After": str(_seconds_until(tomorrow))},
        )
    await db.commit()
    return IntradayDownloadQuota(
        usage_date=usage_date, daily_limit=limit, used=used, remaining=limit - used
    )


def _seconds_until(day: date) -> int:
    boundary = datetime.combine(day, time.min, timezone.utc)
    return max(1, int((boundary - utc_now()).total_seconds()))


async def intraday_download_manifest(
    db: AsyncSession,
    *,
    key_id: UUID,
    instrument_id: UUID,
    kind: Literal["bars", "ticks"],
    month: str,
) -> IntradayDownloadManifestResponse:
    first, last = _month_bounds(month)
    record_kind = RECORD_KINDS[kind]
    instrument, scope = await _resolve(db, instrument_id, record_kind)
    rows = await db.execute(
        select(
            IntradayPartition.trade_date,
            IntradayPartition.row_count,
            IntradayPartition.object_layout,
            IntradayPartition.object_key,
            IntradayPartition.object_etag,
        )
        .where(
            IntradayPartition.dataset_key == scope.dataset_key,
            IntradayPartition.symbol == instrument.symbol,
            IntradayPartition.trade_date.between(first, last),
        )
        .order_by(IntradayPartition.trade_date)
    )
    objects: dict[str, _Object] = {}
    for trade_date, row_count, layout, key, etag in rows:
        item = objects.setdefault(key, _Object(key, layout, etag, []))
        item.days.append(IntradayDownloadDay(trade_date=trade_date, row_count=row_count))
    # Sign every URL before charging, so a signing failure costs the key nothing.
    settings = get_settings()
    ttl = settings.INTRADAY_DOWNLOAD_URL_TTL_SECONDS
    try:
        reader = canonical_reader()
        urls = {key: reader.presign_get(key, expires_in=ttl) for key in objects}
    except (ObjectStorageConfigError, ObjectStorageError):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "INTRADAY_STORAGE_UNAVAILABLE",
                "message": "Intraday storage is not available",
            },
        ) from None
    issued_at = utc_now()
    quota = await _charge(db, key_id, len(objects), issued_at.date())
    logger.info(
        "Intraday download manifest issued key=%s dataset=%s symbol=%s month=%s objects=%d "
        "used=%d/%d",
        key_id,
        scope.dataset_key,
        instrument.symbol,
        month,
        len(objects),
        quota.used,
        quota.daily_limit,
    )
    return IntradayDownloadManifestResponse(
        instrument=_instrument(instrument),
        dataset_key=scope.dataset_key,
        month=month,
        units=IntradayUnitsResponse(market_timezone=SESSIONS[instrument.market].timezone_name),
        layout=_layout(record_kind),
        expires_at=issued_at + timedelta(seconds=ttl),
        objects=[
            IntradayDownloadObject(
                url=urls[item.key],
                object_layout=cast(Literal["day", "month"], item.layout),
                object_etag=item.etag,
                trade_dates=item.days,
            )
            for item in objects.values()
        ],
        quota=quota,
    )
