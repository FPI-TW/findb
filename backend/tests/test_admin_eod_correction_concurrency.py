"""Concurrent Admin EOD corrections must not combine into an OHLC that bypassed DQ."""

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.canonical import Instrument, InstrumentStats, MarketDataEOD
from app.models.correction import CanonicalCorrection
from app.schemas.admin import PatchEODRequest
from app.services import admin as admin_service
from app.services.admin import InvalidCorrectionError, patch_eod_record
from app.utils import utc_now, uuid7

DAY = date(2025, 1, 2)


async def _seed(maker) -> Instrument:
    async with maker() as setup:
        instrument = Instrument(
            instrument_id=uuid7(),
            asset_class="equity",
            market="US",
            symbol="RACE",
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        setup.add(instrument)
        await setup.flush()
        setup.add(
            MarketDataEOD(
                instrument_id=instrument.instrument_id,
                trade_date=DAY,
                open=Decimal("100"),
                high=Decimal("120"),
                low=Decimal("80"),
                close=Decimal("100"),
                volume=1000,
                asof_ts=utc_now(),
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        await setup.commit()
        return instrument


def _hold_first_correction(monkeypatch):
    """Pause the first correction after DQ validation, before its stats write and commit."""
    validated, release = asyncio.Event(), asyncio.Event()
    original = admin_service._update_stats_after_eod_patch

    async def paused(db, eod):
        if not validated.is_set():
            validated.set()
            await release.wait()
        await original(db, eod)

    monkeypatch.setattr(admin_service, "_update_stats_after_eod_patch", paused)
    return validated, release


@pytest.mark.asyncio
async def test_conflicting_concurrent_corrections_revalidate_latest_row(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    instrument = await _seed(maker)
    validated, release = _hold_first_correction(monkeypatch)

    async def correct(**fields):
        async with maker() as session:
            patch = PatchEODRequest(correction_reason="vendor fix", **fields)
            try:
                await patch_eod_record(session, instrument.instrument_id, DAY, patch, "key")
                return "applied"
            except InvalidCorrectionError:
                await session.rollback()
                return "rejected"

    first = asyncio.create_task(correct(low=Decimal("95")))
    await validated.wait()
    second = asyncio.create_task(correct(close=Decimal("90")))
    await asyncio.sleep(0.3)
    # Without the row lock the second correction validated the stale row and committed.
    assert not second.done()
    release.set()
    assert await asyncio.gather(first, second) == ["applied", "rejected"]

    async with maker() as verify:
        eod = await verify.get(MarketDataEOD, (instrument.instrument_id, DAY))
        assert eod is not None
        assert (eod.low, eod.close) == (Decimal("95"), Decimal("100"))
        stats = await verify.get(InstrumentStats, instrument.instrument_id)
        assert stats is not None and stats.eod_latest_close == eod.close
        corrections = (await verify.scalars(select(CanonicalCorrection))).all()
        assert [(row.before_snapshot, row.after_snapshot) for row in corrections] == [
            ({"low": "80"}, {"low": "95"})
        ]


@pytest.mark.asyncio
async def test_normalizer_upsert_waits_for_an_open_correction(test_engine, monkeypatch):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    instrument = await _seed(maker)
    validated, release = _hold_first_correction(monkeypatch)

    async def correct():
        async with maker() as session:
            patch = PatchEODRequest(correction_reason="vendor fix", close=Decimal("110"))
            await patch_eod_record(session, instrument.instrument_id, DAY, patch, "key")

    async def ingest():
        # Same row-level ON CONFLICT DO UPDATE shape the EOD normalizer issues.
        async with maker() as session:
            values = {
                "instrument_id": instrument.instrument_id,
                "trade_date": DAY,
                "open": Decimal("101"),
                "high": Decimal("121"),
                "low": Decimal("81"),
                "close": Decimal("102"),
                "volume": 2000,
                "asof_ts": utc_now(),
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            stmt = insert(MarketDataEOD).values(**values)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["instrument_id", "trade_date"],
                    set_={key: stmt.excluded[key] for key in ("open", "high", "low", "close")},
                )
            )
            await session.commit()

    correction = asyncio.create_task(correct())
    await validated.wait()
    upsert = asyncio.create_task(ingest())
    await asyncio.sleep(0.3)
    assert not upsert.done()
    release.set()
    await asyncio.gather(correction, upsert)

    async with maker() as verify:
        eod = await verify.get(MarketDataEOD, (instrument.instrument_id, DAY))
        assert eod is not None
        # The later whole-row upsert wins as one consistent row, never a field mix.
        assert (eod.open, eod.high, eod.low, eod.close) == (
            Decimal("101"),
            Decimal("121"),
            Decimal("81"),
            Decimal("102"),
        )
