"""
Tests for Serve API endpoints.
"""
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.models.canonical import Instrument, MarketDataEOD, TradingCalendar
from app.utils import utc_now, uuid7


@pytest.mark.asyncio
async def test_list_instruments_returns_data(client: AsyncClient, test_session):
    """Ensure instruments list returns seeded instruments."""
    instrument = Instrument(
        instrument_id=uuid7(),
        asset_class="crypto",
        market="CRYPTO",
        symbol="BTC",
        name="Bitcoin",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(instrument)
    await test_session.commit()

    response = await client.get("/api/v1/serve/instruments?market=CRYPTO")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["data"]) == 1
    assert data["data"][0]["symbol"] == "BTC"


@pytest.mark.asyncio
async def test_get_instrument_by_id(client: AsyncClient, test_session):
    """Ensure instrument detail endpoint works."""
    instrument_id = uuid7()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="crypto",
        market="CRYPTO",
        symbol="ETH",
        name="Ethereum",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(instrument)
    await test_session.commit()

    response = await client.get(f"/api/v1/serve/instruments/{instrument_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == "ETH"


@pytest.mark.asyncio
async def test_list_eod_filters(client: AsyncClient, test_session):
    """Ensure EOD endpoint filters by market/symbol/date range."""
    instrument_id = uuid7()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="crypto",
        market="CRYPTO",
        symbol="BTC",
        name="Bitcoin",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(instrument)

    eod_one = MarketDataEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=date(2026, 1, 15),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    eod_two = MarketDataEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=date(2026, 1, 16),
        open=Decimal("101"),
        high=Decimal("111"),
        low=Decimal("91"),
        close=Decimal("106"),
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add_all([eod_one, eod_two])
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/eod?market=CRYPTO&symbols=BTC&start_date=2026-01-16&end_date=2026-01-16"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["data"]) == 1
    assert payload["data"][0]["trade_date"] == "2026-01-16"


@pytest.mark.asyncio
async def test_get_instrument_eod(client: AsyncClient, test_session):
    """Ensure instrument-specific EOD endpoint works."""
    instrument_id = uuid7()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="crypto",
        market="CRYPTO",
        symbol="SOL",
        name="Solana",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(instrument)

    eod = MarketDataEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=date(2026, 1, 16),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(eod)
    await test_session.commit()

    response = await client.get(
        f"/api/v1/serve/eod/{instrument_id}?start_date=2026-01-16&end_date=2026-01-16"
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["data"]) == 1
    assert payload["data"][0]["symbol"] == "SOL"


@pytest.mark.asyncio
async def test_list_calendar(client: AsyncClient, test_session):
    """Ensure calendar endpoint returns trading days."""
    calendar = TradingCalendar(
        id=uuid7(),
        market="CRYPTO",
        trade_date=date(2026, 1, 16),
        is_open=True,
    )
    test_session.add(calendar)
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/calendar?market=CRYPTO&start_date=2026-01-16&end_date=2026-01-16"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["data"]) == 1
    assert payload["data"][0]["trade_date"] == "2026-01-16"
