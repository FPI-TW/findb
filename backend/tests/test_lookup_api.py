"""Integration tests for the dedicated read-only lookup API."""

from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.models.canonical import Instrument, InstrumentStats, MacroSeries
from app.utils import utc_now


def _instrument(
    instrument_id: str,
    *,
    market: str,
    symbol: str,
    name: str | None,
    asset_class: str = "equity",
    currency: str | None = "USD",
    status: str = "active",
) -> Instrument:
    return Instrument(
        instrument_id=UUID(instrument_id),
        market=market,
        symbol=symbol,
        name=name,
        asset_class=asset_class,
        currency=currency,
        status=status,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def _macro_series(
    series_id: str,
    *,
    name: str,
    market: str | None,
    source_code: str | None,
    frequency: str | None,
    unit: str | None,
    source: str | None,
) -> MacroSeries:
    return MacroSeries(
        series_id=UUID(series_id),
        name=name,
        market=market,
        source_code=source_code,
        frequency=frequency,
        unit=unit,
        source=source,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


@pytest.mark.asyncio
async def test_instrument_lookup_returns_stats_nulls_and_global_facets(
    client: AsyncClient,
    test_session,
):
    apple = _instrument(
        "11111111-1111-1111-1111-111111111111",
        market="US",
        symbol="AAPL",
        name="Apple Inc.",
    )
    future = _instrument(
        "22222222-2222-2222-2222-222222222222",
        market="TW",
        symbol="TXF",
        name=None,
        asset_class="future",
        currency="TWD",
        status="inactive",
    )
    test_session.add_all(
        [
            apple,
            future,
            InstrumentStats(
                instrument_id=apple.instrument_id,
                first_trade_date=date(1980, 12, 12),
                latest_trade_date=date(2026, 7, 22),
                latest_price=Decimal("214.50000000"),
                updated_at=utc_now(),
            ),
        ]
    )
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/lookup/instruments",
        params={"market": " us ", "asset_class": " EQUITY ", "status": "ACTIVE"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["pagination"] == {
        "page": 1,
        "page_size": 50,
        "total_records": 1,
        "total_pages": 1,
        "next_cursor": None,
    }
    assert payload["data"] == [
        {
            "instrument_id": str(apple.instrument_id),
            "market": "US",
            "symbol": "AAPL",
            "name": "Apple Inc.",
            "asset_class": "equity",
            "currency": "USD",
            "status": "active",
            "first_trade_date": "1980-12-12",
            "latest_trade_date": "2026-07-22",
            "latest_price": "214.50000000",
        }
    ]
    assert payload["facets"] == {
        "markets": ["TW", "US"],
        "asset_classes": ["equity", "future"],
        "statuses": ["active", "inactive"],
    }

    null_response = await client.get(
        "/api/v1/serve/lookup/instruments",
        params={"market": "TW"},
    )
    null_item = null_response.json()["data"][0]
    assert null_item["name"] is None
    assert null_item["first_trade_date"] is None
    assert null_item["latest_trade_date"] is None
    assert null_item["latest_price"] is None


@pytest.mark.asyncio
async def test_instrument_lookup_normalizes_search_and_escapes_like_wildcards(
    client: AsyncClient,
    test_session,
):
    apple = _instrument(
        "11111111-1111-1111-1111-111111111111",
        market="US",
        symbol="AAPL",
        name="Apple Inc.",
    )
    literal = _instrument(
        "22222222-2222-2222-2222-222222222222",
        market="US",
        symbol="RATE_100",
        name=r"Yield 100%\Index",
    )
    ordinary = _instrument(
        "33333333-3333-3333-3333-333333333333",
        market="US",
        symbol="RATEA100",
        name="Yield Index",
    )
    test_session.add_all([apple, literal, ordinary])
    await test_session.commit()

    normalized = await client.get(
        "/api/v1/serve/lookup/instruments",
        params={"q": "  ＡＡＰＬ  "},
    )
    assert [item["symbol"] for item in normalized.json()["data"]] == ["AAPL"]

    uuid_search = await client.get(
        "/api/v1/serve/lookup/instruments",
        params={"q": "22222222-2222"},
    )
    assert [item["symbol"] for item in uuid_search.json()["data"]] == ["RATE_100"]

    for literal_query in ("_", "%", "\\"):
        response = await client.get(
            "/api/v1/serve/lookup/instruments",
            params={"q": literal_query},
        )
        assert response.status_code == 200
        assert [item["symbol"] for item in response.json()["data"]] == ["RATE_100"]


@pytest.mark.asyncio
async def test_instrument_lookup_sorts_nulls_ties_and_pages_deterministically(
    client: AsyncClient,
    test_session,
):
    first = _instrument(
        "11111111-1111-1111-1111-111111111111",
        market="US",
        symbol="FIRST",
        name="First",
    )
    second = _instrument(
        "22222222-2222-2222-2222-222222222222",
        market="US",
        symbol="SECOND",
        name="Second",
    )
    no_stats = _instrument(
        "33333333-3333-3333-3333-333333333333",
        market="US",
        symbol="NONE",
        name="No stats",
    )
    test_session.add_all(
        [
            first,
            second,
            no_stats,
            InstrumentStats(
                instrument_id=first.instrument_id,
                latest_price=Decimal("10"),
                updated_at=utc_now(),
            ),
            InstrumentStats(
                instrument_id=second.instrument_id,
                latest_price=Decimal("10"),
                updated_at=utc_now(),
            ),
        ]
    )
    await test_session.commit()

    first_page = await client.get(
        "/api/v1/serve/lookup/instruments",
        params={
            "sort_by": "latest_price",
            "sort_dir": "desc",
            "page": 1,
            "page_size": 2,
        },
    )
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert [item["symbol"] for item in first_payload["data"]] == ["FIRST", "SECOND"]
    assert first_payload["pagination"]["total_records"] == 3
    assert first_payload["pagination"]["total_pages"] == 2

    second_page = await client.get(
        "/api/v1/serve/lookup/instruments",
        params={
            "sort_by": "latest_price",
            "sort_dir": "desc",
            "page": 2,
            "page_size": 2,
        },
    )
    assert [item["symbol"] for item in second_page.json()["data"]] == ["NONE"]


@pytest.mark.asyncio
async def test_macro_lookup_search_filters_sorting_and_global_facets(
    client: AsyncClient,
    test_session,
):
    cpi = _macro_series(
        "11111111-1111-1111-1111-111111111111",
        name="Consumer Price Index",
        market="US",
        source_code="CPI_US",
        frequency="monthly",
        unit="index",
        source="fred",
    )
    gdp = _macro_series(
        "22222222-2222-2222-2222-222222222222",
        name="Gross Domestic Product",
        market="TW",
        source_code="GDP_TW",
        frequency="quarterly",
        unit=None,
        source="dgbas",
    )
    global_series = _macro_series(
        "33333333-3333-3333-3333-333333333333",
        name=r"Coverage 100%\Index",
        market=None,
        source_code=None,
        frequency=None,
        unit=None,
        source=None,
    )
    test_session.add_all([cpi, gdp, global_series])
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/lookup/macro-series",
        params={
            "q": "ＣＰＩ＿ＵＳ",
            "market": " us ",
            "frequency": " MONTHLY ",
            "source": " FRED ",
            "sort_by": "name",
            "sort_dir": "desc",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["source_code"] for item in payload["data"]] == ["CPI_US"]
    assert payload["facets"] == {
        "markets": ["TW", "US"],
        "frequencies": ["monthly", "quarterly"],
        "sources": ["dgbas", "fred"],
    }

    uuid_response = await client.get(
        "/api/v1/serve/lookup/macro-series",
        params={"q": "22222222-2222"},
    )
    assert [item["source_code"] for item in uuid_response.json()["data"]] == ["GDP_TW"]

    for literal_query in ("%", "\\"):
        literal_response = await client.get(
            "/api/v1/serve/lookup/macro-series",
            params={"q": literal_query},
        )
        assert [item["series_id"] for item in literal_response.json()["data"]] == [
            str(global_series.series_id)
        ]

    nulls_last = await client.get(
        "/api/v1/serve/lookup/macro-series",
        params={"sort_by": "source_code", "sort_dir": "desc"},
    )
    assert [item["source_code"] for item in nulls_last.json()["data"]] == [
        "GDP_TW",
        "CPI_US",
        None,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/serve/lookup/instruments?q=" + ("x" * 201),
        "/api/v1/serve/lookup/instruments?sort_by=created_at",
        "/api/v1/serve/lookup/instruments?sort_dir=sideways",
        "/api/v1/serve/lookup/instruments?page=0",
        "/api/v1/serve/lookup/instruments?page_size=201",
        "/api/v1/serve/lookup/macro-series?sort_by=series_id",
        "/api/v1/serve/lookup/macro-series?page_size=0",
    ],
)
async def test_lookup_query_validation_returns_422(
    client: AsyncClient,
    path: str,
):
    response = await client.get(path)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_lookup_uses_serve_api_auth_dependency(
    client: AsyncClient,
):
    settings = get_settings()
    original_require_auth = settings.SERVE_REQUIRE_AUTH
    original_keys = settings.SERVE_API_KEYS
    settings.SERVE_REQUIRE_AUTH = True
    settings.SERVE_API_KEYS = "lookup-test-key"

    try:
        missing = await client.get("/api/v1/serve/lookup/instruments")
        invalid = await client.get(
            "/api/v1/serve/lookup/instruments",
            headers={settings.API_KEY_HEADER: "wrong-key"},
        )
        valid = await client.get(
            "/api/v1/serve/lookup/instruments",
            headers={settings.API_KEY_HEADER: "lookup-test-key"},
        )
    finally:
        settings.SERVE_REQUIRE_AUTH = original_require_auth
        settings.SERVE_API_KEYS = original_keys

    assert missing.status_code == 401
    assert invalid.status_code == 403
    assert valid.status_code == 200
