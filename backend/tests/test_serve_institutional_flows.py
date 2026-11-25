"""Serve reads of the TW exchanges' three institutional investors tables."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from app.models.institutional_flow import (
    InstitutionalFlowPublication,
    InstitutionalFlowStock,
    InstitutionalFlowSummary,
)
from app.models.registry import DatasetRegistry
from app.utils import uuid7
from tests.test_serve_api import assert_error, instrument

FRIDAY = date(2026, 11, 20)
THURSDAY = date(2026, 11, 19)
FETCHED = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
URL = "/api/v1/serve/institutional-flows"


def flow_dataset(key: str, schema_id: str, *sources: str) -> DatasetRegistry:
    return DatasetRegistry(
        dataset_key=key,
        name=key,
        asset_class="equity",
        market="TW",
        frequency="daily",
        is_active=True,
        config={
            "schema_id": schema_id,
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "enforce",
            "allowed_sources": list(sources),
            "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
        },
    )


def flow_datasets() -> list[DatasetRegistry]:
    return [
        flow_dataset("tw_twse_institutional_stock", "institutional_flow_stock", "twse"),
        flow_dataset("tw_tpex_institutional_stock", "institutional_flow_stock", "tpex"),
        flow_dataset("tw_twse_institutional_summary", "institutional_flow_summary", "twse"),
        flow_dataset("tw_tpex_institutional_summary", "institutional_flow_summary", "tpex"),
    ]


def publication(kind: str, board: str, day: date, rows: int) -> InstitutionalFlowPublication:
    return InstitutionalFlowPublication(
        publication_id=uuid7(),
        kind=kind,
        board=board,
        trade_date=day,
        dataset_key=f"tw_{board.lower()}_institutional_{kind}",
        source=board.lower(),
        source_symbol=None,
        source_fetched_at=FETCHED,
        source_raw_sha256="c" * 64,
        rows_sha256="d" * 64 if kind == "stock" else None,
        row_count=rows,
        created_at=FETCHED,
        updated_at=FETCHED,
    )


def security(
    owner: InstitutionalFlowPublication,
    symbol: str,
    foreign: int,
    *,
    instrument_id=None,
) -> InstitutionalFlowStock:
    """A row whose total equals its foreign net plus fixed dealer and trust flows."""
    tpex = owner.board == "TPEX"
    return InstitutionalFlowStock(
        trade_date=owner.trade_date,
        board=owner.board,
        symbol=symbol,
        publication_id=owner.publication_id,
        instrument_id=instrument_id,
        name=f"name {symbol}",
        foreign_ex_dealer_buy=max(foreign, 0),
        foreign_ex_dealer_sell=max(-foreign, 0),
        foreign_ex_dealer_net=foreign,
        foreign_dealer_buy=0,
        foreign_dealer_sell=0,
        foreign_dealer_net=0,
        foreign_total_buy=max(foreign, 0) if tpex else None,
        foreign_total_sell=max(-foreign, 0) if tpex else None,
        foreign_total_net=foreign if tpex else None,
        investment_trust_buy=10,
        investment_trust_sell=0,
        investment_trust_net=10,
        dealer_proprietary_buy=0,
        dealer_proprietary_sell=5,
        dealer_proprietary_net=-5,
        dealer_hedge_buy=0,
        dealer_hedge_sell=0,
        dealer_hedge_net=0,
        dealer_total_buy=0 if tpex else None,
        dealer_total_sell=5 if tpex else None,
        dealer_total_net=-5,
        total_net=foreign + 5,
    )


def amounts(owner, category: str, buy: int, sell: int) -> InstitutionalFlowSummary:
    return InstitutionalFlowSummary(
        trade_date=owner.trade_date,
        board=owner.board,
        category=category,
        publication_id=owner.publication_id,
        buy_amount=Decimal(buy),
        sell_amount=Decimal(sell),
        net_amount=Decimal(buy - sell),
        currency="TWD",
    )


@pytest.fixture
async def seeded(test_session) -> dict[str, Any]:
    tsmc = uuid7()
    test_session.add_all([*flow_datasets(), instrument(tsmc, "2330")])
    await test_session.commit()
    twse_friday = publication("stock", "TWSE", FRIDAY, 3)
    twse_thursday = publication("stock", "TWSE", THURSDAY, 1)
    tpex_friday = publication("stock", "TPEX", FRIDAY, 2)
    twse_totals = publication("summary", "TWSE", FRIDAY, 6)
    tpex_totals = publication("summary", "TPEX", FRIDAY, 8)
    test_session.add_all([twse_friday, twse_thursday, tpex_friday, twse_totals, tpex_totals])
    await test_session.flush()
    test_session.add_all(
        [
            security(twse_friday, "2330", 500, instrument_id=tsmc),
            security(twse_friday, "2317", -800),
            security(twse_friday, "030001", 50),
            security(twse_thursday, "2330", 100, instrument_id=tsmc),
            security(tpex_friday, "6488", 300),
            security(tpex_friday, "700024", 0),
        ]
    )
    for owner in (twse_totals, tpex_totals):
        leaves = {
            "foreign_ex_dealer": (300, 100),
            "foreign_dealer": (7, 2),
            "investment_trust": (50, 60),
            "dealer_proprietary": (20, 10),
            "dealer_hedge": (5, 1),
        }
        rows = [amounts(owner, category, *value) for category, value in leaves.items()]
        rows.append(amounts(owner, "total", 375, 171))
        if owner.board == "TPEX":
            rows.append(amounts(owner, "foreign_total", 307, 102))
            rows.append(amounts(owner, "dealer_total", 25, 11))
        test_session.add_all(rows)
    await test_session.commit()
    return {"tsmc": tsmc}


@pytest.mark.asyncio
async def test_each_board_table_is_listed_as_a_dataset(client: AsyncClient, seeded) -> None:
    response = await client.get("/api/v1/serve/datasets")
    assert response.status_code == 200, response.text
    rows = {row["dataset_key"]: row for row in response.json()["data"]}
    stock = rows["tw_twse_institutional_stock"]
    assert (stock["data_kind"], stock["operation_id"], stock["volume_unit"]) == (
        "institutional_flows",
        "list_institutional_flows",
        "share",
    )
    assert stock["coverage"] == {
        "coverage_start_date": "2026-11-19",
        "coverage_end_date": "2026-11-20",
        "instrument_count": 3,
    }
    summary = rows["tw_tpex_institutional_summary"]
    assert (summary["data_kind"], summary["availability"]) == (
        "institutional_flow_summary",
        "available",
    )


@pytest.mark.asyncio
async def test_securities_are_read_by_code_or_instrument(client: AsyncClient, seeded) -> None:
    by_code = await client.get(URL, params={"symbols": "2330,700024", "order": "asc"})
    assert by_code.status_code == 200, by_code.text
    body = by_code.json()
    assert body["unit"] == "share"
    assert [(row["trade_date"], row["board"], row["symbol"]) for row in body["data"]] == [
        ("2026-11-19", "TWSE", "2330"),
        ("2026-11-20", "TPEX", "700024"),
        ("2026-11-20", "TWSE", "2330"),
    ]
    tsmc = body["data"][2]
    assert tsmc["instrument"]["instrument_id"] == str(seeded["tsmc"])
    assert tsmc["foreign_total"] == {"buy": None, "sell": None, "net": None}
    assert tsmc["dealer_total"] == {"buy": None, "sell": None, "net": -5}
    assert tsmc["total_net"] == 505 and tsmc["source"] == "twse"
    warrant = body["data"][1]
    assert warrant["instrument"] is None and warrant["dealer_total"]["buy"] == 0

    by_instrument = await client.get(
        URL, params={"instruments": "TW:2330", "trade_date": "2026-11-20"}
    )
    assert [row["symbol"] for row in by_instrument.json()["data"]] == ["2330"]


@pytest.mark.asyncio
async def test_a_days_whole_table_is_ranked_by_a_net_flow(client: AsyncClient, seeded) -> None:
    params = {"trade_date": "2026-11-20", "sort": "total_net", "page_size": 2}
    first = await client.get(URL, params=params)
    assert first.status_code == 200, first.text
    assert [row["symbol"] for row in first.json()["data"]] == ["2330", "6488"]
    cursor = first.json()["pagination"]["next_cursor"]
    second = await client.get(URL, params={**params, "cursor": cursor})
    assert [row["symbol"] for row in second.json()["data"]] == ["030001", "700024"]
    third = await client.get(
        URL, params={**params, "cursor": second.json()["pagination"]["next_cursor"]}
    )
    assert [row["symbol"] for row in third.json()["data"]] == ["2317"]
    assert third.json()["pagination"]["next_cursor"] is None

    only_tpex = await client.get(URL, params={**params, "board": "TPEX", "order": "asc"})
    assert [row["symbol"] for row in only_tpex.json()["data"]] == ["700024", "6488"]
    assert_error(
        await client.get(URL, params={**params, "board": "TWSE", "cursor": cursor}),
        422,
        "INVALID_CURSOR",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, "QUERY_SCOPE_REQUIRED"),
        ({"symbols": "2330", "instruments": "TW:2330"}, "QUERY_SCOPE_REQUIRED"),
        ({"symbols": "2330", "sort": "total_net"}, "INVALID_SORT"),
        ({"symbols": "23-0"}, "INVALID_SYMBOLS"),
        ({"symbols": "2330,2330"}, "INVALID_SYMBOLS"),
        ({"trade_date": "2026-11-20", "start_date": "2026-11-01"}, "INVALID_DATE_RANGE"),
        ({"instruments": "TW:9999"}, "INSTRUMENT_NOT_FOUND"),
    ],
)
async def test_invalid_flow_queries_are_rejected(client: AsyncClient, seeded, params, code) -> None:
    response = await client.get(URL, params=params)
    assert response.status_code in {404, 422}
    assert response.json()["detail"]["code"] == code


@pytest.mark.asyncio
async def test_market_totals_add_the_subtotals_a_board_does_not_print(
    client: AsyncClient, seeded
) -> None:
    response = await client.get(f"{URL}/summary")
    assert response.status_code == 200, response.text
    rows = {row["board"]: row for row in response.json()["data"]}
    assert set(rows) == {"TWSE", "TPEX"}
    twse = rows["TWSE"]
    assert twse["currency"] == "TWD"
    assert twse["foreign_total"] == {
        "buy_amount": 307,
        "sell_amount": 102,
        "net_amount": 205,
        "reported": False,
    }
    assert twse["dealer_total"]["reported"] is False and twse["dealer_total"]["net_amount"] == 14
    assert twse["total"] == {
        "buy_amount": 375,
        "sell_amount": 171,
        "net_amount": 204,
        "reported": True,
    }
    assert rows["TPEX"]["foreign_total"]["reported"] is True
    paged = await client.get(f"{URL}/summary", params={"page_size": 1})
    cursor = paged.json()["pagination"]["next_cursor"]
    rest = await client.get(f"{URL}/summary", params={"page_size": 1, "cursor": cursor})
    assert {paged.json()["data"][0]["board"], rest.json()["data"][0]["board"]} == {"TWSE", "TPEX"}


@pytest.mark.asyncio
async def test_without_active_tables_the_reads_are_not_available(
    client: AsyncClient, test_session
) -> None:
    assert_error(await client.get(URL, params={"symbols": "2330"}), 422, "DATASET_NOT_AVAILABLE")
    assert_error(await client.get(f"{URL}/summary"), 422, "DATASET_NOT_AVAILABLE")
    test_session.add(flow_datasets()[0])
    await test_session.commit()
    assert_error(
        await client.get(URL, params={"symbols": "2330", "board": "TPEX"}),
        422,
        "DATASET_NOT_AVAILABLE",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sources", [("twse", "tpex"), ("fmp",), ()])
async def test_a_wrongly_declared_flow_dataset_fails_closed(
    client: AsyncClient, test_session, sources
) -> None:
    test_session.add(flow_dataset("bad", "institutional_flow_stock", *sources))
    await test_session.commit()
    assert_error(await client.get("/api/v1/serve/datasets"), 503, "SERVE_CONFIGURATION_INVALID")


@pytest.mark.asyncio
async def test_two_datasets_of_one_board_table_fail_closed(
    client: AsyncClient, test_session
) -> None:
    test_session.add_all(
        [
            flow_dataset("a", "institutional_flow_summary", "twse"),
            flow_dataset("b", "institutional_flow_summary", "twse"),
        ]
    )
    await test_session.commit()
    assert_error(await client.get("/api/v1/serve/datasets"), 503, "SERVE_CONFIGURATION_INVALID")
