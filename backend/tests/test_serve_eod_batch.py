"""Daily bars for several instruments at once, ordering, units and latest quotes."""

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.models.canonical import CalendarRevisionDay, CalendarYearRevision
from app.utils import uuid7
from tests.test_serve_api import assert_error, dataset, eod, instrument

TSMC = UUID("11111111-1111-1111-1111-111111111111")
AAPL = UUID("22222222-2222-2222-2222-222222222222")
TENCENT = UUID("33333333-3333-3333-3333-333333333333")
SAME_EQUITY = UUID("44444444-4444-4444-4444-444444444444")
SAME_ETF = UUID("55555555-5555-5555-5555-555555555555")
NO_BARS = UUID("66666666-6666-6666-6666-666666666666")
INDEX = UUID("77777777-7777-7777-7777-777777777777")

MON, TUE, WED = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)


@pytest.fixture
async def bars(test_session) -> None:
    test_session.add_all(
        [
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            dataset("tw_etf_eod", market="TW", asset_class="etf", schema_id="market_eod"),
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            dataset("us_index_eod", market="US", asset_class="index", schema_id="market_eod"),
            dataset("hk_equity_eod", market="HK", asset_class="equity", schema_id="market_eod"),
            instrument(TSMC, "2330", name="TSMC"),
            instrument(AAPL, "AAPL", market="US", name="Apple"),
            instrument(TENCENT, "00700", market="HK", name="Tencent"),
            instrument(SAME_EQUITY, "SAME"),
            instrument(SAME_ETF, "SAME", asset_class="etf"),
            instrument(NO_BARS, "EMPTY"),
            instrument(INDEX, "^GSPC", market="US", asset_class="index"),
            eod(TSMC, MON, "1000"),
            eod(TSMC, TUE, "1010"),
            eod(TSMC, WED, "1005.5"),
            eod(AAPL, MON, "200"),
            eod(AAPL, TUE, "190"),
            eod(TENCENT, TUE, "400"),
        ]
    )
    await test_session.commit()


def _rows(response) -> list[tuple[str, str]]:
    assert response.status_code == 200, response.text
    return [(row["symbol"], row["trade_date"]) for row in response.json()["data"]]


@pytest.mark.asyncio
async def test_eod_reads_several_markets_in_one_call_with_units(client: AsyncClient, bars):
    response = await client.get(
        "/api/v1/serve/eod",
        params={"instruments": f"TW:2330,us:aapl,{TENCENT}", "start_date": "2026-11-17"},
    )

    assert _rows(response) == [
        ("2330", "2026-11-18"),
        ("2330", "2026-11-17"),
        ("AAPL", "2026-11-17"),
        ("00700", "2026-11-17"),
    ]
    assert response.json()["units"] == {
        "price_adjustment": "none",
        "trade_date": "exchange_local",
        "instants": "UTC",
        "volume_by_asset_class": {
            "equity": "share",
            "etf": "share",
            "index": "provider_defined",
        },
    }


@pytest.mark.asyncio
async def test_eod_ascending_pages_follow_the_cursor_and_the_cursor_keeps_its_order(
    client: AsyncClient, bars
):
    params: dict[str, str | int] = {
        "instruments": "TW:2330,US:AAPL",
        "order": "asc",
        "page_size": 2,
    }
    seen: list[tuple[str, str]] = []
    while True:
        response = await client.get("/api/v1/serve/eod", params=params)
        seen.extend(_rows(response))
        cursor = response.json()["pagination"]["next_cursor"]
        if not cursor:
            break
        last_cursor = params["cursor"] = cursor

    assert [day for _, day in seen] == sorted(day for _, day in seen)
    assert len(seen) == 5
    flipped = await client.get(
        "/api/v1/serve/eod",
        params={"instruments": "TW:2330,US:AAPL", "page_size": 2, "cursor": last_cursor},
    )
    assert_error(flipped, 422, "INVALID_CURSOR")


@pytest.mark.asyncio
async def test_eod_names_the_candidates_of_an_ambiguous_symbol(client: AsyncClient, bars):
    response = await client.get("/api/v1/serve/eod", params={"instruments": "TW:SAME"})

    assert_error(response, 422, "AMBIGUOUS_INSTRUMENT")
    details = response.json()["detail"]["details"]
    assert details["inputs"] == ["TW:SAME"]
    assert {item["asset_class"] for item in details["matches"]["TW:SAME"]} == {"equity", "etf"}


