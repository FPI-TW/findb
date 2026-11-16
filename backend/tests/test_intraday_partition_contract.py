"""``intraday_partition.v1``: one symbol's trading day delivered as a stored object."""

from __future__ import annotations

import copy
import hashlib
from datetime import date
from unittest.mock import patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import get_settings
from app.models.canonical import IntradayPartition
from app.models.raw import RawMarketPayload
from app.models.registry import DatasetRegistry, IngestionAttempt, IngestionRun, NormalizationJob
from app.schemas.ingress import IntradayPartitionIngressRequest
from app.services.ingress_contracts import (
    DatasetContractDeclaration,
    get_contract_json_schema,
    validate_ingress_request,
)
from app.services.normalization_queue import execute_normalization
from app.services.normalize import intraday
from app.services.source_clients import PROVIDER_DATASET_SCOPE, create_source_client
from tests.intraday_support import RAW_BUCKET, Stores, bars, parquet_bytes

ACCOUNT = "a" * 32
DIGEST = "b" * 64
ROWS_DIGEST = "c" * 64
PREFIX = f"r2://{ACCOUNT}/findb-test-raw/finlab/tw_equity_bar_1m/0123456789abcdef"
REF = f"{PREFIX}/{DIGEST}.parquet"
ROWS_REF = f"{PREFIX}/{ROWS_DIGEST}.rows.parquet"


def _request(**partition) -> dict:
    return {
        "dataset_key": "tw_equity_bar_1m",
        "schema_id": "intraday_partition",
        "schema_version": 1,
        "source": "finlab",
        "request_key": "finlab:tw_equity_bar_1m:2330:2026-07-21",
        "idempotency_key": f"finlab:tw_equity_bar_1m:2330:2026-07-21:{ROWS_DIGEST[:16]}",
        "fetched_at": "2026-07-21T12:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2026-07-21",
                "delivery_mode": "incremental",
                "declared_record_count": 1,
                "source_raw_ref": REF,
                "source_raw_sha256": DIGEST,
            },
            "data": [
                {
                    "symbol": "2330",
                    "source_symbol": "2330",
                    "trade_date": "2026-07-21",
                    "record_kind": "bar_1m",
                    "row_count": 266,
                    "rows_ref": ROWS_REF,
                    "rows_sha256": ROWS_DIGEST,
                    "rows_bytes": 14_302,
                    "rows_format": "parquet",
                    "price_adjustment": "none",
                    "volume_unit": "lot",
                    **partition,
                }
            ],
        },
    }


def _changed(path: str, value) -> dict:
    """The valid request with one value replaced; ``...`` removes the field."""
    request = _request()
    target = request
    *parents, leaf = path.split(".")
    for name in parents:
        target = target[int(name)] if name.isdigit() else target[name]
    if value is ...:
        del target[leaf]
    else:
        target[leaf] = value
    return request


def _dataset_config(record_kind: str = "bar_1m") -> dict:
    return {
        "schema_id": "intraday_partition",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": ["finlab"],
        "defaults": {
            "market": "TW",
            "asset_class": "equity",
            "currency": "TWD",
            "market_timezone": "Asia/Taipei",
            "price_adjustment": "none",
            "record_kind": record_kind,
        },
    }


def _published_schema_errors(request: dict) -> list:
    """What a producer sees: the published artifact, as the Fetcher validates it."""
    validator = Draft202012Validator(
        get_contract_json_schema("intraday_partition", 1), format_checker=FormatChecker()
    )
    return list(validator.iter_errors(request))


def test_a_partition_is_described_by_reference_and_carries_no_rows() -> None:
    request = validate_ingress_request(_request())

    assert isinstance(request, IntradayPartitionIngressRequest)
    assert request.payload.batch.source_raw_ref == REF
    assert request.payload.batch.source_raw_sha256 == DIGEST
    (partition,) = request.payload.data
    assert (partition.symbol, partition.record_kind, partition.row_count) == ("2330", "bar_1m", 266)
    assert (partition.rows_ref, partition.rows_sha256) == (ROWS_REF, ROWS_DIGEST)
    assert _published_schema_errors(_request()) == []
    # A tick day is far larger than a bar day and is still one described object.
    tick = _request(record_kind="tick", row_count=20_449, rows_bytes=589_000)
    assert validate_ingress_request(tick).payload.data[0].row_count == 20_449


