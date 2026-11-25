"""The exchanges' three institutional investors tables: contracts, storage and evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.canonical import Instrument
from app.models.institutional_flow import (
    InstitutionalFlowPublication,
    InstitutionalFlowStock,
    InstitutionalFlowSummary,
)
from app.models.registry import DatasetRegistry, DeliveryMemberEvidence, DQIssue, IngestionRun
from app.schemas.ingress import INSTITUTIONAL_STOCK_COLUMNS
from app.services.delivery_evidence import record_delivery_evidence
from app.services.eod_completion import MEMBER_EOD_SCHEMAS, ROW_DQ_ERRORS
from app.services.ingestion import CONTRACT_NORMALIZER_MAP, IngestionService
from app.services.ingress_contracts import get_contract_json_schema, validate_ingress_request
from app.services.normalize import intraday
from app.services.normalize.institutional_flow import (
    InstitutionalFlowStockNormalizer,
    InstitutionalFlowSummaryNormalizer,
)
from app.utils import utc_now, uuid7
from tests.intraday_support import ACCOUNT, RAW_BUCKET, Stores

DAY = date(2026, 11, 20)
FETCHED = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
SHA = "b" * 64
DATASETS = {
    ("twse", "summary"): "tw_twse_institutional_summary",
    ("tpex", "summary"): "tw_tpex_institutional_summary",
    ("twse", "stock"): "tw_twse_institutional_stock",
    ("tpex", "stock"): "tw_tpex_institutional_stock",
}
SCHEMAS = {"summary": "institutional_flow_summary", "stock": "institutional_flow_stock"}


def _config(kind: str, source: str) -> dict[str, Any]:
    return {
        "schema_id": SCHEMAS[kind],
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": [source],
        "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
    }


# TWSE's 2026-11-20 totals (BFI82U), in TWD.
TWSE_SUMMARY = [
    {"category": "dealer_proprietary", "buy": 8_278_917_111, "sell": 13_346_755_851},
    {"category": "dealer_hedge", "buy": 54_348_015_663, "sell": 83_363_417_043},
    {"category": "investment_trust", "buy": 24_145_010_475, "sell": 31_492_002_478},
    {"category": "foreign_ex_dealer", "buy": 337_268_103_086, "sell": 346_778_646_658},
    {"category": "foreign_dealer", "buy": 0, "sell": 0},
    {"category": "total", "buy": 424_040_046_335, "sell": 474_980_822_030},
]


def _categories(rows=TWSE_SUMMARY) -> list[dict[str, Any]]:
    return [
        {
            "category": row["category"],
            "buy_amount": row["buy"],
            "sell_amount": row["sell"],
            "net_amount": row.get("net", row["buy"] - row["sell"]),
        }
        for row in rows
    ]


def _batch(**changes: Any) -> dict[str, Any]:
    return {
        "data_date": DAY.isoformat(),
        "delivery_mode": "incremental",
        "declared_record_count": 1,
        "source_raw_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/twse/x/{SHA}.json",
        "source_raw_sha256": SHA,
        **changes,
    }


def _summary_payload(board: str = "TWSE", categories=None) -> dict[str, Any]:
    return {
        "batch": _batch(),
        "data": [
            {
                "symbol": board,
                "source_symbol": "BFI82U",
                "trade_date": DAY.isoformat(),
                "currency": "TWD",
                "categories": categories if categories is not None else _categories(),
            }
        ],
    }


def _request(kind: str, source: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_key": DATASETS[(source, kind)],
        "schema_id": SCHEMAS[kind],
        "schema_version": 1,
        "source": source,
        "request_key": "full:x",
        "idempotency_key": "full:x",
        "fetched_at": FETCHED.isoformat(),
        "payload": payload,
    }


def _security(symbol: str, **values: int | None) -> list[Any]:
    """One rows-object row whose relations hold; ``values`` override leaf flows."""
    flows: dict[str, int | None] = {
        "foreign_ex_dealer_buy": 1000,
        "foreign_ex_dealer_sell": 400,
        "foreign_dealer_buy": 0,
        "foreign_dealer_sell": 0,
        "investment_trust_buy": 50,
        "investment_trust_sell": 80,
        "dealer_proprietary_buy": 20,
        "dealer_proprietary_sell": 5,
        "dealer_hedge_buy": 7,
        "dealer_hedge_sell": 9,
    }
    flows.update(values)
    row: dict[str, Any] = {"symbol": symbol, "name": f"name {symbol}", **flows}
    for prefix in (
        "foreign_ex_dealer",
        "foreign_dealer",
        "investment_trust",
        "dealer_proprietary",
        "dealer_hedge",
    ):
        row[f"{prefix}_net"] = row[f"{prefix}_buy"] - row[f"{prefix}_sell"]
    row["foreign_total_buy"] = row["foreign_total_sell"] = row["foreign_total_net"] = None
    row["dealer_total_buy"] = row["dealer_total_sell"] = None
    row["dealer_total_net"] = row["dealer_proprietary_net"] + row["dealer_hedge_net"]
    row["total_net"] = (
        row["foreign_ex_dealer_net"]
        + row["investment_trust_net"]
        + row["dealer_proprietary_net"]
        + row["dealer_hedge_net"]
    )
    return [row[column.name] for column in INSTITUTIONAL_STOCK_COLUMNS]


def _rows_object(rows: list[list[Any]]) -> bytes:
    columns = [column.name for column in INSTITUTIONAL_STOCK_COLUMNS]
    return json.dumps({"columns": columns, "rows": rows}, separators=(",", ":")).encode()


@pytest.fixture
def stores(monkeypatch) -> Stores:
    stores = Stores()
    monkeypatch.setattr(intraday, "raw_reader", lambda: stores.raw)
    return stores


@pytest.fixture
async def datasets(test_session) -> None:
    for (source, kind), key in DATASETS.items():
        test_session.add(
            DatasetRegistry(
                dataset_key=key,
                name=key,
                asset_class="equity",
                market="TW",
                frequency="daily",
                is_active=True,
                config=_config(kind, source),
            )
        )
    await test_session.commit()


async def _run(test_session, kind: str, source: str) -> IngestionRun:
    run = await IngestionService(test_session).create_ingestion_run(
        DATASETS[(source, kind)],
        source=source,
        raw_records=1,
        schema_id=SCHEMAS[kind],
        schema_version=1,
        batch_data_date=DAY,
        delivery_mode="incremental",
    )
    await test_session.commit()
    return run


async def deliver_summary(test_session, payload=None, *, fetched_at=FETCHED, source="twse"):
    run = await _run(test_session, "summary", source)
    normalizer = InstitutionalFlowSummaryNormalizer(
        test_session,
        {**_config("summary", source), "_ingest_source": source, "_ingest_fetched_at": fetched_at},
    )
    result = await normalizer.process(payload or _summary_payload(), run.run_id)
    return run.run_id, result


async def deliver_stock(
    test_session,
    stores: Stores,
    rows: list[list[Any]] | bytes,
    *,
    fetched_at=FETCHED,
    source="twse",
    row_count: int | None = None,
):
    body = rows if isinstance(rows, bytes) else _rows_object(rows)
    digest = hashlib.sha256(body).hexdigest()
    key = f"{source}/{DATASETS[(source, 'stock')]}/{digest}.rows.json"
    stores.r2.objects[(RAW_BUCKET, key)] = body
    payload = {
        "batch": _batch(),
        "data": [
            {
                "symbol": source.upper(),
                "source_symbol": "T86",
                "trade_date": DAY.isoformat(),
                "currency": "TWD",
                "row_count": row_count if row_count is not None else len(rows),
                "rows_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/{key}",
                "rows_sha256": digest,
                "rows_bytes": len(body),
                "rows_format": "json",
                "volume_unit": "share",
            }
        ],
    }
    run = await _run(test_session, "stock", source)
    normalizer = InstitutionalFlowStockNormalizer(
        test_session,
        {**_config("stock", source), "_ingest_source": source, "_ingest_fetched_at": fetched_at},
    )
    result = await normalizer.process(payload, run.run_id)
    return run.run_id, result


async def _issues(test_session, run_id) -> list[DQIssue]:
    return list(await test_session.scalars(select(DQIssue).where(DQIssue.run_id == run_id)))


def test_contracts_are_registered_routed_and_settled_member_by_member() -> None:
    for kind, schema_id in SCHEMAS.items():
        assert (schema_id, 1) in CONTRACT_NORMALIZER_MAP
        assert schema_id in MEMBER_EOD_SCHEMAS
        schema = get_contract_json_schema(schema_id, 1)
        rules = {rule["id"] for rule in schema["x-findb-semantic-rules"]}
        assert {"table.single_trading_day", "table.board_is_source", "table.identities"} <= rules
    assert "INSTITUTIONAL_TABLE_REJECTED" in ROW_DQ_ERRORS
    layout = get_contract_json_schema("institutional_flow_stock", 1)["x-findb-row-layout"]
    assert [column["name"] for column in layout["columns"]][:3] == [
        "symbol",
        "name",
        "foreign_ex_dealer_buy",
    ]


def test_a_summary_request_validates_and_its_table_shape_is_enforced() -> None:
    validate_ingress_request(_request("summary", "twse", _summary_payload()))
    missing_total = _summary_payload(categories=_categories(TWSE_SUMMARY[:-1]))
    repeated = _summary_payload(categories=_categories([*TWSE_SUMMARY, TWSE_SUMMARY[0]]))
    for payload in (missing_total, repeated):
        with pytest.raises(ValidationError):
            validate_ingress_request(_request("summary", "twse", payload))
    # Each exchange publishes only its own board.
    with pytest.raises(ValidationError, match="board must be the source"):
        validate_ingress_request(_request("summary", "tpex", _summary_payload("TWSE")))
    other_day = _summary_payload()
    other_day["data"][0]["trade_date"] = (DAY - timedelta(days=1)).isoformat()
    with pytest.raises(ValidationError, match="trade_date"):
        validate_ingress_request(_request("summary", "twse", other_day))
    with pytest.raises(ValidationError):
        validate_ingress_request(
            {**_request("summary", "twse", _summary_payload()), "source": "fmp"}
        )


async def test_a_summary_is_stored_and_records_its_board_as_written(test_session, datasets) -> None:
    run_id, result = await deliver_summary(test_session)

    assert (result.success_records, result.failed_records) == (1, 0)
    rows = {
        row.category: row
        for row in await test_session.scalars(
            select(InstitutionalFlowSummary).where(InstitutionalFlowSummary.trade_date == DAY)
        )
    }
    assert set(rows) == {item["category"] for item in TWSE_SUMMARY}
    assert int(rows["total"].net_amount) == -50_940_775_695
    publication = await test_session.scalar(select(InstitutionalFlowPublication))
    assert (publication.kind, publication.board, publication.run_id) == ("summary", "TWSE", run_id)
    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed"
    await record_delivery_evidence(test_session, run)
    evidence = set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(DeliveryMemberEvidence.run_id == run_id)
        )
    )
    assert evidence == {"TWSE"}


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"category": "investment_trust", "buy": 10, "sell": 5, "net": 4}, "net_is_buy_minus_sell"),
        ({"category": "dealer_hedge", "buy": 1, "sell": 0}, "total_is_sum"),
        ({"category": "foreign_dealer", "buy": 5, "sell": 0, "skip_total": True}, None),
    ],
)
async def test_a_summary_breaking_a_printed_relation_is_rejected_whole(
    test_session, datasets, change, rule
) -> None:
    rows = [dict(row) for row in TWSE_SUMMARY]
    target = next(row for row in rows if row["category"] == change["category"])
    target.update({key: value for key, value in change.items() if key in {"buy", "sell", "net"}})
    run_id, result = await deliver_summary(
        test_session, _summary_payload(categories=_categories(rows))
    )

    if rule is None:
        # Foreign dealers are not part of the exchanges' total: the table holds.
        assert result.failed_records == 0
        return
    assert (result.success_records, result.failed_records) == (0, 1)
    [issue] = await _issues(test_session, run_id)
    assert issue.issue_type == "INSTITUTIONAL_TABLE_REJECTED" and issue.severity == "error"
    assert issue.raw_data["symbol"] == "TWSE" and issue.raw_data["rule"] == rule
    assert await test_session.scalar(select(InstitutionalFlowSummary)) is None
    assert await test_session.scalar(select(InstitutionalFlowPublication)) is None


async def test_a_later_fetch_replaces_the_table_and_an_earlier_one_is_rejected(
    test_session, datasets
) -> None:
    await deliver_summary(test_session)
    rows = [dict(row) for row in TWSE_SUMMARY]
    rows[0].update(buy=rows[0]["buy"] + 1)
    rows[-1].update(buy=rows[-1]["buy"] + 1)
    later = FETCHED + timedelta(hours=1)
    _, replaced = await deliver_summary(
        test_session, _summary_payload(categories=_categories(rows)), fetched_at=later
    )
    assert replaced.success_records == 1
    stale_id, stale = await deliver_summary(test_session, fetched_at=FETCHED)
    assert (stale.success_records, stale.precedence_rejected_records) == (0, 1)
    assert [issue.issue_type for issue in await _issues(test_session, stale_id)] == [
        "INSTITUTIONAL_TABLE_STALE"
    ]
    test_session.expire_all()
    total = await test_session.get(InstitutionalFlowSummary, (DAY, "TWSE", "total"))
    assert int(total.buy_amount) == TWSE_SUMMARY[-1]["buy"] + 1
    assert len(list(await test_session.scalars(select(InstitutionalFlowPublication)))) == 1


async def _instrument(test_session, symbol: str, asset_class: str = "equity") -> Instrument:
    instrument = Instrument(
        instrument_id=uuid7(),
        market="TW",
        asset_class=asset_class,
        symbol=symbol,
        currency="TWD",
        timezone="Asia/Taipei",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(instrument)
    await test_session.commit()
    return instrument


async def test_every_security_is_stored_and_listed_ones_are_linked(
    test_session, datasets, stores
) -> None:
    listed = await _instrument(test_session, "2330")
    await _instrument(test_session, "0050", "etf")
    # Listed as both an equity and an ETF: stored unlinked, with a warning.
    await _instrument(test_session, "9999")
    await _instrument(test_session, "9999", "etf")
    rows = [_security("2330"), _security("0050"), _security("030001"), _security("9999")]

    run_id, result = await deliver_stock(test_session, stores, rows)

    assert (result.success_records, result.failed_records) == (1, 0)
    stored = {
        row.symbol: row
        for row in await test_session.scalars(
            select(InstitutionalFlowStock).where(InstitutionalFlowStock.trade_date == DAY)
        )
    }
    assert set(stored) == {"2330", "0050", "030001", "9999"}
    assert stored["2330"].instrument_id == listed.instrument_id
    assert stored["0050"].instrument_id is not None
    assert stored["030001"].instrument_id is None  # a warrant has no instrument
    assert stored["9999"].instrument_id is None
    assert stored["2330"].total_net == 600 - 30 + 15 - 2
    assert stored["2330"].foreign_total_net is None and stored["2330"].dealer_total_buy is None
    [warning] = await _issues(test_session, run_id)
    assert warning.issue_type == "INSTITUTIONAL_INSTRUMENT_AMBIGUOUS"
    assert warning.raw_data["securities"] == ["9999"]
    run = await test_session.get(IngestionRun, run_id)
    await record_delivery_evidence(test_session, run)
    assert set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(DeliveryMemberEvidence.run_id == run_id)
        )
    ) == {"TWSE"}


@pytest.mark.parametrize(
    ("rows", "row_count", "rule"),
    [
        ([_security("2330"), _security("2317", investment_trust_sell=81)], None, None),
        ([_security("2330"), _security("2330")], None, "security_repeated"),
        ([_security("2330")], 2, "row_count"),
        ([_security("23")], None, "security_code"),
        (b'{"columns": ["symbol"], "rows": []}', 1, "rows_object_layout"),
        (b"not json", 1, "rows_object_json"),
    ],
)
async def test_a_stock_table_breaking_a_rule_stores_nothing(
    test_session, datasets, stores, rows, row_count, rule
) -> None:
    if rule is None:
        # Break a relation after the row was built consistently.
        broken = list(rows[1])
        names = [column.name for column in INSTITUTIONAL_STOCK_COLUMNS]
        broken[names.index("total_net")] += 1
        rows = [rows[0], broken]
        rule = "total_is_sum"
    run_id, result = await deliver_stock(test_session, stores, rows, row_count=row_count)

    assert (result.success_records, result.failed_records) == (0, 1)
    [issue] = await _issues(test_session, run_id)
    assert issue.issue_type == "INSTITUTIONAL_TABLE_REJECTED"
    assert issue.raw_data["rule"] == rule and issue.raw_data["symbol"] == "TWSE"
    assert await test_session.scalar(select(InstitutionalFlowStock)) is None


async def test_a_tpex_stock_table_keeps_its_printed_subtotals(
    test_session, datasets, stores
) -> None:
    row = _security("6488", foreign_dealer_buy=3, foreign_dealer_sell=1)
    names = [column.name for column in INSTITUTIONAL_STOCK_COLUMNS]
    values = dict(zip(names, row, strict=True))
    values.update(
        foreign_total_buy=1003,
        foreign_total_sell=401,
        foreign_total_net=602,
        dealer_total_buy=27,
        dealer_total_sell=14,
    )
    _, result = await deliver_stock(
        test_session, stores, [[values[name] for name in names]], source="tpex"
    )
    assert result.success_records == 1
    stored = await test_session.get(InstitutionalFlowStock, (DAY, "TPEX", "6488"))
    assert (stored.foreign_total_net, stored.dealer_total_buy) == (602, 27)

    values["foreign_total_buy"] = 1004
    _, rejected = await deliver_stock(
        test_session,
        stores,
        [[values[name] for name in names]],
        source="tpex",
        fetched_at=FETCHED + timedelta(hours=1),
    )
    assert rejected.failed_records == 1


async def test_a_missing_rows_object_rejects_the_table(test_session, datasets, stores) -> None:
    body = _rows_object([_security("2330")])
    digest = hashlib.sha256(body).hexdigest()
    payload = {
        "batch": _batch(),
        "data": [
            {
                "symbol": "TWSE",
                "trade_date": DAY.isoformat(),
                "currency": "TWD",
                "row_count": 1,
                "rows_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/twse/missing/{digest}.rows.json",
                "rows_sha256": digest,
                "rows_bytes": len(body),
                "rows_format": "json",
                "volume_unit": "share",
            }
        ],
    }
    run = await _run(test_session, "stock", "twse")
    result = await InstitutionalFlowStockNormalizer(
        test_session,
        {**_config("stock", "twse"), "_ingest_source": "twse", "_ingest_fetched_at": FETCHED},
    ).process(payload, run.run_id)
    assert result.failed_records == 1
    [issue] = await _issues(test_session, run.run_id)
    assert issue.raw_data["rule"] == "rows_object"
