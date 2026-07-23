"""
Fix routing-bug residue where Taiwan futures/options codes were stored as
``TW/equity`` and a duplicate ``0050`` was stored as ``TW/equity`` alongside
the canonical ``TW/etf`` record.

Actions (idempotent):

1. Delete the duplicate ``TW/equity 0050`` row and its child EOD records.
   The canonical ``TW/etf 0050`` row is kept; its EOD series is independent.

2. Reclassify Taiwan futures/options codes currently sitting in
   ``TW/equity`` to ``WTX/future``. EOD rows follow automatically because
   they reference ``instrument_id``.

Symbols matched as futures/options:

- ``TXF*`` and any ``TXF*_AO`` / ``TXF*_AV`` suffix (Taiwan index futures
  & their AM/PM session variants).
- Two-letter prefix + ``F`` + digit (``EPF1``, ``OMF1``, ``ZEF2`` …) —
  single-stock futures codes used by the futures normalizer.

The script aborts the reclassify for any symbol that already exists under
``WTX/future`` to avoid violating the ``(asset_class, market, symbol)``
unique constraint.

Run with ``--dry-run`` (default) to preview, ``--apply`` to write.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.canonical import (
    CorporateAction,
    FuturesContinuousEOD,
    FuturesContract,
    Instrument,
    InstrumentIdentifier,
    MarketDataEOD,
)
from app.models.registry import DQIssue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


FUTURES_SYMBOL_RE = re.compile(r"^(TXF[A-Z0-9]+(?:_A[OV])?|[A-Z]{2}F[0-9])$")

CHILD_TABLES_FOR_DELETE = (
    ("instrument_identifiers", InstrumentIdentifier),
    ("market_data_eod", MarketDataEOD),
    ("corporate_action", CorporateAction),
    ("futures_continuous_eod", FuturesContinuousEOD),
    ("futures_contract", FuturesContract),
    ("dq_issue", DQIssue),
)


async def _delete_instrument_with_children(
    session: AsyncSession, instrument_id: UUID, *, apply: bool
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label, model in CHILD_TABLES_FOR_DELETE:
        stmt = select(model).where(model.instrument_id == instrument_id)
        result = await session.execute(stmt)
        rows = result.scalars().all()
        counts[label] = len(rows)
        if apply and rows:
            await session.execute(delete(model).where(model.instrument_id == instrument_id))
    if apply:
        await session.execute(delete(Instrument).where(Instrument.instrument_id == instrument_id))
    counts["instruments"] = 1
    return counts


async def delete_duplicate_0050(session: AsyncSession, *, apply: bool) -> dict[str, int] | None:
    stmt = select(Instrument).where(
        Instrument.market == "TW",
        Instrument.asset_class == "equity",
        Instrument.symbol == "0050",
    )
    result = await session.execute(stmt)
    inst = result.scalar_one_or_none()
    if inst is None:
        logger.info("0050 duplicate: not present (already cleaned)")
        return None
    logger.info("0050 duplicate: deleting %s", inst.instrument_id)
    counts = await _delete_instrument_with_children(session, inst.instrument_id, apply=apply)
    logger.info("  children removed: %s", counts)
    return counts


async def reclassify_futures(session: AsyncSession, *, apply: bool) -> tuple[int, int]:
    stmt = select(Instrument).where(
        Instrument.market == "TW",
        Instrument.asset_class == "equity",
    )
    result = await session.execute(stmt)
    candidates = [row for row in result.scalars().all() if FUTURES_SYMBOL_RE.match(row.symbol)]
    logger.info("reclassify candidates: %d", len(candidates))

    if not candidates:
        return 0, 0

    symbols = [c.symbol for c in candidates]
    conflict_stmt = select(Instrument).where(
        Instrument.market == "WTX",
        Instrument.asset_class == "future",
        Instrument.symbol.in_(symbols),
    )
    conflict_result = await session.execute(conflict_stmt)
    conflicts = {row.symbol for row in conflict_result.scalars().all()}
    if conflicts:
        logger.error(
            "abort reclassify: %d symbols already exist under WTX/future: %s",
            len(conflicts),
            sorted(conflicts),
        )
        return 0, len(conflicts)

    if not apply:
        for c in candidates[:10]:
            logger.info("  would move %s → WTX/future", c.symbol)
        if len(candidates) > 10:
            logger.info("  ...and %d more", len(candidates) - 10)
        return len(candidates), 0

    ids = [c.instrument_id for c in candidates]
    await session.execute(
        update(Instrument)
        .where(Instrument.instrument_id.in_(ids))
        .values(market="WTX", asset_class="future")
    )
    logger.info("reclassified %d rows to WTX/future", len(candidates))
    return len(candidates), 0


async def run(apply: bool) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as session:
        await delete_duplicate_0050(session, apply=apply)
        moved, conflicts = await reclassify_futures(session, apply=apply)
        if conflicts:
            logger.error("aborted with conflicts; no changes applied to futures")
            await session.rollback()
            await engine.dispose()
            return
        if apply:
            await session.commit()
            logger.info("committed: moved=%d", moved)
        else:
            logger.info("dry-run: no changes committed (moved=%d)", moved)

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag the script runs as a dry run.",
    )
    args = parser.parse_args()
    asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    main()
