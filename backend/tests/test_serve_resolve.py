"""Exact instrument resolution and relevance-ranked search."""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.models.canonical import InstrumentStats
from app.utils import utc_now
from tests.test_serve_api import assert_error, dataset, instrument

TSMC = UUID("11111111-1111-1111-1111-111111111111")
TSMC_LOOKALIKE = UUID("22222222-2222-2222-2222-222222222222")
AAPL = UUID("33333333-3333-3333-3333-333333333333")
TENCENT = UUID("44444444-4444-4444-4444-444444444444")
SHARED_EQUITY = UUID("55555555-5555-5555-5555-555555555555")
SHARED_ETF = UUID("66666666-6666-6666-6666-666666666666")
UNSERVED = UUID("77777777-7777-7777-7777-777777777777")
NAMED = UUID("88888888-8888-8888-8888-888888888888")

URL = "/api/v1/serve/instruments/resolve"


@pytest.fixture
async def catalog(test_session) -> None:
    test_session.add_all(
        [
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            dataset("tw_etf_eod", market="TW", asset_class="etf", schema_id="market_eod"),
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            dataset("hk_equity_eod", market="HK", asset_class="equity", schema_id="market_eod"),
            instrument(TSMC, "2330", name="台積電"),
            instrument(TSMC_LOOKALIKE, "23301", name="Another"),
            instrument(NAMED, "9999", name="2330 Holdings"),
            instrument(AAPL, "AAPL", market="US", name="Apple Inc."),
            instrument(TENCENT, "00700", market="HK", name="Tencent"),
            instrument(SHARED_EQUITY, "SAME", name="Equity twin"),
            instrument(SHARED_ETF, "SAME", asset_class="etf", name="ETF twin"),
            instrument(UNSERVED, "IDX", market="TW", asset_class="index"),
            InstrumentStats(
                instrument_id=TSMC,
                eod_first_date=date(2020, 1, 2),
                eod_latest_date=date(2026, 11, 20),
                eod_latest_close=Decimal("1300"),
                updated_at=utc_now(),
            ),
        ]
    )
    await test_session.commit()


@pytest.mark.asyncio
async def test_resolve_returns_one_entry_per_input_without_failing_on_misses(
    client: AsyncClient, catalog
):
    response = await client.get(
        URL, params={"symbols": "tw:2330, US:aapl,HK:700,TW:SAME,TW:IDX,TW:NOPE,TW:2330"}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert [(item["input"], item["market"], item["symbol"], item["status"]) for item in data] == [
        ("tw:2330", "TW", "2330", "resolved"),
        ("US:aapl", "US", "AAPL", "resolved"),
        ("HK:700", "HK", "00700", "resolved"),
        ("TW:SAME", "TW", "SAME", "ambiguous"),
        ("TW:IDX", "TW", "IDX", "not_available"),
        ("TW:NOPE", "TW", "NOPE", "not_found"),
    ]
    tsmc, aapl, tencent, same, unserved, missing = data
    assert [match["instrument_id"] for match in tsmc["matches"]] == [str(TSMC)]
    assert tsmc["matches"][0]["coverage"]["eod"]["latest_date"] == "2026-11-20"
    assert aapl["matches"][0]["instrument_id"] == str(AAPL)
    assert tencent["matches"][0]["instrument_id"] == str(TENCENT)
    assert {match["asset_class"] for match in same["matches"]} == {"equity", "etf"}
    assert unserved["matches"] == [] and missing["matches"] == []


@pytest.mark.asyncio
async def test_resolve_uses_market_for_unqualified_symbols(client: AsyncClient, catalog):
    response = await client.get(URL, params={"symbols": "2330,US:AAPL", "market": "TW"})

    assert [item["status"] for item in response.json()["data"]] == ["resolved", "resolved"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbols", ["2330", "TW:2330,,TW:2317", ",".join(f"TW:{n}" for n in range(51)), "TW:"]
)
async def test_resolve_rejects_malformed_symbol_lists(client: AsyncClient, catalog, symbols):
    response = await client.get(URL, params={"symbols": symbols})

    assert_error(response, 422, "INVALID_SYMBOLS")


@pytest.mark.asyncio
async def test_search_ranks_exact_symbol_before_prefix_and_name_matches(
    client: AsyncClient, catalog
):
    response = await client.get("/api/v1/serve/instruments", params={"q": "2330"})

    assert response.status_code == 200
    assert [item["symbol"] for item in response.json()["data"]] == ["2330", "23301", "9999"]


@pytest.mark.asyncio
async def test_search_without_query_keeps_market_order_and_explicit_sort_wins(
    client: AsyncClient, catalog
):
    default = await client.get("/api/v1/serve/instruments")
    markets = [item["market"] for item in default.json()["data"]]
    assert markets == sorted(markets)

    explicit = await client.get(
        "/api/v1/serve/instruments", params={"q": "2330", "sort_by": "symbol", "sort_dir": "desc"}
    )
    assert [item["symbol"] for item in explicit.json()["data"]] == ["9999", "23301", "2330"]
