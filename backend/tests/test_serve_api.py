"""Contract tests for the current canonical Serve API."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from app.config import get_settings
from app.models.canonical import Instrument, InstrumentStats, MarketDataEOD, MarketDataMinute
from app.models.registry import DatasetRegistry
from app.services import serve as serve_service
from app.services.api_keys import create_api_key
from app.utils import utc_now, uuid7


def dataset(
    key: str,
    *,
    market: str,
    asset_class: str,
    schema_id: str,
    active: bool = True,
) -> DatasetRegistry:
    minute = schema_id == "market_minute"
    defaults = {"market": market, "asset_class": asset_class, "currency": "TWD"}
    if minute:
        defaults.update({"market_timezone": "Asia/Taipei", "price_adjustment": "none"})
    return DatasetRegistry(
        dataset_key=key,
        name=key,
        asset_class=asset_class,
        market=market,
        frequency="minute" if minute else "daily",
        is_active=active,
        config={
            "schema_id": schema_id,
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "enforce",
            "allowed_sources": ["secret_provider"],
            "defaults": defaults,
        },
    )


def instrument(
    instrument_id: UUID,
    symbol: str,
    *,
    market: str = "TW",
    asset_class: str = "equity",
    name: str | None = None,
) -> Instrument:
    return Instrument(
        instrument_id=instrument_id,
        market=market,
        asset_class=asset_class,
        symbol=symbol,
        name=name,
        currency="TWD" if market == "TW" else "USD",
        timezone="Asia/Taipei" if market == "TW" else "America/New_York",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def eod(instrument_id: UUID, trade_date: date, close: str = "100") -> MarketDataEOD:
    now = utc_now()
    return MarketDataEOD(
        instrument_id=instrument_id,
        trade_date=trade_date,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
        source="secret_provider",
        source_fetched_at=now,
        asof_ts=now,
        created_at=now,
        updated_at=now,
    )


def minute(instrument_id: UUID, start: datetime, close: str) -> MarketDataMinute:
    now = utc_now()
    return MarketDataMinute(
        instrument_id=instrument_id,
        trade_date=(start.astimezone(timezone(timedelta(hours=8)))).date(),
        bar_start_time=start,
        bar_end_time=start + timedelta(minutes=1),
        signal_time=start + timedelta(minutes=1),
        market_timezone="Asia/Taipei",
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=10,
        turnover=Decimal("1000"),
        trade_count=None,
        price_adjustment="none",
        source="secret_provider",
        source_fetched_at=now,
        asof_ts=now,
        created_at=now,
        updated_at=now,
    )


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


@pytest.mark.asyncio
async def test_dataset_catalog_is_active_provider_free_and_reports_configured_empty(
    client: AsyncClient, test_session
):
    us_id = uuid7()
    test_session.add_all(
        [
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            dataset(
                "tw_equity_minute",
                market="TW",
                asset_class="equity",
                schema_id="market_minute",
            ),
            dataset(
                "inactive", market="HK", asset_class="equity", schema_id="market_eod", active=False
            ),
            instrument(us_id, "AAPL", market="US"),
            eod(us_id, date(2026, 9, 18), "250"),
        ]
    )
    await test_session.commit()

    response = await client.get("/api/v1/serve/datasets")
    assert response.status_code == 200
    payload = response.json()
    assert [item["dataset_key"] for item in payload["data"]] == [
        "tw_equity_minute",
        "us_equity_eod",
    ]
    assert payload["data"][0]["availability"] == "configured_empty"
    assert payload["data"][1]["coverage"] == {
        "coverage_start_date": "2026-09-18",
        "coverage_end_date": "2026-09-18",
        "instrument_count": 1,
    }
    serialized = response.text.lower()
    assert "secret_provider" not in serialized
    assert "credential" not in serialized


@pytest.mark.asyncio
async def test_unknown_or_mismatched_active_registry_config_fails_closed(
    client: AsyncClient, test_session
):
    invalid = dataset("bad", market="TW", asset_class="equity", schema_id="market_eod")
    invalid.config["defaults"]["market"] = "US"
    test_session.add(invalid)
    await test_session.commit()

    response = await client.get("/api/v1/serve/datasets")
    assert_error(response, 503, "SERVE_CONFIGURATION_INVALID")
    assert "US" not in response.text


@pytest.mark.asyncio
async def test_internally_consistent_unsupported_active_scope_fails_closed(
    client: AsyncClient, test_session
):
    test_session.add(
        dataset("cn_equity_eod", market="CN", asset_class="equity", schema_id="market_eod")
    )
    await test_session.commit()

    response = await client.get("/api/v1/serve/datasets")

    assert_error(response, 503, "SERVE_CONFIGURATION_INVALID")
    assert "CN" not in response.text


@pytest.mark.asyncio
async def test_instruments_search_facets_sort_pagination_and_domain_coverage(
    client: AsyncClient, test_session
):
    first_id = UUID("11111111-1111-1111-1111-111111111111")
    second_id = UUID("22222222-2222-2222-2222-222222222222")
    etf_id = UUID("33333333-3333-3333-3333-333333333333")
    hidden_id = UUID("44444444-4444-4444-4444-444444444444")
    partial_id = UUID("55555555-5555-5555-5555-555555555555")
    test_session.add_all(
        [
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            dataset(
                "tw_equity_minute",
                market="TW",
                asset_class="equity",
                schema_id="market_minute",
            ),
            dataset("tw_etf_minute", market="TW", asset_class="etf", schema_id="market_minute"),
            instrument(first_id, "2330", name="台積電"),
            instrument(second_id, "2317", name="鴻海"),
            instrument(etf_id, "0050", asset_class="etf", name="元大台灣50"),
            instrument(hidden_id, "BTC", market="CRYPTO", asset_class="crypto"),
            instrument(partial_id, "PART", name="Partial stats"),
            InstrumentStats(
                instrument_id=first_id,
                eod_first_date=date(2020, 1, 2),
                eod_latest_date=date(2026, 9, 18),
                eod_latest_close=Decimal("1300"),
                minute_first_bar_at=datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
                minute_latest_bar_at=datetime(2026, 9, 18, 5, 30, tzinfo=timezone.utc),
                minute_latest_close=Decimal("1301"),
                updated_at=utc_now(),
            ),
            InstrumentStats(
                instrument_id=etf_id,
                # Legacy EOD fields must not leak into an unsupported EOD domain.
                eod_first_date=date(2020, 1, 2),
                eod_latest_date=date(2026, 9, 18),
                eod_latest_close=Decimal("200"),
                minute_first_bar_at=datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
                minute_latest_bar_at=datetime(2026, 9, 18, 5, 30, tzinfo=timezone.utc),
                minute_latest_close=Decimal("201"),
                updated_at=utc_now(),
            ),
            InstrumentStats(
                instrument_id=partial_id,
                eod_latest_date=date(2026, 9, 18),
                eod_latest_close=Decimal("150"),
                minute_latest_bar_at=datetime(2026, 9, 18, 5, 30, tzinfo=timezone.utc),
                minute_latest_close=Decimal("151"),
                updated_at=utc_now(),
            ),
        ]
    )
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/instruments?q=２３３０&has_eod=true&sort_by=eod_latest_close&sort_dir=desc&page=1&page_size=1"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total_records"] == 1
    assert payload["data"][0]["symbol"] == "2330"
    assert payload["data"][0]["coverage"]["eod"]["latest_close"] == "1300.00000000"
    assert payload["data"][0]["coverage"]["minute"]["latest_close"] == "1301.00000000"
    assert payload["facets"] == {
        "markets": ["TW"],
        "asset_classes": ["equity", "etf"],
        "statuses": ["active"],
    }

    sorted_response = await client.get(
        "/api/v1/serve/instruments?sort_by=eod_latest_close&sort_dir=asc&page_size=4"
    )
    assert [item["symbol"] for item in sorted_response.json()["data"]] == [
        "2330",
        "2317",
        "0050",
        "PART",
    ]
    assert sorted_response.json()["data"][2]["coverage"]["eod"] is None
    assert sorted_response.json()["data"][3]["coverage"] == {
        "eod": None,
        "minute": None,
        "futures": None,
    }

    minute_sorted = await client.get(
        "/api/v1/serve/instruments?sort_by=minute_latest_close&sort_dir=asc&page_size=4"
    )
    assert [item["symbol"] for item in minute_sorted.json()["data"]] == [
        "0050",
        "2330",
        "2317",
        "PART",
    ]
    no_eod = await client.get("/api/v1/serve/instruments?has_eod=false&page_size=4")
    assert [item["symbol"] for item in no_eod.json()["data"]] == ["2317", "0050", "PART"]
    no_minute = await client.get("/api/v1/serve/instruments?has_minute=false&page_size=4")
    assert [item["symbol"] for item in no_minute.json()["data"]] == ["2317", "PART"]

    etf = await client.get(f"/api/v1/serve/instruments/{etf_id}")
    assert etf.json()["data"]["coverage"]["eod"] is None
    assert etf.json()["data"]["coverage"]["minute"] is not None
    assert_error(
        await client.get(f"/api/v1/serve/instruments/{hidden_id}"), 404, "INSTRUMENT_NOT_FOUND"
    )


@pytest.mark.asyncio
async def test_all_coverage_sorts_mask_inactive_partial_and_missing_stats(
    client: AsyncClient, test_session
):
    tw_equity_id = UUID("10000000-0000-0000-0000-000000000001")
    tw_etf_id = UUID("20000000-0000-0000-0000-000000000002")
    us_equity_id = UUID("30000000-0000-0000-0000-000000000003")
    partial_id = UUID("40000000-0000-0000-0000-000000000004")
    empty_id = UUID("50000000-0000-0000-0000-000000000005")
    test_session.add_all(
        [
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            dataset(
                "tw_equity_minute",
                market="TW",
                asset_class="equity",
                schema_id="market_minute",
            ),
            dataset("tw_etf_minute", market="TW", asset_class="etf", schema_id="market_minute"),
            instrument(tw_equity_id, "TW_EQ"),
            instrument(tw_etf_id, "TW_ETF", asset_class="etf"),
            instrument(us_equity_id, "US_EQ", market="US"),
            instrument(partial_id, "PART"),
            instrument(empty_id, "EMPTY"),
            InstrumentStats(
                instrument_id=tw_equity_id,
                eod_first_date=date(2020, 1, 2),
                eod_latest_date=date(2026, 9, 18),
                eod_latest_close=Decimal("100"),
                minute_first_bar_at=datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
                minute_latest_bar_at=datetime(2026, 9, 18, 5, 30, tzinfo=timezone.utc),
                minute_latest_close=Decimal("100"),
                updated_at=utc_now(),
            ),
            InstrumentStats(
                instrument_id=tw_etf_id,
                eod_first_date=date(2019, 1, 2),
                eod_latest_date=date(2026, 9, 19),
                eod_latest_close=Decimal("50"),
                minute_first_bar_at=datetime(2026, 9, 2, 1, tzinfo=timezone.utc),
                minute_latest_bar_at=datetime(2026, 9, 17, 5, 30, tzinfo=timezone.utc),
                minute_latest_close=Decimal("50"),
                updated_at=utc_now(),
            ),
            InstrumentStats(
                instrument_id=us_equity_id,
                eod_first_date=date(2021, 1, 2),
                eod_latest_date=date(2026, 9, 17),
                eod_latest_close=Decimal("75"),
                minute_first_bar_at=datetime(2026, 8, 1, 1, tzinfo=timezone.utc),
                minute_latest_bar_at=datetime(2026, 9, 19, 5, 30, tzinfo=timezone.utc),
                minute_latest_close=Decimal("25"),
                updated_at=utc_now(),
            ),
            InstrumentStats(
                instrument_id=partial_id,
                eod_latest_date=date(2026, 9, 20),
                eod_latest_close=Decimal("10"),
                minute_latest_bar_at=datetime(2026, 9, 20, 5, 30, tzinfo=timezone.utc),
                minute_latest_close=Decimal("10"),
                updated_at=utc_now(),
            ),
        ]
    )
    await test_session.commit()

    expected_orders = {
        "eod_first_date": {
            "asc": ["TW_EQ", "US_EQ", "TW_ETF", "PART", "EMPTY"],
            "desc": ["US_EQ", "TW_EQ", "TW_ETF", "PART", "EMPTY"],
        },
        "eod_latest_date": {
            "asc": ["US_EQ", "TW_EQ", "TW_ETF", "PART", "EMPTY"],
            "desc": ["TW_EQ", "US_EQ", "TW_ETF", "PART", "EMPTY"],
        },
        "eod_latest_close": {
            "asc": ["US_EQ", "TW_EQ", "TW_ETF", "PART", "EMPTY"],
            "desc": ["TW_EQ", "US_EQ", "TW_ETF", "PART", "EMPTY"],
        },
        "minute_first_bar_at": {
            "asc": ["TW_EQ", "TW_ETF", "US_EQ", "PART", "EMPTY"],
            "desc": ["TW_ETF", "TW_EQ", "US_EQ", "PART", "EMPTY"],
        },
        "minute_latest_bar_at": {
            "asc": ["TW_ETF", "TW_EQ", "US_EQ", "PART", "EMPTY"],
            "desc": ["TW_EQ", "TW_ETF", "US_EQ", "PART", "EMPTY"],
        },
        "minute_latest_close": {
            "asc": ["TW_ETF", "TW_EQ", "US_EQ", "PART", "EMPTY"],
            "desc": ["TW_EQ", "TW_ETF", "US_EQ", "PART", "EMPTY"],
        },
    }
    for sort_by, directions in expected_orders.items():
        for sort_dir, expected in directions.items():
            response = await client.get(
                "/api/v1/serve/instruments",
                params={"sort_by": sort_by, "sort_dir": sort_dir, "page_size": 5},
            )
            assert response.status_code == 200
            assert [item["symbol"] for item in response.json()["data"]] == expected

    payload = (await client.get("/api/v1/serve/instruments?sort_by=symbol&page_size=5")).json()[
        "data"
    ]
    by_symbol = {item["symbol"]: item["coverage"] for item in payload}
    assert by_symbol["TW_ETF"]["eod"] is None
    assert by_symbol["US_EQ"]["minute"] is None
    assert by_symbol["PART"] == {"eod": None, "minute": None, "futures": None}
    assert by_symbol["EMPTY"] == {"eod": None, "minute": None, "futures": None}


@pytest.mark.asyncio
async def test_eod_requires_scope_and_distinguishes_unsupported_from_empty(
    client: AsyncClient, test_session
):
    equity_id, etf_id = uuid7(), uuid7()
    test_session.add_all(
        [
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            dataset("tw_etf_minute", market="TW", asset_class="etf", schema_id="market_minute"),
            instrument(equity_id, "2330"),
            instrument(etf_id, "0050", asset_class="etf"),
        ]
    )
    await test_session.commit()

    assert_error(await client.get("/api/v1/serve/eod"), 422, "QUERY_SCOPE_REQUIRED")
    assert_error(
        await client.get(f"/api/v1/serve/eod?instrument_id={etf_id}"),
        422,
        "DATASET_NOT_AVAILABLE",
    )
    empty = await client.get(f"/api/v1/serve/eod?instrument_id={equity_id}")
    assert empty.status_code == 200
    assert empty.json()["data"] == []


@pytest.mark.asyncio
async def test_eod_cursor_is_stable_and_rejects_scope_changes(client: AsyncClient, test_session):
    instrument_id = uuid7()
    test_session.add_all(
        [
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            instrument(instrument_id, "AAPL", market="US"),
            eod(instrument_id, date(2026, 9, 18), "250"),
            eod(instrument_id, date(2026, 9, 17), "249"),
        ]
    )
    await test_session.commit()

    first = await client.get(f"/api/v1/serve/eod?instrument_id={instrument_id}&page_size=1")
    assert first.json()["data"][0]["trade_date"] == "2026-09-18"
    cursor = first.json()["pagination"]["next_cursor"]
    second = await client.get(
        f"/api/v1/serve/eod?instrument_id={instrument_id}&page_size=1&cursor={cursor}"
    )
    assert second.json()["data"][0]["trade_date"] == "2026-09-17"
    assert_error(
        await client.get(
            f"/api/v1/serve/eod?instrument_id={instrument_id}&start_date=2026-09-18&cursor={cursor}"
        ),
        422,
        "INVALID_CURSOR",
    )
    assert_error(
        await client.get(f"/api/v1/serve/eod?instrument_id={instrument_id}&cursor=%%%"),
        422,
        "INVALID_CURSOR",
    )


def test_cursor_decoder_rejects_non_urlsafe_base64_alphabet() -> None:
    with pytest.raises(HTTPException) as exc_info:
        serve_service._decode_cursor("abc+def")

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_eod_market_symbol_validation_is_atomic(client: AsyncClient, test_session):
    instrument_id = uuid7()
    test_session.add_all(
        [
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            instrument(instrument_id, "AAPL", market="US"),
        ]
    )
    await test_session.commit()

    assert_error(await client.get("/api/v1/serve/eod?market="), 422, "QUERY_SCOPE_REQUIRED")
    assert_error(await client.get("/api/v1/serve/eod?market=%20%20"), 422, "QUERY_SCOPE_REQUIRED")

    assert_error(
        await client.get("/api/v1/serve/eod?market=US&symbols=AAPL,MISSING"),
        404,
        "INSTRUMENT_NOT_FOUND",
    )
    assert_error(
        await client.get(
            "/api/v1/serve/eod?market=US&symbols=" + ",".join(f"S{i}" for i in range(51))
        ),
        422,
        "INVALID_SYMBOLS",
    )
    assert_error(
        await client.get(f"/api/v1/serve/eod?instrument_id={instrument_id}&symbols=AAPL"),
        422,
        "INVALID_SYMBOLS",
    )


@pytest.mark.asyncio
async def test_minute_default_range_and_cursor_freeze_latest_available_window(
    client: AsyncClient, test_session
):
    instrument_id = uuid7()
    test_session.add_all(
        [
            dataset(
                "tw_equity_minute",
                market="TW",
                asset_class="equity",
                schema_id="market_minute",
            ),
            instrument(instrument_id, "2330"),
            minute(instrument_id, datetime(2026, 3, 31, 5, 30, tzinfo=timezone.utc), "1301"),
            minute(instrument_id, datetime(2026, 3, 31, 5, 29, tzinfo=timezone.utc), "1300"),
        ]
    )
    await test_session.commit()

    first = await client.get(f"/api/v1/serve/minute?instrument_id={instrument_id}&page_size=1")
    payload = first.json()
    assert payload["range"] == {
        "start_date": "2026-02-28",
        "end_date": "2026-03-31",
        "anchor": "latest_available",
    }
    assert payload["data"][0]["bar_start_time"].endswith("Z")
    cursor = payload["pagination"]["next_cursor"]
    second = await client.get(
        f"/api/v1/serve/minute?instrument_id={instrument_id}&page_size=1&cursor={cursor}"
    )
    assert second.json()["range"] == payload["range"]
    assert second.json()["data"][0]["close"] == "1300.00000000"


@pytest.mark.asyncio
async def test_minute_range_semantics_include_leap_day_boundary(client: AsyncClient, test_session):
    instrument_id = uuid7()
    test_session.add_all(
        [
            dataset(
                "tw_equity_minute",
                market="TW",
                asset_class="equity",
                schema_id="market_minute",
            ),
            instrument(instrument_id, "2330"),
        ]
    )
    await test_session.commit()

    accepted = await client.get(
        f"/api/v1/serve/minute?instrument_id={instrument_id}&start_date=2024-02-29&end_date=2029-02-28"
    )
    assert accepted.status_code == 200
    assert accepted.json()["range"] == {
        "start_date": None,
        "end_date": None,
        "anchor": "explicit",
    }
    assert_error(
        await client.get(
            f"/api/v1/serve/minute?instrument_id={instrument_id}&start_date=2024-02-29&end_date=2029-03-01"
        ),
        422,
        "INVALID_DATE_RANGE",
    )
    assert_error(
        await client.get(
            f"/api/v1/serve/minute?instrument_id={instrument_id}&start_date=2026-01-01"
        ),
        422,
        "INVALID_DATE_RANGE",
    )

    upper_bound = await client.get(
        f"/api/v1/serve/minute?instrument_id={instrument_id}&start_date=9999-01-01&end_date=9999-01-01"
    )
    assert upper_bound.status_code == 200
    assert upper_bound.json()["range"] == {
        "start_date": None,
        "end_date": None,
        "anchor": "explicit",
    }

    for forged_start, forged_end in (
        ("2000-01-01", "2026-01-01"),
        ("2026-01-02", "2026-01-01"),
    ):
        forged_cursor = serve_service._encode_cursor(
            {
                "v": 1,
                "kind": "minute",
                "instrument_id": str(instrument_id),
                "start_date": forged_start,
                "end_date": forged_end,
                "anchor": "explicit",
                "last_bar_start_time": "2026-01-01T00:00:00+00:00",
            }
        )
        assert_error(
            await client.get(
                f"/api/v1/serve/minute?instrument_id={instrument_id}&cursor={forged_cursor}"
            ),
            422,
            "INVALID_CURSOR",
        )


@pytest.mark.asyncio
async def test_minute_no_data_has_empty_resolved_range(client: AsyncClient, test_session):
    instrument_id = uuid7()
    test_session.add_all(
        [
            dataset("tw_etf_minute", market="TW", asset_class="etf", schema_id="market_minute"),
            instrument(instrument_id, "0050", asset_class="etf"),
        ]
    )
    await test_session.commit()

    response = await client.get(f"/api/v1/serve/minute?instrument_id={instrument_id}")
    assert response.status_code == 200
    assert response.json()["data"] == []
    assert response.json()["range"] == {
        "start_date": None,
        "end_date": None,
        "anchor": "latest_available",
    }


@pytest.mark.asyncio
async def test_current_serve_routes_use_optional_auth_dependency(client: AsyncClient, test_session):
    settings = get_settings()
    original = settings.SERVE_REQUIRE_AUTH
    settings.SERVE_REQUIRE_AUTH = True
    _, key = await create_api_key(
        test_session,
        owner="serve-test",
        tier="standard",
        scopes=["serve"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        kind="serve",
        name="serve-test",
        role=None,
        commit=False,
    )
    try:
        assert (await client.get("/api/v1/serve/datasets")).status_code == 401
        assert (
            await client.get("/api/v1/serve/datasets", headers={settings.API_KEY_HEADER: key})
        ).status_code == 200
    finally:
        settings.SERVE_REQUIRE_AUTH = original


@pytest.mark.asyncio
async def test_removed_routes_are_404_and_absent_from_openapi(client: AsyncClient):
    removed = [
        "/api/v1/serve/lookup/instruments",
        "/api/v1/serve/lookup/macro-series",
        "/api/v1/serve/eod/11111111-1111-1111-1111-111111111111",
        "/api/v1/serve/corporate-actions",
        "/api/v1/serve/macro/series",
        "/api/v1/serve/macro/observations",
        "/api/v1/serve/futures/contracts",
        "/api/v1/serve/futures/continuous",
        "/api/v1/serve/bonds",
        "/api/v1/serve/bonds/eod",
    ]
    for path in removed:
        assert (await client.get(path)).status_code == 404

    paths = (await client.get("/openapi.json")).json()["paths"]
    for path in removed:
        assert path not in paths
    assert {
        "/api/v1/serve/datasets",
        "/api/v1/serve/instruments",
        "/api/v1/serve/instruments/{instrument_id}",
        "/api/v1/serve/eod",
        "/api/v1/serve/minute",
        "/api/v1/serve/calendar",
        "/api/v1/serve/calendar/years/{market}/{year}",
        "/api/v1/serve/market-freshness",
    }.issubset(paths)
