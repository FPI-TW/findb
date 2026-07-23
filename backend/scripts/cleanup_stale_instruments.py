"""
Remove stale duplicate instrument records that survived past ingest bugs.

Background:
- The instruments table uses (asset_class, market, symbol) as the uniqueness key,
  so the same symbol can legitimately appear with different asset_class or market
  (e.g. HK 0700 vs. TW 0700 — different companies that happen to share a digit string).
- However, an audit of production /static/data/instruments.json on 2026-05-19 found
  three classes of bad records caused by legacy ingest paths:

    A. Same-market / wrong-asset-class duplicates
       - 0050 stored as TW/equity in addition to the canonical TW/etf
       - US index tickers (SPX, NDX, INDU, RTY, SOX, VIX, BM7T, S5*) stored as
         US/equity in addition to the canonical US/index
       - VIX additionally stored as FX/fx

    B. Wrong-market duplicates (TW/CN numeric codes routed into US)
       - 4938 / 6125 / 6506 (TW) and 688322 (CN) created as US/equity

    C. Wrong asset-class
       - TXF1 created as TW/equity in addition to the canonical WTX/future

This script removes the bad rows above plus their dependent children rows
(instrument_identifiers, market_data_eod, corporate_action, futures_contract,
futures_continuous_eod, dq_issue). canonical_correction uses ON DELETE SET NULL.

Run with --dry-run (default) to preview, --apply to delete.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from uuid import UUID

from sqlalchemy import delete, func, select
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


# Instrument IDs that should be deleted, taken from the 2026-05-19 production audit.
# Each entry is (instrument_id, expected_market, expected_asset_class, expected_symbol, reason).
# The script verifies the (market, asset_class, symbol) before deleting to avoid
# accidentally wiping the wrong record.
STALE_INSTRUMENTS: list[tuple[str, str, str, str, str]] = [
    # A. Same-market / wrong-asset-class duplicates
    (
        "019e017d-3e9a-72de-b312-bcd6fd8cc96b",
        "TW",
        "equity",
        "0050",
        "0050 is an ETF; TW/equity copy is a pre-FinLab residue.",
    ),
    (
        "019cd628-a449-70d1-b212-f91066ab7511",
        "US",
        "equity",
        "BM7T",
        "BM7T is an index; US/equity copy is a stale Bloomberg ingest.",
    ),
    (
        "019cd628-a3b4-74d3-8245-2182dda1eb4f",
        "US",
        "equity",
        "INDU",
        "INDU is an index; US/equity copy is a stale Bloomberg ingest.",
    ),
    (
        "019cd628-a3a9-758c-a2fb-e8f99a6129fb",
        "US",
        "equity",
        "NDX",
        "NDX is an index; US/equity copy is a stale Bloomberg ingest.",
    ),
    (
        "019cd628-a3c6-738a-9bd5-6924ea135a1d",
        "US",
        "equity",
        "RTY",
        "RTY is an index; US/equity copy is a stale Bloomberg ingest.",
    ),
    (
        "019cd628-a3d7-767c-81e1-0480e58a6af4",
        "US",
        "equity",
        "S5COND",
        "S5COND is an S&P sector index.",
    ),
    (
        "019cd628-a440-7ecb-9c80-4b071401062e",
        "US",
        "equity",
        "S5CONS",
        "S5CONS is an S&P sector index.",
    ),
    (
        "019cd628-a42c-7a89-b5a9-fcd48eea83ec",
        "US",
        "equity",
        "S5ENRS",
        "S5ENRS is an S&P sector index.",
    ),
    (
        "019cd628-a41c-74d7-8e9b-ec8a17241204",
        "US",
        "equity",
        "S5FINL",
        "S5FINL is an S&P sector index.",
    ),
    (
        "019cd628-a424-7a8c-8f1c-f67b614bfcc1",
        "US",
        "equity",
        "S5HLTH",
        "S5HLTH is an S&P sector index.",
    ),
    (
        "019cd628-a3f1-73ab-b69c-e400a989ae8a",
        "US",
        "equity",
        "S5INDU",
        "S5INDU is an S&P sector index.",
    ),
    (
        "019cd628-a3cf-7c96-9de8-e75a1691e9ec",
        "US",
        "equity",
        "S5INFT",
        "S5INFT is an S&P sector index.",
    ),
    (
        "019cd628-a415-7b09-9eec-a67fddf26617",
        "US",
        "equity",
        "S5MATR",
        "S5MATR is an S&P sector index.",
    ),
    (
        "019cd628-a3e9-70ac-b45e-d2e82f7793d3",
        "US",
        "equity",
        "S5RLST",
        "S5RLST is an S&P sector index.",
    ),
    (
        "019cd628-a3e0-75ad-ab56-2b9292e8184b",
        "US",
        "equity",
        "S5TELS",
        "S5TELS is an S&P sector index.",
    ),
    (
        "019cd628-a435-70ae-84c1-20fc72a0015c",
        "US",
        "equity",
        "S5UTIL",
        "S5UTIL is an S&P sector index.",
    ),
    (
        "019cd628-a3be-7d45-9a77-f2dec530e73b",
        "US",
        "equity",
        "SOX",
        "SOX is the PHLX semiconductor index.",
    ),
    ("019cd628-a389-7572-b3bc-50104e873be9", "US", "equity", "SPX", "SPX is the S&P 500 index."),
    (
        "019cd628-a40c-788b-ac4e-518def5021dd",
        "US",
        "equity",
        "VIX",
        "VIX is a volatility index, not a US equity.",
    ),
    (
        "019ce681-20b4-7711-9662-6d43709b480e",
        "FX",
        "fx",
        "VIX",
        "VIX is a volatility index, not an FX pair.",
    ),
    # B. Wrong-market: TW/CN numeric codes routed into US/equity
    (
        "019cd628-a4a9-70c5-8c20-313030a2f156",
        "US",
        "equity",
        "4938",
        "4938 is a TW equity (Pegatron); US/equity record is a routing bug.",
    ),
    (
        "019cd628-a49e-7896-b07b-1e7c2ab79a00",
        "US",
        "equity",
        "6125",
        "6125 is a TW equity; US/equity record is a routing bug.",
    ),
    (
        "019cd628-a540-715c-b75a-7973767313f4",
        "US",
        "equity",
        "6506",
        "6506 is a TW equity; US/equity record is a routing bug.",
    ),
    (
        "019cd628-a55e-769a-874d-3518aa7afdd4",
        "US",
        "equity",
        "688322",
        "688322 is a CN STAR-board equity; US/equity record is a routing bug.",
    ),
    # C. Wrong asset class
    (
        "019e0184-4d11-7f43-88a7-f5c209603036",
        "TW",
        "equity",
        "TXF1",
        "TXF1 is a Taiwan futures contract; canonical record is WTX/future.",
    ),
]


async def _count(session: AsyncSession, model, instrument_id: UUID) -> int:
    stmt = select(func.count()).select_from(model).where(model.instrument_id == instrument_id)
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def _delete_children(
    session: AsyncSession, instrument_id: UUID, *, apply: bool
) -> dict[str, int]:
    """Delete dependent rows that block deleting the instrument."""
    counts: dict[str, int] = {}
    # Order matters: identifiers and EOD are leaves; futures_contract is
    # referenced by futures_continuous_eod via instrument_id directly so both
    # are children of instruments and can be deleted independently.
    for label, model in (
        ("instrument_identifiers", InstrumentIdentifier),
        ("market_data_eod", MarketDataEOD),
        ("corporate_action", CorporateAction),
        ("futures_continuous_eod", FuturesContinuousEOD),
        ("futures_contract", FuturesContract),
        ("dq_issue", DQIssue),
    ):
        n = await _count(session, model, instrument_id)
        counts[label] = n
        if apply and n:
            await session.execute(delete(model).where(model.instrument_id == instrument_id))
    return counts


async def cleanup(apply: bool) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    deleted = 0
    skipped = 0
    try:
        async with session_maker() as session:
            for raw_id, market, asset_class, symbol, reason in STALE_INSTRUMENTS:
                instrument_id = UUID(raw_id)
                stmt = select(Instrument).where(Instrument.instrument_id == instrument_id)
                row = (await session.execute(stmt)).scalar_one_or_none()

                if row is None:
                    logger.info(
                        "[skip] %s %s/%s %s: not found (already cleaned?)",
                        raw_id,
                        market,
                        asset_class,
                        symbol,
                    )
                    skipped += 1
                    continue

                if (row.market, row.asset_class, row.symbol) != (market, asset_class, symbol):
                    logger.warning(
                        "[skip] %s expected %s/%s %s but DB has %s/%s %s — refusing to delete",
                        raw_id,
                        market,
                        asset_class,
                        symbol,
                        row.market,
                        row.asset_class,
                        row.symbol,
                    )
                    skipped += 1
                    continue

                child_counts = await _delete_children(session, instrument_id, apply=apply)
                summary = ", ".join(f"{k}={v}" for k, v in child_counts.items() if v)
                action = "delete" if apply else "would delete"
                logger.info(
                    "[%s] %s %s/%s %s — %s | children: %s",
                    action,
                    raw_id,
                    market,
                    asset_class,
                    symbol,
                    reason,
                    summary or "none",
                )

                if apply:
                    await session.execute(
                        delete(Instrument).where(Instrument.instrument_id == instrument_id)
                    )
                deleted += 1

            if apply:
                await session.commit()
                logger.info("Committed deletion of %d instruments (%d skipped).", deleted, skipped)
            else:
                await session.rollback()
                logger.info(
                    "Dry run: %d instruments would be deleted, %d skipped. "
                    "Re-run with --apply to commit.",
                    deleted,
                    skipped,
                )
    finally:
        await engine.dispose()

    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete rows. Without this flag the script runs in dry-run mode.",
    )
    args = parser.parse_args()
    asyncio.run(cleanup(apply=args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
