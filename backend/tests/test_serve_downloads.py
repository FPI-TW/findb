"""Intraday download manifests: presigned objects, the dates to take, a daily quota."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import reset_source_rate_limit_state
from app.config import get_settings
from app.models.registry import IntradayDownloadUsage
from app.services import intraday_partitions as store
from app.services import serve_downloads, serve_intraday
from app.services.api_keys import create_api_key, rotate_api_key
from app.services.object_storage import ObjectStorageConfigError
from app.utils import utc_now
from tests.intraday_support import FETCHED, Stores, bars, ticks
from tests.test_serve_intraday import BARS, MON, TICKS, TUE, WED, intraday_instrument, stored_day

URL = "/api/v1/serve/intraday/downloads"


@pytest.fixture
async def tw(test_session):
    return await intraday_instrument(test_session)


@pytest.fixture
def stores(monkeypatch) -> Iterator[Stores]:
    stores = Stores()
    serve_intraday.reset_read_state()
    monkeypatch.setattr(serve_intraday, "canonical_reader", lambda: stores.canonical)
    monkeypatch.setattr(serve_downloads, "canonical_reader", lambda: stores.canonical)
    yield stores
    serve_intraday.reset_read_state()


async def download_key(session, *, scopes=("serve", "intraday_download"), limit=5):
    reset_source_rate_limit_state()
    row, plaintext = await create_api_key(
        session,
        owner="research",
        tier="standard",
        scopes=list(scopes),
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        intraday_download_daily_limit=limit,
        kind="serve",
        name="research",
        role=None,
        commit=False,
    )
    return row, {get_settings().API_KEY_HEADER: plaintext}


async def manifest(client, headers, instrument_id, **params):
    return await client.get(
        URL,
        params={"instrument_id": str(instrument_id), "month": "2026-07", **params},
        headers=headers,
    )


async def used(session, key_id) -> int | None:
    return await session.scalar(
        select(IntradayDownloadUsage.object_count).where(IntradayDownloadUsage.key_id == key_id)
    )


@pytest.mark.asyncio
async def test_a_manifest_names_each_object_and_the_only_days_to_take_from_it(
    client, test_session, stores, tw
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
    # Republished after the merge: the month object still holds Tuesday's old rows.
    tuesday = await stored_day(
        test_session,
        stores,
        BARS,
        bars(day=TUE, minutes=(9,)),
        fetched_at=FETCHED + timedelta(days=30),
    )
    key, headers = await download_key(test_session)
    stores.r2.calls.clear()

    response = await manifest(client, headers, tw)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["dataset_key"], body["month"]) == (BARS, "2026-07")
    assert body["units"]["volume"] == "lot"
    assert body["layout"]["schema_id"] == "intraday_partition"
    assert body["layout"]["record_kind"] == "bar_1m"
    assert [column["name"] for column in body["layout"]["columns"]][:3] == [
        "symbol",
        "trade_date",
        "bar_start_time",
    ]
    month_key = store.month_key(BARS, "2330", 2026, 7)
    assert [
        (item["object_layout"], item["url"].split("?")[0].split("/", 4)[-1], item["trade_dates"])
        for item in body["objects"]
    ] == [
        (
            "month",
            month_key,
            [
                {"trade_date": MON.isoformat(), "row_count": 3},
                {"trade_date": WED.isoformat(), "row_count": 3},
            ],
        ),
        (
            "day",
            store.day_key(BARS, "2330", TUE),
            [{"trade_date": TUE.isoformat(), "row_count": 1}],
        ),
    ]
    assert body["objects"][1]["object_etag"] == tuesday.object_etag
    assert body["quota"] == {
        "usage_date": utc_now().date().isoformat(),
        "daily_limit": 5,
        "used": 2,
        "remaining": 3,
    }
    # URLs are signed locally: nothing is read or listed, and each lives 15 minutes.
    assert {name for name, _ in stores.r2.calls} == {"presign"}
    assert {arguments["ExpiresIn"] for _, arguments in stores.r2.calls} == {900}
    assert await used(test_session, key.key_id) == 2


@pytest.mark.asyncio
async def test_the_daily_quota_refuses_a_manifest_it_cannot_cover_in_full(
    client, test_session, stores, tw
) -> None:
    for day in (MON, TUE):
        await stored_day(test_session, stores, BARS, bars(day=day))
    key, headers = await download_key(test_session, limit=5)
    key_id = key.key_id  # a refused charge rolls the session back and expires ``key``

    assert (await manifest(client, headers, tw)).json()["quota"]["used"] == 2
    assert (await manifest(client, headers, tw)).json()["quota"]["remaining"] == 1

    refused = await manifest(client, headers, tw)

    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == "DOWNLOAD_QUOTA_EXCEEDED"
    assert 0 < int(refused.headers["Retry-After"]) <= 86400
    assert await used(test_session, key_id) == 4
    # A month without data needs nothing and still answers.
    empty = await manifest(client, headers, tw, month="2026-06")
    assert empty.status_code == 200 and empty.json()["objects"] == []
    assert empty.json()["quota"]["remaining"] == 1


@pytest.mark.asyncio
async def test_concurrent_manifests_of_one_key_never_exceed_its_limit(test_engine) -> None:
    sessions = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        key, _ = await download_key(session, limit=3)
        await session.commit()
    today = utc_now().date()

    async def charge() -> bool:
        async with sessions() as session:
            try:
                await serve_downloads._charge(session, key.key_id, 2, today)
            except HTTPException as exc:
                assert exc.status_code == 429
                return False
            return True

    results = await asyncio.gather(*(charge() for _ in range(4)))

    assert sorted(results) == [False, False, False, True]
    async with sessions() as session:
        assert await used(session, key.key_id) == 2


@pytest.mark.asyncio
async def test_only_keys_with_the_download_scope_get_a_manifest(
    client, test_session, stores, tw
) -> None:
    assert (
        await client.get(URL, params={"instrument_id": str(tw), "month": "2026-07"})
    ).status_code == 401
    _, plain = await download_key(test_session, scopes=("serve",))
    refused = await manifest(client, plain, tw)
    assert refused.status_code == 403
    assert "intraday_download" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_ticks_months_and_storage_errors_are_checked_before_any_charge(
    client, test_session, stores, tw, monkeypatch
) -> None:
    await stored_day(test_session, stores, TICKS, ticks(day=MON))
    key, headers = await download_key(test_session)

    tick_manifest = await manifest(client, headers, tw, kind="ticks")
    assert tick_manifest.status_code == 200, tick_manifest.text
    assert tick_manifest.json()["dataset_key"] == TICKS
    assert tick_manifest.json()["layout"]["record_kind"] == "tick"
    assert len(tick_manifest.json()["objects"]) == 1

    for month in ("2026-13", "202607", "2026-7x"):
        response = await manifest(client, headers, tw, month=month)
        assert response.status_code == 422, month

    def unconfigured():
        raise ObjectStorageConfigError("R2 credential is not configured for this role")

    monkeypatch.setattr(serve_downloads, "canonical_reader", unconfigured)
    unavailable = await manifest(client, headers, tw, kind="ticks")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "INTRADAY_STORAGE_UNAVAILABLE"
    assert await used(test_session, key.key_id) == 1


@pytest.mark.asyncio
async def test_the_daily_limit_is_set_at_creation_and_kept_on_rotation(test_session) -> None:
    key, _ = await download_key(test_session, limit=1234)
    rotated, _ = await rotate_api_key(test_session, key, commit=False)
    assert (key.intraday_download_daily_limit, rotated.intraday_download_daily_limit) == (
        1234,
        1234,
    )