@pytest.mark.parametrize(
    ("path", "value"),
    [
        # The provider's file and the mapped rows are both mandatory, checksummed,
        # credential-free and inside a plain R2 path.
        ("payload.batch.source_raw_ref", ...),
        ("payload.batch.source_raw_sha256", ...),
        ("payload.batch.source_raw_sha256", "B" * 64),
        ("payload.batch.source_raw_ref", REF.replace("r2://", "https://")),
        ("payload.data.0.rows_ref", ...),
        ("payload.data.0.rows_sha256", ...),
        ("payload.data.0.rows_sha256", "C" * 64),
        ("payload.data.0.rows_ref", ROWS_REF.replace("r2://", "https://")),
        ("payload.data.0.rows_ref", ROWS_REF.replace(ACCOUNT, f"key:secret@{ACCOUNT}")),
        ("payload.data.0.rows_ref", ROWS_REF + "?versionId=1"),
        ("payload.data.0.rows_ref", ROWS_REF.replace("/finlab/", "/finlab/../")),
        ("payload.data.0.rows_ref", ROWS_REF.replace("/finlab/", "/finlab//")),
        ("payload.data.0.rows_ref", ROWS_REF.replace("/finlab/", "/%2e%2e/")),
        ("payload.data.0.rows_ref", f"r2://{ACCOUNT}/findb-test-raw"),
        # One whole day of one symbol: never a full snapshot, a sequence or a row list.
        ("payload.batch.delivery_mode", "full_snapshot"),
        ("payload.batch.declared_record_count", 266),
        ("payload.batch.sequence", 1),
        ("payload.data", []),
        ("payload.data.0.open", "100"),
        ("payload.data.0.symbol", "2330/../0050"),
        ("payload.data.0.symbol", "symbol=2330"),
        ("payload.data.0.record_kind", "bar_60m"),
        ("payload.data.0.row_count", 0),
        ("payload.data.0.rows_bytes", 0),
        ("payload.data.0.rows_bytes", 16 * 1024 * 1024 + 1),
        ("payload.data.0.rows_format", "csv"),
        # The declared units are the only ones the stored columns are defined for.
        ("payload.data.0.price_adjustment", "adjusted"),
        ("payload.data.0.volume_unit", "share"),
        ("payload.data.0.volume_unit", ...),
    ],
)
def test_the_model_and_the_published_schema_refuse_the_same_shapes(path, value) -> None:
    request = _changed(path, value)

    with pytest.raises(ValidationError):
        validate_ingress_request(request)
    assert _published_schema_errors(request)


def test_two_partitions_cannot_share_a_request() -> None:
    request = _request()
    request["payload"]["data"].append(copy.deepcopy(request["payload"]["data"][0]))
    request["payload"]["batch"]["declared_record_count"] = 2

    with pytest.raises(ValidationError):
        validate_ingress_request(request)
    assert _published_schema_errors(request)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("payload.data.0.trade_date", "2026-07-20", "trade_date must equal batch data_date"),
        ("payload.batch.coverage_start_date", "2026-07-20", "coverage"),
        ("payload.data.0.row_count", 1441, "minutes in a day"),
    ],
)
def test_rules_the_schema_cannot_express_are_published_and_enforced(path, value, message) -> None:
    request = _changed(path, value)
    if "coverage" in path:
        request["payload"]["batch"]["coverage_end_date"] = "2026-07-21"

    with pytest.raises(ValidationError, match=message):
        validate_ingress_request(request)


def test_the_published_contract_lists_only_rules_that_apply_to_a_partition() -> None:
    schema = get_contract_json_schema("intraday_partition", 1)
    rules = {rule["id"] for rule in schema["x-findb-semantic-rules"]}

    assert {
        "partition.single_trading_day",
        "partition.bar_1m.row_count_bound",
        "partition.record_kind.dataset_declared",
        "payload.serialized.max_bytes",
    } <= rules
    assert not [name for name in rules if name.startswith(("row.ohlc.", "batch.sequence."))]
    assert "payload.delivery_key.unique" not in rules
    batch = schema["$defs"]["IntradayPartitionBatch"]
    assert {"source_raw_ref", "source_raw_sha256"} <= set(batch["required"])
    assert schema["$defs"]["IntradayPartitionPayload"]["properties"]["data"]["maxItems"] == 1


def test_the_published_contract_defines_the_columns_a_producer_must_deliver() -> None:
    layouts = get_contract_json_schema("intraday_partition", 1)["x-findb-row-layouts"]

    assert set(layouts) == {"bar_1m", "tick"}
    bar = {column["name"]: column for column in layouts["bar_1m"]["columns"]}
    assert list(bar) == [
        "symbol",
        "trade_date",
        "bar_start_time",
        "session",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    ]
    # Instants are UTC and the trade date is a date, so no reader depends on its
    # own timezone; the symbol is a string, so a leading zero survives.
    assert bar["bar_start_time"]["type"] == "timestamp[us, tz=UTC]"
    assert (bar["trade_date"]["type"], bar["symbol"]["type"]) == ("date32[day]", "string")
    assert [name for name, column in bar.items() if column["nullable"]] == ["trade_count", "vwap"]
    tick = {column["name"]: column for column in layouts["tick"]["columns"]}
    assert tick["trade_time"]["type"] == "timestamp[us, tz=UTC]"
    assert {"sequence", "price", "volume", "session"} <= {
        name for name, column in tick.items() if not column["nullable"]
    }
    # Only this contract carries rows by reference.
    assert "x-findb-row-layouts" not in get_contract_json_schema("market_eod", 2)


