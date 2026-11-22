"""The exchange quote corrects provider EOD rows field by field, in either write order."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.canonical import (
    MANUAL_CORRECTION_PRIORITY,
    EODFieldCorrection,
    Instrument,
    InstrumentStats,
    MarketDataEOD,
)
from app.models.registry import DatasetRegistry, DeliveryMemberEvidence, DQIssue
from app.schemas.admin import PatchEODRequest
from app.services.admin import patch_eod_record
from app.services.ingestion import IngestionService
from app.services.normalize.contracts import MarketEODV2ContractNormalizer
from tests.intraday_support import Stores
from tests.test_exchange_daily_quote import (
    DAY,
    FETCHED,
    deliver,
    quote,
    seed_dataset,
    stub_stores,
)

PROVIDER_DATASET = "hk_equity_eod"
PROVIDER_FETCHED = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)
QUOTED = {
    "close": Decimal("344.00"),
    "high": Decimal("349.60"),
    "low": Decimal("339.60"),
    "volume": 41094469,
    "turnover": Decimal("14089992865"),
}


def provider_config() -> dict:
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1, 2],
        "current_schema_version": 2,
        "schema_enforcement": "enforce",
        "allowed_sources": ["factset"],
        "defaults": {"market": "HK", "asset_class": "equity", "currency": "HKD"},
    }


def provider_row(**values) -> dict:
    return {
        "symbol": "00700",
        "source_symbol": "J3TRKQ-L",
        "trade_date": DAY.isoformat(),
        "currency": "HKD",
        "open": "350.00",
        "high": "349.60",
        "low": "339.60",
        "close": "343.80",
        "volume": 41094469,
        "turnover": "14089992865",
        **values,
    }


@pytest.fixture
def stores(monkeypatch) -> Stores:
    return stub_stores(monkeypatch)


@pytest.fixture
async def provider_dataset(test_session) -> None:
    await seed_dataset(test_session)
    test_session.add(
        DatasetRegistry(
            dataset_key=PROVIDER_DATASET,
            name="HK Equity EOD",
            asset_class="equity",
            market="HK",
            frequency="daily",
            is_active=True,
            config=provider_config(),
        )
    )
    await test_session.commit()


async def write_provider(
    session: AsyncSession, row: dict | None = None, *, fetched_at: datetime = PROVIDER_FETCHED
) -> UUID:
    """Normalize one FactSet-shaped delivery of a single member."""
    run = await IngestionService(session).create_ingestion_run(
        PROVIDER_DATASET,
        source="factset",
        raw_records=1,
        schema_id="market_eod",
        schema_version=2,
        batch_data_date=DAY,
        delivery_mode="incremental",
    )
    await session.commit()
    run_id = run.run_id
    normalizer = MarketEODV2ContractNormalizer(
        session,
        {**provider_config(), "_ingest_source": "factset", "_ingest_fetched_at": fetched_at},
    )
    payload = {
        "batch": {
            "data_date": DAY.isoformat(),
            "delivery_mode": "incremental",
            "declared_record_count": 1,
        },
        "data": [row or provider_row()],
    }
    result = await normalizer.process(payload, run_id)
    assert result.success_records == 1, result.dq_issues
    return run_id


async def _row(session: AsyncSession) -> MarketDataEOD:
    session.expire_all()
    return await session.scalar(select(MarketDataEOD).where(MarketDataEOD.trade_date == DAY))


async def _corrections(session: AsyncSession) -> list[EODFieldCorrection]:
    return list(
        await session.scalars(select(EODFieldCorrection).order_by(EODFieldCorrection.created_at))
    )


async def _differences(session: AsyncSession, run_id: UUID) -> list[DQIssue]:
    return list(
        await session.scalars(
            select(DQIssue).where(
                DQIssue.run_id == run_id, DQIssue.issue_type == "EOD_EXCHANGE_QUOTE_DIFFERENCE"
            )
        )
    )


def _values(row: MarketDataEOD) -> dict:
    return {field: getattr(row, field) for field in ("open", *QUOTED)}


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_first", [False, True])
async def test_either_write_order_leaves_the_same_corrected_row(
    test_session, stores, provider_dataset, quote_first
):
    if quote_first:
        quote_run, _ = await deliver(test_session, stores)
        assert await _corrections(test_session) == []
        provider_run = await write_provider(test_session)
        applying_run = provider_run
    else:
        provider_run = await write_provider(test_session)
        quote_run, _ = await deliver(test_session, stores)
        applying_run = quote_run

    row = await _row(test_session)
    # The open stays FactSet's; the row stays FactSet's for priority and evidence.
    assert _values(row) == {"open": Decimal("350.00"), **QUOTED}
    assert (row.source, row.run_id) == ("factset", provider_run)
    assert row.field_sources == dict.fromkeys(QUOTED, "hkex")
    (correction,) = await _corrections(test_session)
    assert (correction.before, correction.after) == ({"close": "343.8"}, {"close": "344"})
    assert (correction.source, correction.replaced_source) == ("hkex", "factset")
    assert correction.run_id == applying_run
    (difference,) = await _differences(test_session, applying_run)
    assert difference.severity == "warning"
    assert difference.instrument_id == row.instrument_id
    stats = await test_session.get(InstrumentStats, row.instrument_id)
    assert stats.eod_latest_close == Decimal("344.00")
    evidence = set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(
                DeliveryMemberEvidence.run_id == provider_run
            )
        )
    )
    assert evidence == {"00700"}
    assert quote_run != provider_run


@pytest.mark.asyncio
async def test_a_provider_rewrite_is_corrected_again(test_session, stores, provider_dataset):
    await write_provider(test_session)
    await deliver(test_session, stores)

    rerun = await write_provider(test_session, fetched_at=PROVIDER_FETCHED + timedelta(hours=1))

    row = await _row(test_session)
    assert (_values(row), row.run_id) == ({"open": Decimal("350.00"), **QUOTED}, rerun)
    assert row.field_sources == dict.fromkeys(QUOTED, "hkex")
    assert [c.run_id for c in await _corrections(test_session)][-1] == rerun
    assert len(await _differences(test_session, rerun)) == 1


@pytest.mark.asyncio
async def test_matching_values_only_record_their_source(test_session, stores, provider_dataset):
    await write_provider(test_session, provider_row(close="344.00"))

    quote_run, _ = await deliver(test_session, stores)

    row = await _row(test_session)
    assert row.field_sources == dict.fromkeys(QUOTED, "hkex")
    assert await _corrections(test_session) == []
    assert await _differences(test_session, quote_run) == []
    updated = row.updated_at
    # A repeated quote with nothing new to say leaves the row alone.
    await deliver(test_session, stores, fetched_at=FETCHED + timedelta(minutes=5))
    assert (await _row(test_session)).updated_at == updated


@pytest.mark.asyncio
@pytest.mark.parametrize(("volume", "reported"), [(41300000, False), (42000000, True)])
async def test_only_a_volume_off_by_more_than_one_percent_is_reported(
    test_session, stores, provider_dataset, volume, reported
):
    await write_provider(test_session, provider_row(close="344.00", volume=volume))

    quote_run, _ = await deliver(test_session, stores)

    (correction,) = await _corrections(test_session)
    assert correction.after == {"volume": "41094469"}
    assert len(await _differences(test_session, quote_run)) == int(reported)


@pytest.mark.asyncio
async def test_a_field_the_quote_leaves_empty_keeps_the_provider_value(
    test_session, stores, provider_dataset
):
    # A day without a board-lot trade: the exchange prints no high/low.
    await write_provider(test_session, provider_row(high="345.00", low="343.00"))

    await deliver(test_session, stores, [quote(high=None, low=None, volume=400, turnover="137600")])

    row = await _row(test_session)
    assert (row.high, row.low, row.volume) == (Decimal("345.00"), Decimal("343.00"), 400)
    assert row.field_sources == dict.fromkeys(("close", "volume", "turnover"), "hkex")


@pytest.mark.asyncio
async def test_a_manual_correction_is_never_replaced(test_session, stores, provider_dataset):
    await write_provider(test_session)
    row = await _row(test_session)
    await patch_eod_record(
        test_session,
        row.instrument_id,
        DAY,
        PatchEODRequest(close=Decimal("343.90"), correction_reason="checked by hand"),
        "admin-key",
    )

    quote_run, _ = await deliver(test_session, stores)

    row = await _row(test_session)
    assert row.source_priority == MANUAL_CORRECTION_PRIORITY
    assert (row.close, row.field_sources) == (Decimal("343.90"), {"close": "manual"})
    assert await _corrections(test_session) == []
    assert await _differences(test_session, quote_run) == []


@pytest.mark.asyncio
async def test_a_provider_write_waits_for_an_uncommitted_quote(
    test_engine, test_session, stores, provider_dataset
):
    """Without the per-date lock each write would miss the other's uncommitted row."""
    # Both writers find the instrument; only the slot rows race.
    await write_provider(
        test_session,
        provider_row(trade_date=(DAY - timedelta(days=1)).isoformat()),
    )
    instrument = await test_session.scalar(select(Instrument).where(Instrument.symbol == "00700"))
    assert instrument is not None

    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as quote_session, sessions() as provider_session:
        # The quote is written and locked, but not committed.
        await deliver(quote_session, stores, commit=False)
        provider = asyncio.create_task(write_provider(provider_session))
        done, _ = await asyncio.wait({provider}, timeout=0.5)
        assert not done
        await quote_session.commit()
        provider_run = await asyncio.wait_for(provider, 5)

    row = await _row(test_session)
    assert _values(row) == {"open": Decimal("350.00"), **QUOTED}
    (correction,) = await _corrections(test_session)
    assert correction.run_id == provider_run
