"""Serve reads of HSIL index data: lists on a date, changes and HSICS classification."""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.index_reference import IndexMembership
from app.models.registry import DatasetRegistry
from app.services.normalize import index_reference
from tests.index_reference_support import (
    HSCI,
    HSCI_UTILITIES,
    HSI,
    HSI_FINANCE,
    change,
    changes_payload,
    constituent,
    constituents_payload,
)
from tests.intraday_support import Stores
from tests.test_index_reference_normalizers import (
    CHANGES,
    CONSTITUENTS,
    HSI_UTILITIES,
    config,
    deliver,
    hsi_report,
)
from tests.test_serve_api import assert_error


@pytest.fixture
def stores(monkeypatch) -> Stores:
    stores = Stores()
    monkeypatch.setattr(index_reference, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(index_reference, "canonical_publisher", lambda: stores.canonical)
    return stores


@pytest.fixture
async def datasets(test_session) -> None:
    """Both reference feeds active, as their migration installs them."""
    for key, schema_id in (
        (CONSTITUENTS, "index_constituents"),
        (CHANGES, "index_constituent_changes"),
    ):
        test_session.add(
            DatasetRegistry(
                dataset_key=key,
                name=key,
                asset_class="index",
                market="HK",
                frequency="daily",
                is_active=True,
                config=config(schema_id),
            )
        )
    await test_session.commit()


async def _hsi_history(test_session, stores) -> None:
    """A report of 2026-11-23 and HSI's changes: 00002 joined on 2026-09-07."""
    await deliver(test_session, stores, hsi_report())
    await deliver(
        test_session,
        stores,
        changes_payload(
            [
                change(1, "2026-09-07", "add", "00002", 2, count_change=1),
                change(2, "2008-10-08", "add", "00005", 1),
            ],
            index=HSI,
            history_start="2008-10-08",
            document_date=date(2026, 9, 7),
            series="hsi",
        ),
    )


@pytest.mark.asyncio
async def test_indexes_list_each_index_with_its_parent_and_coverage(
    client: AsyncClient, test_session, stores, datasets
):
    await _hsi_history(test_session, stores)

    response = await client.get("/api/v1/serve/indexes")

    assert response.status_code == 200, response.text
    rows = {row["code"]: row for row in response.json()["data"]}
    assert set(rows) == {"00001.00", "00001.01", "00001.02"}
    assert rows["00001.00"] | {} == {
        "code": "00001.00",
        "name_en": "Hang Seng Index",
        "name_zh": "恒生指數",
        "series": "hsi",
        "parent_code": None,
        "classification": None,
        "history_start": "2008-10-08",
        "observed_from": "2026-11-23",
        "latest_report_date": "2026-11-23",
        "constituent_count": 2,
    }
    assert rows["00001.01"]["parent_code"] == "00001.00"


@pytest.mark.asyncio
async def test_constituents_on_a_date_carry_the_latest_stored_weights(
    client: AsyncClient, test_session, stores, datasets
):
    await _hsi_history(test_session, stores)

    reported = await client.get(
        "/api/v1/serve/indexes/00001.00/constituents", params={"as_of": "2026-11-23"}
    )

    assert reported.status_code == 200, reported.text
    body = reported.json()
    assert (body["as_of"], body["derived"], body["weights_date"]) == (
        "2026-11-23",
        False,
        "2026-11-23",
    )
    assert [(row["symbol"], row["member_since"], row["weight_pct"]) for row in body["data"]] == [
        ("00002", "2026-09-07", "40.0000"),
        ("00005", "2008-10-08", "60.0000"),
    ]

    # Before the first report the list is rolled back from the changes, with no weights.
    rolled_back = await client.get(
        "/api/v1/serve/indexes/00001.00/constituents", params={"as_of": "2026-09-01"}
    )
    body = rolled_back.json()
    assert (body["derived"], body["weights_date"]) == (True, None)
    assert [row["symbol"] for row in body["data"]] == ["00005"]
    assert body["data"][0]["weight_pct"] is None

    # Nothing is served before the index's published history.
    before = await client.get(
        "/api/v1/serve/indexes/00001.00/constituents", params={"as_of": "2000-01-03"}
    )
    assert_error(before, 422, "DATE_BEFORE_HISTORY")
    assert_error(
        await client.get("/api/v1/serve/indexes/00009.00/constituents"), 404, "INDEX_NOT_FOUND"
    )
    assert_error(
        await client.get("/api/v1/serve/indexes/HSI/constituents"), 422, "INVALID_PARAMETER"
    )


@pytest.mark.asyncio
async def test_a_member_on_a_counter_keeps_its_weight_and_a_reused_code_its_own_name(
    client: AsyncClient, test_session, stores, datasets
):
    # 01440 trades as counter 02963 at the only report; 00017 named another stock before.
    await deliver(
        test_session,
        stores,
        changes_payload(
            [
                change(1, "2026-09-07", "add", "01440", 4, name_en="STAR SHINE HLDG"),
                change(2, "2024-01-02", "add", "00017", 3, name_en="NEW CO"),
                change(3, "2020-03-09", "delete", "00017", 2, name_en="OLD CO"),
                change(4, "2015-03-09", "add", "00017", 3, name_en="OLD CO"),
            ],
            index=HSI,
            history_start="2015-03-09",
            document_date=date(2026, 9, 7),
            series="hsi",
        ),
    )
    rows = [
        constituent("00001.00", "00005", "40"),
        constituent("00001.00", "00017", "30", name_en="NEW CO"),
        constituent("00001.00", "00002", "20"),
        constituent("00001.00", "02963", "10", name_en="STAR SHINE HLDG"),
        constituent("00001.01", "00005", "60"),
        constituent("00001.01", "00017", "40", name_en="NEW CO"),
        constituent("00001.02", "00002", "70"),
        constituent("00001.02", "02963", "30", name_en="STAR SHINE HLDG"),
    ]
    indexes = [
        {**HSI, "constituent_count": 4},
        {**HSI_FINANCE, "constituent_count": 2},
        {**HSI_UTILITIES, "constituent_count": 2},
    ]
    await deliver(test_session, stores, constituents_payload(rows, indexes))

    response = await client.get(
        "/api/v1/serve/indexes/00001.00/constituents", params={"as_of": "2026-11-23"}
    )

    assert response.status_code == 200, response.text
    assert [
        (row["symbol"], row["name_en"], row["member_since"], row["weight_pct"])
        for row in response.json()["data"]
    ] == [
        ("00002", "STOCK 00002", None, "20.0000"),
        ("00005", "STOCK 00005", None, "40.0000"),
        ("00017", "NEW CO", "2024-01-02", "30.0000"),
        ("01440", "STAR SHINE HLDG", "2026-09-07", "10.0000"),
    ]
    earlier = await test_session.scalars(
        select(IndexMembership)
        .where(IndexMembership.symbol == "00017")
        .order_by(IndexMembership.valid_from)
    )
    assert [
        (row.name_en, row.valid_from, row.valid_to)
        for row in earlier
        if row.valid_from and row.valid_from.year < 2024
    ] == [("OLD CO", date(2015, 3, 9), date(2020, 3, 9))]


@pytest.mark.asyncio
async def test_changes_page_newest_first(client: AsyncClient, test_session, stores, datasets):
    await _hsi_history(test_session, stores)

    first = await client.get("/api/v1/serve/indexes/00001.00/changes", params={"page_size": 1})

    assert first.status_code == 200, first.text
    body = first.json()
    assert [(row["effective_date"], row["action"], row["symbol"]) for row in body["data"]] == [
        ("2026-09-07", "add", "00002")
    ]
    cursor = body["pagination"]["next_cursor"]
    assert cursor
    second = await client.get(
        "/api/v1/serve/indexes/00001.00/changes", params={"page_size": 1, "cursor": cursor}
    )
    body = second.json()
    assert [row["symbol"] for row in body["data"]] == ["00005"]
    assert body["pagination"]["next_cursor"] is None
    # A cursor belongs to its own query.
    other = await client.get(
        "/api/v1/serve/indexes/00001.00/changes",
        params={"page_size": 1, "cursor": cursor, "start_date": "2020-01-01"},
    )
    assert_error(other, 422, "INVALID_CURSOR")
    since = await client.get(
        "/api/v1/serve/indexes/00001.00/changes", params={"start_date": "2020-01-01"}
    )
    assert [row["symbol"] for row in since.json()["data"]] == ["00002"]


@pytest.mark.asyncio
async def test_hsics_classifies_hsci_constituents_by_their_industry_index(
    client: AsyncClient, test_session, stores, datasets
):
    report = constituents_payload(
        [
            constituent(
                "00011.00",
                "00002",
                "100",
                industry={"scheme": "hsics", "code": "40", "name_en": "Utilities"},
            ),
            constituent("00011.07", "00002", "100"),
        ],
        [{**HSCI, "constituent_count": 1}, {**HSCI_UTILITIES, "constituent_count": 1}],
        series="hsci",
    )
    await deliver(test_session, stores, report)

    response = await client.get(
        "/api/v1/serve/classifications/hsics", params={"as_of": "2026-11-23"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["derived"] is False
    assert [
        (row["symbol"], row["index_code"], row["industry"]["code"]) for row in body["data"]
    ] == [("00002", "00011.07", "40")]
    filtered = await client.get(
        "/api/v1/serve/classifications/hsics",
        params={"as_of": "2026-11-23", "industry": "50"},
    )
    assert filtered.json()["data"] == []
    by_symbol = await client.get(
        "/api/v1/serve/classifications/hsics", params={"as_of": "2026-11-23", "symbols": "2"}
    )
    assert [row["symbol"] for row in by_symbol.json()["data"]] == ["00002"]
    assert_error(
        await client.get("/api/v1/serve/classifications/hsics", params={"symbols": "2,00002"}),
        422,
        "INVALID_SYMBOLS",
    )
    assert_error(
        await client.get("/api/v1/serve/classifications/hsics", params={"industry": "5"}),
        422,
        "INVALID_PARAMETER",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/serve/indexes",
        "/api/v1/serve/indexes/00001.00/constituents",
        "/api/v1/serve/indexes/00001.00/changes",
        "/api/v1/serve/classifications/hsics",
    ],
)
async def test_index_reads_need_their_feed_active(client: AsyncClient, url):
    assert_error(await client.get(url), 422, "DATASET_NOT_AVAILABLE")
