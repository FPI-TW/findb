"""Index instruments take FinDB's display name on write; a name is filled, never replaced."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import Instrument
from app.models.registry import DatasetRegistry
from app.services.index_names import INDEX_NAMES, index_name
from app.services.ingestion import IngestionService
from app.services.normalize.contracts import MarketEODV2ContractNormalizer

DATASET = "us_index_eod"
DAY = date(2026, 11, 23)
FETCHED = datetime(2026, 11, 24, 0, 30, tzinfo=timezone.utc)


def _config() -> dict:
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [2],
        "current_schema_version": 2,
        "schema_enforcement": "enforce",
        "allowed_sources": ["fmp"],
        "defaults": {"market": "US", "asset_class": "index", "currency": "USD"},
    }


def _row(symbol: str, **values) -> dict:
    return {
        "symbol": symbol,
        # FMP rows carry the provider symbol, so a known index resolves by identifier.
        "source_symbol": symbol,
        "trade_date": DAY.isoformat(),
        "currency": "USD",
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": 0,
        **values,
    }


@pytest.fixture
async def index_dataset(test_session) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key=DATASET,
            name="US Index EOD",
            asset_class="index",
            market="US",
            frequency="daily",
            is_active=True,
            config=_config(),
        )
    )
    await test_session.commit()


async def _write(session: AsyncSession, *rows: dict) -> UUID:
    run = await IngestionService(session).create_ingestion_run(
        DATASET,
        source="fmp",
        raw_records=len(rows),
        schema_id="market_eod",
        schema_version=2,
        batch_data_date=DAY,
        delivery_mode="incremental",
    )
    await session.commit()
    normalizer = MarketEODV2ContractNormalizer(
        session, {**_config(), "_ingest_source": "fmp", "_ingest_fetched_at": FETCHED}
    )
    payload = {
        "batch": {
            "data_date": DAY.isoformat(),
            "delivery_mode": "incremental",
            "declared_record_count": len(rows),
        },
        "data": list(rows),
    }
    result = await normalizer.process(payload, run.run_id)
    assert result.success_records == len(rows), result.dq_issues
    return run.run_id


async def _names(session: AsyncSession) -> dict[str, str | None]:
    session.expire_all()
    rows = await session.execute(
        select(Instrument.symbol, Instrument.name).where(Instrument.asset_class == "index")
    )
    return dict(rows.all())


def test_every_fmp_index_has_a_display_name():
    # The Fetcher's lists are the source of every index symbol FinDB writes.
    from findb_fetcher.provider_adapters.fmp import INDEX_UNIVERSES

    markets = {"hk_index_eod": "HK", "us_index_eod": "US", "us_dollar_index_eod": "US"}
    unnamed = [
        (markets[dataset], symbol)
        for dataset, group in INDEX_UNIVERSES.items()
        for symbol in group
        if (markets[dataset], symbol) not in INDEX_NAMES
    ]
    assert unnamed == []
    assert index_name("us", " ^gspc ") == "S&P 500 指數"


async def test_a_new_index_instrument_takes_its_display_name(test_session, index_dataset):
    await _write(test_session, _row("^GSPC"), _row("DX-Y.NYB"), _row("^UNLISTED"))
    assert await _names(test_session) == {
        "^GSPC": "S&P 500 指數",
        "DX-Y.NYB": "美元指數",
        "^UNLISTED": None,
    }


async def test_an_index_written_before_it_was_named_is_named_on_the_next_write(
    test_session, index_dataset
):
    # An environment that already holds the instrument with no name (and its identifier).
    await _write(test_session, _row("^GSPC", name=None), _row("^NDX"))
    await test_session.execute(
        Instrument.__table__.update()
        .where(Instrument.symbol.in_(["^GSPC", "^NDX"]))
        .values(name=None)
    )
    await test_session.execute(
        Instrument.__table__.update().where(Instrument.symbol == "^NDX").values(name="自訂名稱")
    )
    await test_session.commit()

    await _write(test_session, _row("^GSPC"), _row("^NDX"))

    # The empty name is filled; a name already set (for example by hand) is kept.
    assert await _names(test_session) == {"^GSPC": "S&P 500 指數", "^NDX": "自訂名稱"}
