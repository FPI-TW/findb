"""Bias ratios (乖離率) computed by Serve from stored TW daily closes."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.utils import uuid7
from tests.test_serve_api import assert_error, dataset, eod, instrument

URL = "/api/v1/serve/eod/bias-ratios"


def weekdays(end: date, count: int) -> list[date]:
    """The ``count`` weekdays ending on ``end``: TW open days without a published calendar."""
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


async def seed_series(session, symbol: str, days: list[date], *, asset_class: str = "equity"):
    instrument_id = uuid7()
    session.add(instrument(instrument_id, symbol, asset_class=asset_class))
    session.add_all(eod(instrument_id, day, str(100 + offset)) for offset, day in enumerate(days))
    await session.commit()
    return instrument_id


def by_window(row: dict) -> dict[int, dict]:
    return {value["window"]: value for value in row["values"]}


@pytest.fixture
async def tw_equity(test_session):
    test_session.add(
        dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod")
    )
    await test_session.commit()


@pytest.mark.asyncio
async def test_latest_bar_reports_each_window_or_why_not(
    client: AsyncClient, test_session, tw_equity
):
    days = weekdays(date(2026, 11, 20), 25)
    instrument_id = await seed_series(test_session, "2330", days)

    response = await client.get(URL, params={"instruments": "TW:2330", "windows": "20,5,60"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["windows"] == [5, 20, 60]
    assert body["price_basis"] == "unadjusted_close"
    [row] = body["data"]
    assert row["instrument"]["instrument_id"] == str(instrument_id)
    assert row["trade_date"] == "2026-11-20"
    values = by_window(row)
    # Closes are 100..124; the last five average 122, the last twenty 114.5.
    assert values[5]["sma"] == "122.0000"
    assert values[5]["bias_percent"] == "1.6393"
    assert values[20]["sma"] == "114.5000"
    assert Decimal(values[20]["bias_percent"]) == Decimal("8.2969")
    assert values[60] == {
        "window": 60,
        "sma": None,
        "bias_percent": None,
        "missing_sessions": None,
        "reason": "insufficient_history",
    }


@pytest.mark.asyncio
async def test_a_window_that_skips_an_open_session_is_not_reported(
    client: AsyncClient, test_session, tw_equity
):
    days = weekdays(date(2026, 11, 20), 26)
    gap = days[-8]  # inside the 20-bar window, outside the 5-bar one
    await seed_series(test_session, "2330", [day for day in days if day != gap])

    response = await client.get(URL, params={"instruments": "TW:2330", "windows": "5,20"})

    values = by_window(response.json()["data"][0])
    assert values[5]["reason"] is None
    assert values[20]["reason"] == "missing_sessions"
    assert values[20]["missing_sessions"] == 1
    assert values[20]["bias_percent"] is None


@pytest.mark.asyncio
async def test_a_range_is_answered_per_bar_and_pages_with_a_cursor(
    client: AsyncClient, test_session, tw_equity
):
    days = weekdays(date(2026, 11, 20), 12)
    await seed_series(test_session, "2330", days)
    params = {
        "instruments": "TW:2330",
        "windows": "5",
        "start_date": days[-4].isoformat(),
        "end_date": days[-1].isoformat(),
        "order": "asc",
        "page_size": 3,
    }

    first = await client.get(URL, params=params)
    assert first.status_code == 200, first.text
    assert [row["trade_date"] for row in first.json()["data"]] == [
        day.isoformat() for day in days[-4:-1]
    ]
    cursor = first.json()["pagination"]["next_cursor"]
    assert cursor
    second = await client.get(URL, params={**params, "cursor": cursor})
    assert [row["trade_date"] for row in second.json()["data"]] == [days[-1].isoformat()]
    assert second.json()["pagination"]["next_cursor"] is None
    # The window reaches back before the range: the first answered bar still has five closes.
    assert by_window(first.json()["data"][0])[5]["sma"] == "106.0000"

    changed = await client.get(URL, params={**params, "windows": "10", "cursor": cursor})
    assert_error(changed, 422, "INVALID_CURSOR")


@pytest.mark.asyncio
async def test_tw_indices_are_covered_and_other_markets_are_not(
    client: AsyncClient, test_session, tw_equity
):
    index = dataset("tw_twse_index_eod", market="TW", asset_class="index", schema_id="market_eod")
    index.config["allowed_sources"] = ["secret_provider"]
    us = dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod")
    test_session.add_all([index, us, instrument(uuid7(), "AAPL", market="US")])
    await test_session.commit()
    await seed_series(test_session, "^TWII", weekdays(date(2026, 11, 20), 5), asset_class="index")

    response = await client.get(URL, params={"instruments": "TW:^TWII", "windows": "5"})
    assert response.status_code == 200, response.text
    assert by_window(response.json()["data"][0])[5]["sma"] == "102.0000"

    assert_error(
        await client.get(URL, params={"instruments": "US:AAPL"}), 422, "DATASET_NOT_AVAILABLE"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"windows": "7"}, "INVALID_WINDOWS"),
        ({"windows": "5,x"}, "INVALID_WINDOWS"),
        ({"start_date": "2025-01-01", "end_date": "2026-01-02"}, "INVALID_DATE_RANGE"),
        ({"start_date": "2026-02-01", "end_date": "2026-01-01"}, "INVALID_DATE_RANGE"),
    ],
)
async def test_invalid_parameters_are_rejected(
    client: AsyncClient, test_session, tw_equity, params, code
):
    await seed_series(test_session, "2330", weekdays(date(2026, 11, 20), 5))

    assert_error(await client.get(URL, params={"instruments": "TW:2330", **params}), 422, code)


@pytest.mark.asyncio
async def test_without_any_tw_dataset_bias_ratios_are_not_available(
    client: AsyncClient, test_session
):
    test_session.add(
        dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod")
    )
    await test_session.commit()

    assert_error(
        await client.get(URL, params={"instruments": "TW:2330"}), 422, "DATASET_NOT_AVAILABLE"
    )