@pytest.mark.asyncio
async def test_latest_eod_reports_change_and_lists_what_it_cannot_answer(client: AsyncClient, bars):
    response = await client.get(
        "/api/v1/serve/eod/latest",
        params={"instruments": "TW:2330,US:AAPL,HK:700,TW:SAME,TW:EMPTY,TW:NOPE,US:^GSPC"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    quotes = {item["input"]: item for item in payload["data"]}
    assert quotes["TW:2330"]["trade_date"] == "2026-11-18"
    assert quotes["TW:2330"]["previous_trade_date"] == "2026-11-17"
    assert quotes["TW:2330"]["close"] == "1005.50000000"
    assert quotes["TW:2330"]["previous_close"] == "1010.00000000"
    assert quotes["TW:2330"]["change"] == "-4.50000000"
    assert quotes["TW:2330"]["change_percent"] == "-0.4455"
    assert quotes["TW:2330"]["missing_sessions"] == 0
    assert quotes["US:AAPL"]["change_percent"] == "-5.0000"
    assert quotes["HK:700"]["instrument"]["symbol"] == "00700"
    assert quotes["HK:700"]["previous_close"] is None and quotes["HK:700"]["change"] is None
    assert quotes["HK:700"]["missing_sessions"] is None
    assert {item["input"]: item["reason"] for item in payload["missing"]} == {
        "TW:SAME": "ambiguous",
        "TW:EMPTY": "no_data",
        "TW:NOPE": "not_found",
        "US:^GSPC": "no_data",
    }
    ambiguous = next(item for item in payload["missing"] if item["reason"] == "ambiguous")
    assert {item["asset_class"] for item in ambiguous["matches"]} == {"equity", "etf"}


@pytest.mark.asyncio
async def test_latest_eod_as_of_a_date_takes_the_bar_on_or_before_it(client: AsyncClient, bars):
    response = await client.get(
        "/api/v1/serve/eod/latest",
        params={"instruments": f"{TSMC}", "as_of_date": "2026-11-17"},
    )

    quote = response.json()["data"][0]
    assert response.json()["as_of_date"] == "2026-11-17"
    assert (quote["trade_date"], quote["previous_trade_date"]) == ("2026-11-17", "2026-11-16")
    before_any = await client.get(
        "/api/v1/serve/eod/latest", params={"instruments": "TW:2330", "as_of_date": "2026-01-01"}
    )
    assert before_any.json()["missing"] == [
        {"input": "TW:2330", "reason": "no_data", "matches": []}
    ]


@pytest.mark.asyncio
async def test_latest_eod_reports_no_change_across_skipped_sessions(
    client: AsyncClient, test_session
):
    # TW publishes Tuesday as a holiday; US and HK fall back to weekday defaults.
    revision = CalendarYearRevision(
        id=uuid7(),
        market="TW",
        year=2026,
        revision=1,
        status="published",
        timezone="Asia/Taipei",
        expected_days=365,
        actual_days=1,
        source_kind="test",
    )
    test_session.add_all(
        [
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            dataset("hk_equity_eod", market="HK", asset_class="equity", schema_id="market_eod"),
            instrument(TSMC, "2330"),
            instrument(AAPL, "AAPL", market="US"),
            instrument(TENCENT, "00700", market="HK"),
            revision,
            eod(TSMC, MON, "1000"),
            eod(TSMC, WED, "1020"),
            eod(AAPL, date(2026, 11, 13), "200"),
            eod(AAPL, MON, "210"),
            eod(TENCENT, date(2026, 11, 6), "419.8"),
            eod(TENCENT, date(2026, 11, 20), "344"),
        ]
    )
    await test_session.flush()
    test_session.add(
        CalendarRevisionDay(
            id=uuid7(),
            calendar_revision_id=revision.id,
            trade_date=TUE,
            is_open=False,
            day_status="closed",
            source_kind="test",
        )
    )
    await test_session.commit()

    response = await client.get(
        "/api/v1/serve/eod/latest", params={"instruments": "TW:2330,US:AAPL,HK:700"}
    )

    assert response.status_code == 200, response.text
    quotes = {item["input"]: item for item in response.json()["data"]}
    # The holiday and the weekend are not skipped sessions.
    assert (quotes["TW:2330"]["missing_sessions"], quotes["TW:2330"]["change"]) == (
        0,
        "20.00000000",
    )
    assert (quotes["US:AAPL"]["missing_sessions"], quotes["US:AAPL"]["change_percent"]) == (
        0,
        "5.0000",
    )
    tencent = quotes["HK:700"]
    assert tencent["previous_trade_date"] == "2026-11-06"
    assert tencent["previous_close"] == "419.80000000"
    assert tencent["missing_sessions"] == 9
    assert tencent["change"] is None and tencent["change_percent"] is None


@pytest.mark.asyncio
async def test_latest_eod_marks_instruments_outside_daily_scopes_not_available(
    client: AsyncClient, test_session
):
    test_session.add_all(
        [
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            instrument(TSMC, "2330"),
            instrument(INDEX, "TAIEX", asset_class="index"),
        ]
    )
    await test_session.commit()

    response = await client.get("/api/v1/serve/eod/latest", params={"instruments": "TW:TAIEX"})

    assert response.json()["missing"] == [
        {"input": "TW:TAIEX", "reason": "not_available", "matches": []}
    ]


@pytest.mark.asyncio
async def test_datasets_name_their_operation_and_volume_unit(client: AsyncClient, bars):
    response = await client.get("/api/v1/serve/datasets")

    by_key = {item["dataset_key"]: item for item in response.json()["data"]}
    assert (by_key["tw_equity_eod"]["operation_id"], by_key["tw_equity_eod"]["volume_unit"]) == (
        "list_eod",
        "share",
    )
    assert by_key["us_index_eod"]["volume_unit"] == "provider_defined"
