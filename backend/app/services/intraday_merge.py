"""Merge the day objects of finished months, in the Worker's idle time.

What still needs merging is read from the index, so nothing is scheduled or
queued: a pass that is skipped, interrupted or run twice changes nothing.
Normalization keeps priority — a dataset with deliveries waiting is left alone,
and each month is merged under the same dataset lock normalization takes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import exists, extract, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.canonical import IntradayPartition
from app.models.registry import DatasetRegistry, NormalizationJob
from app.services.ingress_contracts import parse_dataset_contract_declaration
from app.services.object_storage import ObjectStorageError
from app.utils import utc_now

logger = logging.getLogger(__name__)
settings = get_settings()

EXCHANGE_TIMEZONE = ZoneInfo("Asia/Taipei")
MonthUnit = tuple[str, str, int, int]


def merge_boundary(now: datetime, grace_days: int) -> date:
    """First day of the earliest month that is not yet merged.

    A month is merged once it ended more than ``grace_days`` ago on the
    exchange calendar, leaving time for late republication of its last days.
    """
    return (now.astimezone(EXCHANGE_TIMEZONE).date() - timedelta(days=grace_days)).replace(day=1)


async def pending_months(session: AsyncSession, *, boundary: date, limit: int) -> list[MonthUnit]:
    """Symbol-months before ``boundary`` that still have a day object."""
    year = extract("year", IntradayPartition.trade_date)
    month = extract("month", IntradayPartition.trade_date)
    rows = await session.execute(
        select(IntradayPartition.dataset_key, IntradayPartition.symbol, year, month)
        .where(
            IntradayPartition.object_layout == "day",
            IntradayPartition.trade_date < boundary,
        )
        .group_by(IntradayPartition.dataset_key, IntradayPartition.symbol, year, month)
        .order_by(IntradayPartition.dataset_key, year, month, IntradayPartition.symbol)
        .limit(limit)
    )
    return [(dataset_key, symbol, int(y), int(m)) for dataset_key, symbol, y, m in rows]


async def _deliveries_waiting(session: AsyncSession, dataset_key: str) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(
                    NormalizationJob.dataset_key == dataset_key,
                    NormalizationJob.status.in_(("queued", "retrying", "processing")),
                )
            )
        )
    )


async def _record_kind(session: AsyncSession, dataset_key: str) -> str | None:
    dataset = await session.get(DatasetRegistry, dataset_key)
    try:
        declaration = parse_dataset_contract_declaration(dataset.config if dataset else None)
    except ValueError:
        return None
    return declaration.defaults.record_kind if declaration is not None else None


async def merge_finished_months(
    *,
    database_url: str | None = None,
    now: datetime | None = None,
) -> int:
    """Run one pass and return how many symbol-months were merged."""
    engine = create_async_engine(database_url or settings.DATABASE_URL, pool_size=1, max_overflow=0)
    merged = 0
    try:
        # One connection for the whole pass: the dataset lock is session-level.
        async with engine.connect() as connection:
            factory = async_sessionmaker(
                bind=connection, class_=AsyncSession, expire_on_commit=False
            )
            async with factory() as session:
                units = await pending_months(
                    session,
                    boundary=merge_boundary(now or utc_now(), settings.INTRADAY_MERGE_GRACE_DAYS),
                    limit=settings.INTRADAY_MERGE_BATCH_SIZE,
                )
                await session.commit()
                if not units:
                    return 0
                # Parquet and R2 support are loaded only when there is something to merge.
                from app.services import intraday_partitions as store
                from app.services.normalize.intraday import canonical_publisher

                publisher = canonical_publisher()
                skipped: set[str] = set()
                for dataset_key, symbol, year, month in units:
                    if dataset_key in skipped:
                        continue
                    kind = await _record_kind(session, dataset_key)
                    if kind is None or await _deliveries_waiting(session, dataset_key):
                        skipped.add(dataset_key)
                        await session.commit()
                        continue
                    locked = await session.scalar(
                        text("SELECT pg_try_advisory_lock(hashtext(:dataset_key))"),
                        {"dataset_key": dataset_key},
                    )
                    await session.commit()
                    if not locked:
                        skipped.add(dataset_key)
                        continue
                    try:
                        superseded = await store.merge_month(
                            session,
                            publisher,
                            dataset_key=dataset_key,
                            symbol=symbol,
                            year=year,
                            month=month,
                            kind=kind,
                        )
                        await session.commit()
                    except store.IntradayStorageError as exc:
                        # This month waits for its pending delivery; others can go on.
                        await session.rollback()
                        logger.warning(
                            "Intraday month not merged (%s %s %04d-%02d): %s",
                            dataset_key,
                            symbol,
                            year,
                            month,
                            exc,
                        )
                        continue
                    except ObjectStorageError as exc:
                        await session.rollback()
                        logger.warning("Intraday merge pass stopped by object storage: %s", exc)
                        break
                    finally:
                        await session.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:dataset_key))"),
                            {"dataset_key": dataset_key},
                        )
                        await session.commit()
                    await asyncio.to_thread(store.delete_superseded, publisher, superseded)
                    merged += 1
    finally:
        await engine.dispose()
    return merged
