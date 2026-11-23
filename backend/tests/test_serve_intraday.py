"""Serve reads of canonical intraday bars and ticks: index first, bounded, one instrument."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal

import pyarrow as pa
import pytest
from httpx import AsyncClient

from app.api.deps import reset_source_rate_limit_state
from app.config import get_settings
from app.models.canonical import IntradayPartition
from app.services import intraday_partitions as store
from app.services import serve_intraday
from app.services.api_keys import create_api_key
from app.services.object_storage import ObjectChangedError, ObjectStorageConfigError
from app.utils import uuid7
from tests.intraday_support import FETCHED, Stores, bars, market_open, ticks
from tests.test_serve_api import dataset, instrument, intraday_dataset

BARS = "tw_equity_bar_1m"
TICKS = "tw_equity_tick"
MON, TUE, WED = date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22)


@pytest.fixture
def stores(monkeypatch) -> Iterator[Stores]:
    stores = Stores()
    serve_intraday.reset_read_state()
    monkeypatch.setattr(serve_intraday, "canonical_reader", lambda: stores.canonical)
    yield stores
    serve_intraday.reset_read_state()


@pytest.fixture
async def key(test_session) -> dict[str, str]:
    reset_source_rate_limit_state()
    _, plaintext = await create_api_key(
        test_session,
        owner="intraday-test",
        tier="standard",
        scopes=["serve"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=5000,
        kind="serve",
        name="intraday-test",
        role=None,
        commit=False,
    )
    return {get_settings().API_KEY_HEADER: plaintext}


@pytest.fixture
async def tw(test_session):
    """2330 with active EOD, bar and tick datasets."""
    instrument_id = uuid7()
    test_session.add_all(
        [
            dataset("tw_equity_eod", market="TW", asset_class="equity", schema_id="market_eod"),
            intraday_dataset(BARS, "bar_1m"),
            intraday_dataset(TICKS, "tick"),
            instrument(instrument_id, "2330", name="TSMC"),
        ]
    )
    await test_session.commit()
    return instrument_id


async def stored_day(
    session, stores: Stores, dataset_key: str, table: pa.Table, *, fetched_at: datetime = FETCHED
) -> IntradayPartition:
    """Store one symbol-day the way the Worker leaves it: a day object and its index row."""
    kind = "bar_1m" if "bar_start_time" in table.column_names else "tick"
    table = table.cast(store.row_schema(kind))
    symbol = table["symbol"][0].as_py()
    trade_date = table["trade_date"][0].as_py()
    key = store.day_key(dataset_key, symbol, trade_date)
    current = await session.get(IntradayPartition, (dataset_key, symbol, trade_date))
    expected = current.object_etag if current and current.object_layout == "day" else None
    etag = stores.canonical.write(
        key, store.encode_rows(table), content_type=store.CONTENT_TYPE, expected_etag=expected
    )
    row = current or IntradayPartition(
        dataset_key=dataset_key, symbol=symbol, trade_date=trade_date
    )
    row.row_count = table.num_rows
    row.source = "finlab"
    row.source_raw_sha256 = "a" * 64
    row.source_rows_sha256 = hashlib.sha256(fetched_at.isoformat().encode()).hexdigest()
    row.source_fetched_at = fetched_at
    row.object_layout = "day"
    row.object_key = key
    row.object_etag = etag
    session.add(row)
    await session.commit()
    return row


async def get_bars(client: AsyncClient, key, instrument_id, **params):
    return await client.get(
        "/api/v1/serve/intraday/bars",
        params={"instrument_id": str(instrument_id), **params},
        headers=key,
    )


async def get_ticks(client: AsyncClient, key, instrument_id, **params):
    return await client.get(
        "/api/v1/serve/intraday/ticks",
        params={"instrument_id": str(instrument_id), **params},
        headers=key,
    )


def assert_error(response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_minute_bars_are_served_from_the_indexed_days_in_lots(
    client, test_session, stores, key, tw
) -> None:
    await stored_day(test_session, stores, BARS, bars(day=MON))
    await stored_day(test_session, stores, BARS, bars(day=TUE, minutes=(0, 270)))

    response = await get_bars(client, key, tw, start_date=MON.isoformat(), end_date=WED.isoformat())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instrument"] == {
        "instrument_id": str(tw),
        "symbol": "2330",
        "name": "TSMC",
        "market": "TW",
        "asset_class": "equity",
        "currency": "TWD",
    }
    assert (body["dataset_key"], body["interval"]) == (BARS, "1m")
    assert body["units"] == {
        "volume": "lot",
        "price_adjustment": "none",
        "instants": "UTC",
        "trade_date": "exchange_local",
        "market_timezone": "Asia/Taipei",
    }
    assert body["range"] == {
        "start_date": MON.isoformat(),
        "end_date": WED.isoformat(),
        "anchor": "explicit",
    }
    assert [(row["trade_date"], row["bar_start_time"]) for row in body["data"]] == [
        (MON.isoformat(), "2026-07-20T01:00:00Z"),
        (MON.isoformat(), "2026-07-20T01:01:00Z"),
        (MON.isoformat(), "2026-07-20T01:02:00Z"),
        (TUE.isoformat(), "2026-07-21T01:00:00Z"),
        (TUE.isoformat(), "2026-07-21T05:30:00Z"),
    ]
    first = body["data"][0]
    assert first["bar_end_time"] == "2026-07-20T01:01:00Z"
    # Prices are the stored doubles as their shortest decimal text.
    assert (first["open"], first["high"], first["low"], first["vwap"]) == (
        "100.0",
        "101.0",
        "99.5",
        "100.25",
    )
    assert (first["volume"], first["trade_count"], first["source"]) == (10, 3, "finlab")
    assert body["pagination"]["next_cursor"] is None


@pytest.mark.asyncio
async def test_bars_page_through_days_with_a_cursor_bound_to_the_query(
    client, test_session, stores, key, tw
) -> None:
    for day in (MON, TUE, WED):
        await stored_day(test_session, stores, BARS, bars(day=day))
    seen: list[tuple[str, str]] = []
    cursor = None
    params = {"start_date": MON.isoformat(), "end_date": WED.isoformat(), "page_size": 4}
    for _ in range(5):
        response = await get_bars(
            client, key, tw, **params, **({"cursor": cursor} if cursor else {})
        )
        assert response.status_code == 200, response.text
        seen += [(row["trade_date"], row["bar_start_time"]) for row in response.json()["data"]]
        cursor = response.json()["pagination"]["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 9 and len(set(seen)) == 9 and seen == sorted(seen)

    first = await get_bars(client, key, tw, **params)
    cursor = first.json()["pagination"]["next_cursor"]
    assert_error(
        await get_bars(
            client, key, tw, start_date=TUE.isoformat(), end_date=WED.isoformat(), cursor=cursor
        ),
        422,
        "INVALID_CURSOR",
    )
    assert_error(
        await get_bars(client, key, tw, **params, interval="5m", cursor=cursor),
        422,
        "INVALID_CURSOR",
    )


@pytest.mark.asyncio
async def test_sixty_minute_bars_follow_finlab_labels_with_the_close_at_1333(
    client, test_session, stores, key, tw
) -> None:
    # 09:00 and 09:59 → 10:00; 10:00 → 11:00; 13:00, 13:24 and the 13:30 auction → 13:33.
    minutes = (0, 59, 60, 240, 264, 270)
    await stored_day(
        test_session,
        stores,
        BARS,
        bars(
            day=MON,
            minutes=minutes,
            open=[10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
            high=[10.5, 13.0, 12.5, 13.5, 16.0, 15.5],
            low=[9.5, 10.5, 11.5, 12.5, 13.5, 14.5],
            close=[10.2, 11.2, 12.2, 13.2, 14.2, 15.2],
            volume=[1, 3, 2, 1, 1, 2],
            trade_count=[1, 2, 1, 1, 1, None],
            vwap=[10.0, 12.0, 12.0, 13.0, 14.0, 15.0],
        ),
    )

    response = await get_bars(
        client, key, tw, interval="60m", start_date=MON.isoformat(), end_date=MON.isoformat()
    )

    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert [(row["bar_start_time"], row["bar_end_time"]) for row in rows] == [
        ("2026-07-20T01:00:00Z", "2026-07-20T02:00:00Z"),
        ("2026-07-20T02:00:00Z", "2026-07-20T03:00:00Z"),
        ("2026-07-20T05:00:00Z", "2026-07-20T05:33:00Z"),
    ]
    first, _, last = rows
    assert (first["open"], first["high"], first["low"], first["close"]) == (
        "10.0",
        "13.0",
        "9.5",
        "11.2",
    )
    assert (first["volume"], first["trade_count"], Decimal(first["vwap"])) == (
        4,
        3,
        Decimal("11.5"),
    )
    # One member without a trade count leaves the bucket's count unknown, not undercounted.
    assert (last["open"], last["close"], last["volume"], last["trade_count"]) == (
        "13.0",
        "15.2",
        4,
        None,
    )
    assert Decimal(last["vwap"]) == Decimal("14.25")


@pytest.mark.asyncio
async def test_bar_ranges_are_bounded_and_default_to_the_latest_stored_day(
    client, test_session, stores, key, tw
) -> None:
    july = date(2026, 7, 1)
    assert_error(
        await get_bars(client, key, tw, start_date=july.isoformat()), 422, "INVALID_DATE_RANGE"
    )
    assert_error(
        await get_bars(client, key, tw, start_date="2026-07-01", end_date="2026-08-01"),
        422,
        "INVALID_DATE_RANGE",
    )
    assert (
        await get_bars(client, key, tw, start_date="2026-07-01", end_date="2026-07-31")
    ).status_code == 200
    # 366 calendar days is the most an aggregated range may span.
    assert (
        await get_bars(
            client, key, tw, interval="60m", start_date="2025-12-31", end_date="2026-12-31"
        )
    ).status_code == 200
    assert_error(
        await get_bars(
            client, key, tw, interval="60m", start_date="2025-12-30", end_date="2026-12-31"
        ),
        422,
        "INVALID_DATE_RANGE",
    )
    empty = await get_bars(client, key, tw)
    assert empty.json()["data"] == []
    assert empty.json()["range"] == {
        "start_date": None,
        "end_date": None,
        "anchor": "latest_available",
    }

    await stored_day(test_session, stores, BARS, bars(day=MON))
    await stored_day(test_session, stores, BARS, bars(day=TUE, minutes=(5,)))

    latest = await get_bars(client, key, tw)
    assert latest.json()["range"]["start_date"] == TUE.isoformat()
    assert [row["bar_start_time"] for row in latest.json()["data"]] == ["2026-07-21T01:05:00Z"]


@pytest.mark.asyncio
async def test_a_merged_month_is_read_by_the_indexed_days_row_groups_only(
    client, test_session, stores, key, tw
) -> None:
    for day in (MON, TUE, WED):
        await stored_day(test_session, stores, BARS, bars(day=day))
    superseded = await store.merge_month(
        test_session,
        stores.canonical,
        dataset_key=BARS,
        symbol="2330",
        year=2026,
        month=7,
        kind="bar_1m",
    )
    await test_session.commit()
    store.delete_superseded(stores.canonical, superseded)
    # Republished after the merge: the month object keeps the old rows of that day.
    await stored_day(
        test_session,
        stores,
        BARS,
        bars(day=TUE, minutes=(9,)),
        fetched_at=FETCHED + timedelta(days=30),
    )
    stores.r2.calls.clear()

    response = await get_bars(client, key, tw, start_date=MON.isoformat(), end_date=WED.isoformat())

    assert response.status_code == 200, response.text
    assert [
        (row["trade_date"], row["bar_start_time"][11:16]) for row in response.json()["data"]
    ] == [
        (MON.isoformat(), "01:00"),
        (MON.isoformat(), "01:01"),
        (MON.isoformat(), "01:02"),
        (TUE.isoformat(), "01:09"),
        (WED.isoformat(), "01:00"),
        (WED.isoformat(), "01:01"),
        (WED.isoformat(), "01:02"),
    ]
    reads = [arguments for name, arguments in stores.r2.calls if name == "get"]
    # Every read names the indexed version; nothing lists the bucket.
    assert reads and all("IfMatch" in arguments and "Range" in arguments for arguments in reads)
    assert {name for name, _ in stores.r2.calls} == {"get"}

    stores.r2.calls.clear()
    again = await get_bars(client, key, tw, start_date=MON.isoformat(), end_date=WED.isoformat())
    assert again.json()["data"] == response.json()["data"]
    assert stores.r2.calls == []  # served from the cache of recently read days


@pytest.mark.asyncio
async def test_an_object_that_moved_under_the_index_is_read_again_from_the_index(
    client, test_session, stores, key, tw, monkeypatch
) -> None:
    await stored_day(test_session, stores, BARS, bars(day=MON))
    real = serve_intraday.read_days
    calls = []

    def flaky(reader, days, kind, cache):
        calls.append([day.object_etag for day in days])
        if len(calls) == 1:
            raise ObjectChangedError("replaced")
        return real(reader, days, kind, cache)

    monkeypatch.setattr(serve_intraday, "read_days", flaky)
    response = await get_bars(client, key, tw, start_date=MON.isoformat(), end_date=MON.isoformat())
    assert response.status_code == 200, response.text
    assert len(calls) == 2 and len(response.json()["data"]) == 3


@pytest.mark.asyncio
async def test_an_object_that_is_not_the_indexed_version_is_never_served(
    client, test_session, stores, key, tw
) -> None:
    row = await stored_day(test_session, stores, BARS, bars(day=MON))
    # A newer object was written but its index row was not committed.
    stores.canonical.write(
        row.object_key,
        store.encode_rows(bars(day=MON, minutes=(7,)).cast(store.row_schema("bar_1m"))),
        content_type=store.CONTENT_TYPE,
        expected_etag=row.object_etag,
    )

    response = await get_bars(client, key, tw, start_date=MON.isoformat(), end_date=MON.isoformat())

    assert_error(response, 503, "INTRADAY_DATA_CHANGING")
    assert response.headers["Retry-After"] == "5"


@pytest.mark.asyncio
async def test_unavailable_storage_and_saturated_reads_answer_503(
    client, test_session, stores, key, tw, monkeypatch
) -> None:
    await stored_day(test_session, stores, BARS, bars(day=MON))
    params = {"start_date": MON.isoformat(), "end_date": MON.isoformat()}

    settings = get_settings()
    monkeypatch.setattr(settings, "INTRADAY_SERVE_MAX_CONCURRENT_READS", 1)
    monkeypatch.setattr(settings, "INTRADAY_SERVE_READ_WAIT_SECONDS", 0.0)
    serve_intraday.reset_read_state()
    slots = serve_intraday._slots()
    assert slots.acquire(blocking=False)
    try:
        busy = await get_bars(client, key, tw, **params)
    finally:
        slots.release()
    assert_error(busy, 503, "INTRADAY_READS_BUSY")
    assert busy.headers["Retry-After"] == "1"

    def unconfigured():
        raise ObjectStorageConfigError("R2 credential is not configured for this role")

    monkeypatch.setattr(serve_intraday, "canonical_reader", unconfigured)
    assert_error(await get_bars(client, key, tw, **params), 503, "INTRADAY_STORAGE_UNAVAILABLE")


# ---------------------------------------------------------------------------
# Ticks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticks_of_one_day_page_by_sequence_and_filter_by_local_time(
    client, test_session, stores, key, tw
) -> None:
    after_hours = ticks(day=MON, sequences=(4,))
    after_hours = after_hours.set_column(
        after_hours.schema.get_field_index("trade_time"),
        "trade_time",
        pa.array([market_open(MON) + timedelta(hours=5, minutes=30)], pa.timestamp("us", tz="UTC")),
    ).set_column(
        after_hours.schema.get_field_index("session"), "session", pa.array(["after_hours_fixed"])
    )
    await stored_day(
        test_session,
        stores,
        TICKS,
        pa.concat_tables([ticks(day=MON, sequences=(0, 1, 2, 3)), after_hours]),
    )

    first = await get_ticks(client, key, tw, trade_date=MON.isoformat(), page_size=3)
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["trade_date"], body["anchor"], body["dataset_key"]) == (
        MON.isoformat(),
        "explicit",
        TICKS,
    )
    assert [row["sequence"] for row in body["data"]] == [0, 1, 2]
    assert body["data"][0] == {
        "trade_date": MON.isoformat(),
        "trade_time": "2026-07-20T01:00:00Z",
        "sequence": 0,
        "session": "regular",
        "price": "100.0",
        "volume": 1,
        "tick_type": 1,
        "bid_price": "99.5",
        "bid_volume": 5,
        "ask_price": "100.5",
        "ask_volume": 7,
        "source": "finlab",
        "source_fetched_at": "2026-07-21T12:00:00Z",
    }
    second = await get_ticks(
        client,
        key,
        tw,
        trade_date=MON.isoformat(),
        page_size=3,
        cursor=body["pagination"]["next_cursor"],
    )
    assert [(row["sequence"], row["session"]) for row in second.json()["data"]] == [
        (3, "regular"),
        (4, "after_hours_fixed"),
    ]
    assert second.json()["pagination"]["next_cursor"] is None

    windowed = await get_ticks(
        client, key, tw, trade_date=MON.isoformat(), start_time="09:00:01", end_time="13:00:00"
    )
    assert [row["sequence"] for row in windowed.json()["data"]] == [2, 3]
    latest = await get_ticks(client, key, tw)
    assert (latest.json()["trade_date"], latest.json()["anchor"]) == (
        MON.isoformat(),
        "latest_available",
    )
    assert_error(
        await get_ticks(
            client, key, tw, trade_date=MON.isoformat(), start_time="10:00", end_time="09:00"
        ),
        422,
        "INVALID_TIME_RANGE",
    )
    assert_error(
        await get_ticks(
            client, key, tw, trade_date=TUE.isoformat(), cursor=body["pagination"]["next_cursor"]
        ),
        422,
        "INVALID_CURSOR",
    )
    empty = await get_ticks(client, key, tw, trade_date=TUE.isoformat())
    assert empty.status_code == 200 and empty.json()["data"] == []


# ---------------------------------------------------------------------------
# Access, limits, catalog and coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intraday_reads_need_a_key_and_an_intraday_dataset(
    client, test_session, stores, key, tw
) -> None:
    # A key is required even while the rest of Serve is open.
    assert get_settings().SERVE_REQUIRE_AUTH is False
    response = await client.get("/api/v1/serve/intraday/bars", params={"instrument_id": str(tw)})
    assert response.status_code == 401
    assert (await client.get("/api/v1/serve/datasets")).status_code == 200

    us_id = uuid7()
    test_session.add_all(
        [
            dataset("us_equity_eod", market="US", asset_class="equity", schema_id="market_eod"),
            instrument(us_id, "AAPL", market="US"),
        ]
    )
    await test_session.commit()
    assert_error(await get_bars(client, key, us_id), 422, "DATASET_NOT_AVAILABLE")
    assert_error(await get_ticks(client, key, us_id), 422, "DATASET_NOT_AVAILABLE")
    assert_error(await get_bars(client, key, uuid7()), 404, "INSTRUMENT_NOT_FOUND")


@pytest.mark.asyncio
async def test_intraday_reads_draw_on_their_own_weighted_allowance(
    client, test_session, stores, tw
) -> None:
    reset_source_rate_limit_state()
    _, plaintext = await create_api_key(
        test_session,
        owner="intraday-limit",
        tier="standard",
        scopes=["serve"],
        rate_limit_requests=3,
        rate_limit_window=60,
        page_size_limit=5000,
        kind="serve",
        name="intraday-limit",
        role=None,
        commit=False,
    )
    key = {get_settings().API_KEY_HEADER: plaintext}

    # A 2,500-row tick page costs three units; the next intraday read is refused.
    assert (await get_ticks(client, key, tw, page_size=2500)).status_code == 200
    refused = await get_bars(client, key, tw)
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "60"
    # The rest of Serve keeps its own allowance.
    assert (await client.get("/api/v1/serve/datasets", headers=key)).status_code == 200
    assert (await get_ticks(client, key, tw, page_size=5001)).status_code in {400, 422}


@pytest.mark.asyncio
async def test_catalog_and_instruments_report_intraday_coverage(
    client, test_session, stores, key, tw
) -> None:
    await stored_day(test_session, stores, BARS, bars(day=MON))
    await stored_day(test_session, stores, BARS, bars(day=WED))
    await stored_day(test_session, stores, TICKS, ticks(day=TUE))
    await stored_day(test_session, stores, BARS, bars(symbol="0050", day=TUE))

    catalog = {
        row["dataset_key"]: row
        for row in (await client.get("/api/v1/serve/datasets")).json()["data"]
    }
    assert catalog[BARS]["coverage"] == {
        "coverage_start_date": MON.isoformat(),
        "coverage_end_date": WED.isoformat(),
        "instrument_count": 2,
    }
    assert (catalog[BARS]["availability"], catalog[TICKS]["coverage"]["instrument_count"]) == (
        "available",
        1,
    )

    detail = (await client.get(f"/api/v1/serve/instruments/{tw}")).json()["data"]["coverage"]
    assert detail["intraday_bars"] == {
        "first_date": MON.isoformat(),
        "latest_date": WED.isoformat(),
    }
    assert detail["intraday_ticks"] == {
        "first_date": TUE.isoformat(),
        "latest_date": TUE.isoformat(),
    }
    listed = (await client.get("/api/v1/serve/instruments", params={"q": "2330"})).json()["data"]
    assert listed[0]["coverage"]["intraday_bars"]["latest_date"] == WED.isoformat()