def test_a_backfilled_day_keeps_its_single_day_coverage() -> None:
    request = _request()
    request["payload"]["batch"].update(
        delivery_mode="backfill",
        coverage_start_date="2026-07-21",
        coverage_end_date="2026-07-21",
    )

    assert validate_ingress_request(request).payload.batch.delivery_mode == "backfill"


def test_only_an_intraday_dataset_declares_the_kind_it_stores() -> None:
    declaration = DatasetContractDeclaration.model_validate(_dataset_config("tick"))
    assert declaration.defaults.record_kind == "tick"

    undeclared = _dataset_config()
    del undeclared["defaults"]["record_kind"]
    with pytest.raises(ValidationError, match="record_kind"):
        DatasetContractDeclaration.model_validate(undeclared)

    eod = _dataset_config()
    eod["schema_id"] = "market_eod"
    with pytest.raises(ValidationError, match="only declared by intraday_partition"):
        DatasetContractDeclaration.model_validate(eod)


@pytest.fixture
def intraday_scope():
    """The provider scope the catalog does not grant yet: no feed delivers this contract."""
    assert "tw_equity_bar_1m" not in PROVIDER_DATASET_SCOPE["finlab"]
    scope = (*PROVIDER_DATASET_SCOPE["finlab"], "tw_equity_bar_1m")
    with patch.dict(PROVIDER_DATASET_SCOPE, {"finlab": scope}):
        yield


async def _seed(test_session, *, record_kind: str = "bar_1m") -> dict:
    test_session.add(
        DatasetRegistry(
            dataset_key="tw_equity_bar_1m",
            name="TW Equity 1-minute bars",
            asset_class="equity",
            market="TW",
            frequency="minute",
            is_active=True,
            config=_dataset_config(record_kind),
        )
    )
    _, api_key = await create_source_client(
        test_session,
        name="intraday-source-client",
        owner="tests",
        source_name="finlab",
        allowed_datasets=["tw_equity_bar_1m"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    await test_session.commit()
    return {get_settings().API_KEY_HEADER: api_key}


@pytest.mark.asyncio
async def test_an_accepted_partition_is_stored_by_the_queue_from_its_reference(
    owned_client: AsyncClient,
    test_session,
    test_engine,
    intraday_scope,
    monkeypatch,
) -> None:
    headers = await _seed(test_session)
    stores = Stores()
    monkeypatch.setattr(intraday, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(intraday, "canonical_publisher", lambda: stores.canonical)
    body = parquet_bytes(bars(day=date(2026, 7, 21)))
    digest = hashlib.sha256(body).hexdigest()
    key = f"finlab/tw_equity_bar_1m/0123456789abcdef/{digest}.rows.parquet"
    stores.r2.objects[(RAW_BUCKET, key)] = body
    request = _request(
        row_count=3,
        rows_ref=f"r2://{ACCOUNT}/{RAW_BUCKET}/{key}",
        rows_sha256=digest,
        rows_bytes=len(body),
    )

    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)

    assert response.status_code == 202, response.text
    body_json = response.json()
    attempt = await test_session.get(IngestionAttempt, UUID(body_json["attempt_id"]))
    run = await test_session.get(IngestionRun, UUID(body_json["run_id"]))
    raw = await test_session.get(RawMarketPayload, run.raw_payload_id)
    assert attempt.status == "accepted"
    assert (run.schema_id, run.schema_version, run.raw_records) == ("intraday_partition", 1, 1)
    # The retained payload is the description only; the rows stay in the bucket.
    assert raw.payload["batch"]["source_raw_ref"] == REF
    assert raw.payload["data"] == request["payload"]["data"]
    assert await test_session.scalar(select(func.count()).select_from(IntradayPartition)) == 0

    run_id = run.run_id
    job = await test_session.scalar(
        select(NormalizationJob).where(NormalizationJob.run_id == run_id)
    )
    await execute_normalization(
        run_id,
        job.delivery_id,
        database_url=test_engine.url.render_as_string(hide_password=False),
    )
    test_session.expire_all()
    run = await test_session.get(IngestionRun, run_id)
    assert (run.status, run.failure_code, run.success_records) == ("completed", None, 1)
    partition = await test_session.scalar(select(IntradayPartition))
    assert (partition.symbol, partition.row_count, partition.run_id) == ("2330", 3, run_id)
    assert partition.source_raw_sha256 == DIGEST and partition.source_rows_sha256 == digest
    assert partition.object_etag == stores.canonical.version(partition.object_key)


@pytest.mark.asyncio
async def test_a_dataset_refuses_partitions_of_another_kind(
    owned_client: AsyncClient,
    test_session,
    intraday_scope,
) -> None:
    headers = await _seed(test_session, record_kind="tick")

    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=_request())

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INGRESS_SCHEMA_NOT_ALLOWED"
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0
