"""An Admin EOD correction outranks every later ingest of the same row."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.canonical import (
    MANUAL_CORRECTION_PRIORITY,
    Instrument,
    InstrumentStats,
    MarketDataEOD,
)
from app.schemas.admin import PatchEODRequest
from app.services.admin import patch_eod_record
from app.services.normalize.base import BaseNormalizer
from app.services.normalize.types import MappedRecord
from app.utils import utc_now, uuid7

DAY = date(2025, 1, 2)
FETCHED = datetime(2025, 1, 2, 22, tzinfo=timezone.utc)


class _Normalizer(BaseNormalizer):
    def map_fields(self, raw_data: dict) -> list:
        return []


@pytest.mark.asyncio
async def test_corrected_row_is_not_overwritten_by_later_ingest(test_session):
    instrument = Instrument(
        instrument_id=uuid7(),
        asset_class="equity",
        market="HK",
        symbol="0005",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(instrument)
    await test_session.flush()
    instrument_id = instrument.instrument_id
    test_session.add(
        MarketDataEOD(
            instrument_id=instrument_id,
            trade_date=DAY,
            open=Decimal("100"),
            high=Decimal("120"),
            low=Decimal("80"),
            close=Decimal("100"),
            volume=1000,
            source="twelve_data",
            source_priority=0,
            source_fetched_at=FETCHED,
            asof_ts=utc_now(),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
    )
    await test_session.commit()

    patch = PatchEODRequest(correction_reason="vendor fix", close=Decimal("110"))
    _, eod = await patch_eod_record(test_session, instrument_id, DAY, patch, "key")
    assert (eod.source, eod.source_priority) == ("twelve_data", MANUAL_CORRECTION_PRIORITY)

    normalizer = _Normalizer(
        test_session,
        {
            "source_precedence": {"factset": -1, "twelve_data": 0},
            "_ingest_fetched_at": FETCHED + timedelta(days=1),
        },
    )
    normalizer._eod_partition_cache.add(DAY.year)
    for source in ("factset", "twelve_data"):
        # Both a higher-precedence provider and a newer same-provider fetch would win
        # against an uncorrected row.
        record = MappedRecord(
            symbol="0005",
            trade_date=datetime(2025, 1, 2),
            open=Decimal("1"),
            high=Decimal("300"),
            low=Decimal("1"),
            close=Decimal("200"),
            volume=1,
            source=source,
        )
        assert not await normalizer.upsert_eod(instrument_id, record, uuid7())

    eod = await test_session.get(MarketDataEOD, (instrument_id, DAY), populate_existing=True)
    assert eod is not None
    assert (eod.close, eod.source_priority) == (Decimal("110"), MANUAL_CORRECTION_PRIORITY)
    stats = await test_session.get(InstrumentStats, instrument_id, populate_existing=True)
    assert stats is not None and stats.eod_latest_close == Decimal("110")
