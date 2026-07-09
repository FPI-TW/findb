"""
Tests for Serve API endpoints.
"""

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.models.canonical import (
    CorporateAction,
    FuturesContinuousEOD,
    FuturesContract,
    Instrument,
    MacroObservation,
    MacroSeries,
    MarketDataEOD,
    RollRule,
    TradingCalendar,
)
from app.utils import utc_now, uuid7


@pytest.mark.asyncio
async def test_list_instruments_returns_data(client: AsyncClient, test_session):
    """Ensure instruments list returns seeded instruments."""
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
    stale_eod = MarketDataEOD(
        instrument_id=instrument_id,
        trade_date=date(2026, 1, 15),
        close=Decimal("95700.00"),
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    latest_eod = MarketDataEOD(
        instrument_id=instrument_id,
        trade_date=date(2026, 1, 16),
        close=Decimal("95709.01"),
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add_all([instrument, stale_eod, latest_eod])
    await test_session.commit()

    response = await client.get("/api/v1/serve/instruments?market=CRYPTO")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["data"]) == 1
    assert data["data"][0]["symbol"] == "BTC"
    assert data["data"][0]["latest_trade_date"] == "2026-01-16"
    assert data["data"][0]["latest_price"] == "95709.01000000"


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
    assert data["latest_trade_date"] is None
    assert data["latest_price"] is None


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
async def test_eod_endpoints_return_total_ticks(client: AsyncClient, test_session):
    """Ensure EOD responses include total_ticks when present."""
    instrument_id = uuid7()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="equity",
        market="TW",
        symbol="6160",
        name="欣技",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    eod = MarketDataEOD(
        instrument_id=instrument_id,
        trade_date=date(2024, 4, 29),
        open=Decimal("20.45"),
        high=Decimal("21.50"),
        low=Decimal("20.45"),
        close=Decimal("21.10"),
        volume=528,
        total_ticks=221,
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add_all([instrument, eod])
    await test_session.commit()

    list_response = await client.get(
        "/api/v1/serve/eod?market=TW&symbols=6160&start_date=2024-04-29&end_date=2024-04-29"
    )
    assert list_response.status_code == 200
    list_payload = list_response.json()["data"]
    assert len(list_payload) == 1
    assert list_payload[0]["total_ticks"] == 221

    detail_response = await client.get(
        f"/api/v1/serve/eod/{instrument_id}?start_date=2024-04-29&end_date=2024-04-29"
    )
    assert detail_response.status_code == 200
    detail_payload = detail_response.json()["data"]
    assert len(detail_payload) == 1
    assert detail_payload[0]["total_ticks"] == 221


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


@pytest.mark.asyncio
async def test_list_corporate_actions(client: AsyncClient, test_session):
    instrument_id = uuid7()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="equity",
        market="US",
        symbol="AAPL",
        name="Apple",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    action = CorporateAction(
        action_id=uuid7(),
        instrument_id=instrument_id,
        action_type="split",
        ex_date=date(2026, 1, 20),
        ratio=Decimal("2.0"),
        asof_ts=utc_now(),
        source="bloomberg",
    )
    test_session.add_all([instrument, action])
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/corporate-actions?market=US&symbols=AAPL&action_type=split"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["data"]) == 1
    assert payload["data"][0]["symbol"] == "AAPL"
    assert payload["data"][0]["action_type"] == "split"


@pytest.mark.asyncio
async def test_list_macro_series_and_observations(client: AsyncClient, test_session):
    series_id = uuid7()
    series = MacroSeries(
        series_id=series_id,
        name="US CPI",
        unit="index",
        frequency="monthly",
        market="US",
        source_code="CPI_US",
        source="fred",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    observation = MacroObservation(
        id=uuid7(),
        series_id=series_id,
        obs_date=date(2026, 1, 31),
        value=Decimal("302.1000"),
        source="fred",
        asof_ts=utc_now(),
    )
    test_session.add_all([series, observation])
    await test_session.commit()

    series_response = await client.get("/api/v1/serve/macro/series?market=US&source_code=CPI_US")
    assert series_response.status_code == 200
    series_payload = series_response.json()
    assert series_payload["success"] is True
    assert len(series_payload["data"]) == 1
    assert series_payload["data"][0]["source_code"] == "CPI_US"

    obs_response = await client.get("/api/v1/serve/macro/observations?market=US&source_code=CPI_US")
    assert obs_response.status_code == 200
    obs_payload = obs_response.json()
    assert obs_payload["success"] is True
    assert len(obs_payload["data"]) == 1
    assert obs_payload["data"][0]["series_name"] == "US CPI"


@pytest.mark.asyncio
async def test_list_futures_contracts_and_continuous(client: AsyncClient, test_session):
    instrument_id = uuid7()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="future",
        market="WTX",
        symbol="TX",
        name="Taiwan Index Future",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    roll_rule_id = uuid7()
    roll_rule = RollRule(
        rule_id=roll_rule_id,
        name="front-month",
        description="Front month roll",
        config={"type": "front_month"},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    contract = FuturesContract(
        contract_id=uuid7(),
        instrument_id=instrument_id,
        contract_code="TXF202602",
        contract_month="202602",
        expiry_date=date(2026, 2, 19),
        currency="TWD",
        source="taifex",
        asof_ts=utc_now(),
    )
    continuous = FuturesContinuousEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        roll_rule_id=roll_rule_id,
        trade_date=date(2026, 1, 16),
        open=Decimal("21000"),
        high=Decimal("21100"),
        low=Decimal("20900"),
        close=Decimal("21050"),
        volume=12000,
        turnover=Decimal("987654.1234"),
        source="taifex",
        asof_ts=utc_now(),
    )
    test_session.add_all([instrument, roll_rule, contract, continuous])
    await test_session.commit()

    contracts_response = await client.get("/api/v1/serve/futures/contracts?market=WTX&symbols=TX")
    assert contracts_response.status_code == 200
    contracts_payload = contracts_response.json()
    assert contracts_payload["success"] is True
    assert len(contracts_payload["data"]) == 1
    assert contracts_payload["data"][0]["contract_code"] == "TXF202602"

    continuous_response = await client.get(
        "/api/v1/serve/futures/continuous?market=WTX&symbols=TX&start_date=2026-01-16&end_date=2026-01-16"
    )
    assert continuous_response.status_code == 200
    continuous_payload = continuous_response.json()
    assert continuous_payload["success"] is True
    assert len(continuous_payload["data"]) == 1
    assert continuous_payload["data"][0]["roll_rule_name"] == "front-month"
