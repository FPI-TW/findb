"""HKEX daily quotation rows served on their own endpoint and in the catalog."""

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.models.canonical import ExchangeDailyQuote, ExchangeReportArchive
from tests.test_serve_api import assert_error, dataset, exchange_quote_dataset, instrument

TENCENT = UUID("11111111-1111-1111-1111-111111111111")
HSBC = UUID("22222222-2222-2222-2222-222222222222")
TSMC = UUID("33333333-3333-3333-3333-333333333333")
URL = "/api/v1/serve/exchange-quotes"
FETCHED = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


def _archive(day: date) -> ExchangeReportArchive:
    digest = hashlib.sha256(day.isoformat().encode()).hexdigest()
    return ExchangeReportArchive(
        source="hkex",
        board="main",
        trade_date=day,
        gzip_sha256=digest,
        gzip_bytes=10,
        original_sha256=digest,
        original_bytes=20,
        source_url=f"https://www.hkex.com.hk/eng/stat/smstat/dayquot/d{day:%y%m%d}e.htm",
        source_last_modified=FETCHED,
        source_etag="etag",
        object_key=f"archive/v1/hkex/{digest}.htm.gz",
        object_etag="object-etag",
    )


def _quote(instrument_id: UUID, archive: ExchangeReportArchive, close: str, volume: int):
    return ExchangeDailyQuote(
        instrument_id=instrument_id,
        trade_date=archive.trade_date,
        board="main",
        currency="HKD",
        trading_status="traded" if volume else "no_trade",
        previous_close=Decimal("340"),
        close=Decimal(close),
        ask=Decimal(close) + 1,
        bid=Decimal(close) - 1,
        high=Decimal(close) + 2,
        low=Decimal(close) - 2,
        volume=volume,
        turnover=Decimal(volume) * Decimal(close),
        source="hkex",
        source_fetched_at=FETCHED,
        archive_id=archive.archive_id,
    )


@pytest.fixture
async def quotes(test_session) -> None:
    thursday, friday = _archive(date(2026, 11, 19)), _archive(date(2026, 11, 20))
    test_session.add_all(
        [
            dataset("hk_equity_eod", market="HK", asset_class="equity", schema_id="market_eod"),
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            exchange_quote_dataset(),
            instrument(TENCENT, "00700", market="HK", name="Tencent"),
            instrument(HSBC, "00005", market="HK", name="HSBC"),
            instrument(TSMC, "2330"),
            thursday,
            friday,
        ]
    )
    await test_session.flush()
    test_session.add_all(
        [
            _quote(TENCENT, thursday, "344", 100),
            _quote(TENCENT, friday, "350", 200),
            _quote(HSBC, friday, "60", 0),
        ]
    )
    await test_session.commit()


@pytest.mark.asyncio
async def test_exchange_quotes_serve_the_report_fields(client: AsyncClient, quotes):
    response = await client.get(URL, params={"instruments": "HK:700,HK:5"})

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    # Newest first, then by instrument_id within a date.
    assert [(row["symbol"], row["trade_date"]) for row in rows] == [
        ("00700", "2026-11-20"),
        ("00005", "2026-11-20"),
        ("00700", "2026-11-19"),
    ]
    tencent = next(row for row in rows if row["symbol"] == "00700")
    assert tencent["previous_close"] == "340.00000000"
    assert (tencent["bid"], tencent["ask"]) == ("349.00000000", "351.00000000")
    assert (tencent["board"], tencent["currency"], tencent["source"]) == ("main", "HKD", "hkex")
    hsbc = next(row for row in rows if row["symbol"] == "00005")
    assert (hsbc["trading_status"], hsbc["volume"]) == ("no_trade", 0)
    assert "archive_id" not in tencent and "run_id" not in tencent


@pytest.mark.asyncio
async def test_exchange_quotes_page_in_either_order(client: AsyncClient, quotes):
    params: dict[str, str | int] = {"instruments": "HK:700", "order": "asc", "page_size": 1}
    days: list[str] = []
    while True:
        response = await client.get(URL, params=params)
        assert response.status_code == 200, response.text
        days.extend(row["trade_date"] for row in response.json()["data"])
        if not (cursor := response.json()["pagination"]["next_cursor"]):
            break
        params["cursor"] = cursor
    assert days == ["2026-11-19", "2026-11-20"]
    bounded = await client.get(URL, params={"instruments": "HK:700", "start_date": "2026-11-20"})
    assert [row["trade_date"] for row in bounded.json()["data"]] == ["2026-11-20"]


@pytest.mark.asyncio
async def test_exchange_quotes_refuse_instruments_outside_the_report(client: AsyncClient, quotes):
    assert_error(
        await client.get(URL, params={"instruments": "TW:2330"}), 422, "DATASET_NOT_AVAILABLE"
    )
    assert_error(
        await client.get(URL, params={"instruments": "HK:99999"}), 404, "INSTRUMENT_NOT_FOUND"
    )


@pytest.mark.asyncio
async def test_exchange_quotes_need_an_active_report_dataset(client: AsyncClient, test_session):
    test_session.add_all(
        [
            dataset("hk_equity_eod", market="HK", asset_class="equity", schema_id="market_eod"),
            instrument(TENCENT, "00700", market="HK"),
        ]
    )
    await test_session.commit()

    assert_error(
        await client.get(URL, params={"instruments": "HK:700"}), 422, "DATASET_NOT_AVAILABLE"
    )


@pytest.mark.asyncio
async def test_catalog_lists_the_exchange_quote_dataset(client: AsyncClient, quotes):
    response = await client.get("/api/v1/serve/datasets")

    entry = next(
        item for item in response.json()["data"] if item["dataset_key"] == "hk_equity_official_eod"
    )
    assert entry["data_kind"] == "exchange_quotes"
    assert entry["operation_id"] == "list_exchange_quotes"
    assert entry["coverage"] == {
        "coverage_start_date": "2026-11-19",
        "coverage_end_date": "2026-11-20",
        "instrument_count": 2,
    }
