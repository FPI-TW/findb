"""
Tests for Serve API endpoints.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.models.canonical import (
    BondDetails,
    BondEOD,
    CalendarMarket,
    CalendarRevisionDay,
    CalendarYearRevision,
    CorporateAction,
    FuturesContinuousEOD,
    FuturesContract,
    Instrument,
    InstrumentStats,
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
    stats = InstrumentStats(
        instrument_id=instrument_id,
        first_trade_date=date(2026, 1, 15),
        latest_trade_date=date(2026, 1, 16),
        latest_price=Decimal("95709.01"),
        updated_at=utc_now(),
    )
    test_session.add_all([instrument, stale_eod, latest_eod, stats])
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
async def test_list_instruments_supports_keyset_without_count(
    client: AsyncClient,
    test_session,
):
    """Cursor pagination can skip count for deep instrument scans."""
    instruments = [
        Instrument(
            instrument_id=uuid7(),
            asset_class="equity",
            market="US",
            symbol=symbol,
            name=symbol,
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        for symbol in ("AAA", "BBB", "CCC")
    ]
    test_session.add_all(instruments)
    await test_session.commit()

    first_page = await client.get(
        "/api/v1/serve/instruments?market=US&page_size=2&include_count=false"
    )
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert [item["symbol"] for item in first_payload["data"]] == ["AAA", "BBB"]
    assert first_payload["pagination"]["total_records"] is None
    assert first_payload["pagination"]["total_pages"] is None
    assert first_payload["pagination"]["next_cursor"].startswith("BBB|")

    second_page = await client.get(
        "/api/v1/serve/instruments?market=US&page_size=2&include_count=false"
        f"&cursor={first_payload['pagination']['next_cursor']}"
    )
    assert second_page.status_code == 200
    second_payload = second_page.json()
    assert [item["symbol"] for item in second_payload["data"]] == ["CCC"]
    assert second_payload["pagination"]["next_cursor"] is None


@pytest.mark.asyncio
async def test_list_instruments_cursor_handles_duplicate_symbols(client: AsyncClient, test_session):
    instruments = [
        Instrument(
            instrument_id=UUID("11111111-1111-1111-1111-111111111111"),
            asset_class="equity",
            market="US",
            symbol="DUP",
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        ),
        Instrument(
            instrument_id=UUID("22222222-2222-2222-2222-222222222222"),
            asset_class="equity",
            market="HK",
            symbol="DUP",
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        ),
        Instrument(
            instrument_id=UUID("33333333-3333-3333-3333-333333333333"),
            asset_class="equity",
            market="US",
            symbol="ZZZ",
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        ),
    ]
    test_session.add_all(instruments)
    await test_session.commit()

    first_page = await client.get("/api/v1/serve/instruments?page_size=1")
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert first_payload["data"][0]["symbol"] == "DUP"
    assert first_payload["pagination"]["total_records"] == 3

    second_page = await client.get(
        f"/api/v1/serve/instruments?page_size=1&cursor={first_payload['pagination']['next_cursor']}"
    )
    assert second_page.status_code == 200
    second_payload = second_page.json()
    assert second_payload["data"][0]["symbol"] == "DUP"
    assert second_payload["pagination"]["total_records"] == 3


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
    """Only complete published managed days are exposed to Serve clients."""
    market = CalendarMarket(market="CRYPTO", display_name="Crypto", timezone="UTC", weekend_days=[])
    revision = CalendarYearRevision(
        market="CRYPTO",
        year=2026,
        revision=1,
        status="published",
        expected_days=365,
        actual_days=365,
        timezone="UTC",
        source_kind="test",
    )
    test_session.add_all([market, revision])
    await test_session.flush()
    test_session.add_all(
        [
            CalendarRevisionDay(
                calendar_revision_id=revision.id,
                trade_date=date(2026, 1, 1) + timedelta(days=offset),
                is_open=(date(2026, 1, 1) + timedelta(days=offset)).weekday() < 5,
                day_status=(
                    "open"
                    if (date(2026, 1, 1) + timedelta(days=offset)).weekday() < 5
                    else "closed"
                ),
                source_kind="test",
            )
            for offset in range(365)
        ]
        + [
            TradingCalendar(
                id=uuid7(),
                market="CRYPTO",
                trade_date=date(2026, 1, 16),
                is_open=False,
                day_status="closed",
            ),
        ]
    )
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/calendar?market=CRYPTO&start_date=2026-01-16&end_date=2026-01-16"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert len(payload["data"]) == 1
    assert payload["data"][0]["trade_date"] == "2026-01-16"
    assert payload["data"][0]["is_open"] is True

    open_response = await client.get(
        "/api/v1/serve/calendar?market=CRYPTO&start_date=2026-01-15&end_date=2026-01-17&is_open=true"
    )
    assert open_response.status_code == 200
    assert [row["trade_date"] for row in open_response.json()["data"]] == [
        "2026-01-15",
        "2026-01-16",
    ]
    first_page = await client.get("/api/v1/serve/calendar?market=CRYPTO&page=1&page_size=2")
    second_page = await client.get("/api/v1/serve/calendar?market=CRYPTO&page=2&page_size=2")
    assert first_page.status_code == second_page.status_code == 200
    assert first_page.json()["pagination"] == {
        "page": 1,
        "page_size": 2,
        "total_records": 365,
        "total_pages": 183,
        "next_cursor": None,
    }
    assert [row["trade_date"] for row in first_page.json()["data"]] == [
        "2026-01-01",
        "2026-01-02",
    ]
    assert [row["trade_date"] for row in second_page.json()["data"]] == [
        "2026-01-03",
        "2026-01-04",
    ]

    test_session.add_all(
        [
            CalendarMarket(
                market="WTX", display_name="WTX", timezone="Asia/Taipei", weekend_days=[5, 6]
            ),
            CalendarYearRevision(
                market="WTX",
                year=2026,
                revision=1,
                status="draft",
                expected_days=365,
                actual_days=1,
                timezone="Asia/Taipei",
                source_kind="test",
            ),
            TradingCalendar(id=uuid7(), market="WTX", trade_date=date(2026, 1, 16), is_open=True),
        ]
    )
    await test_session.commit()
    wtx_response = await client.get(
        "/api/v1/serve/calendar?market=WTX&start_date=2026-01-16&end_date=2026-01-16"
    )
    assert wtx_response.status_code == 200
    assert wtx_response.json()["data"] == []

    test_session.add_all(
        [
            CalendarMarket(
                market="US", display_name="US", timezone="America/New_York", weekend_days=[5, 6]
            ),
            CalendarYearRevision(
                market="US",
                year=2026,
                revision=1,
                status="published",
                expected_days=365,
                actual_days=364,
                timezone="America/New_York",
                source_kind="test",
            ),
        ]
    )
    await test_session.commit()
    incomplete_response = await client.get(
        "/api/v1/serve/calendar?market=US&start_date=2026-01-16&end_date=2026-01-16"
    )
    assert incomplete_response.status_code == 200
    assert incomplete_response.json()["pagination"]["total_records"] == 0
    assert incomplete_response.json()["data"] == []


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
        open_interest=23000,
        active_contract_code="TXF202602",
        roll_adjustment=Decimal("1.5"),
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
    assert continuous_payload["data"][0]["open_interest"] == 23000
    assert continuous_payload["data"][0]["active_contract_code"] == "TXF202602"
    assert continuous_payload["data"][0]["roll_adjustment"] == "1.5"

    detail_response = await client.get(f"/api/v1/serve/futures/continuous/{instrument_id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()["data"][0]
    assert detail["open_interest"] == 23000
    assert detail["active_contract_code"] == "TXF202602"
    assert detail["roll_adjustment"] == "1.5"


@pytest.mark.asyncio
async def test_list_bonds_and_bond_eod(client: AsyncClient, test_session):
    instrument_id = uuid7()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="bond",
        market="US",
        symbol="US10Y-2026",
        name="US Treasury 10Y",
        currency="USD",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    details = BondDetails(
        instrument_id=instrument_id,
        issuer="US Treasury",
        coupon=Decimal("0.045"),
        maturity_date=date(2036, 2, 15),
        rating="AA+",
        face_value=Decimal("1000"),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    eod = BondEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=date(2026, 1, 16),
        yield_to_maturity=Decimal("0.0412"),
        clean_price=Decimal("99.25"),
        dirty_price=Decimal("99.40"),
        duration=Decimal("8.1"),
        source="bloomberg",
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add_all([instrument, details, eod])
    await test_session.commit()

    bonds_response = await client.get("/api/v1/serve/bonds?market=US&issuer=Treasury")
    assert bonds_response.status_code == 200
    bonds_payload = bonds_response.json()
    assert bonds_payload["success"] is True
    assert bonds_payload["data"][0]["symbol"] == "US10Y-2026"
    assert bonds_payload["data"][0]["issuer"] == "US Treasury"
    assert bonds_payload["data"][0]["maturity_date"] == "2036-02-15"

    eod_response = await client.get(
        "/api/v1/serve/bonds/eod?market=US&symbols=US10Y-2026&start_date=2026-01-16"
    )
    assert eod_response.status_code == 200
    eod_payload = eod_response.json()
    assert eod_payload["success"] is True
    assert len(eod_payload["data"]) == 1
    assert eod_payload["data"][0]["yield_to_maturity"] == "0.0412"
    assert eod_payload["data"][0]["clean_price"] == "99.25"
