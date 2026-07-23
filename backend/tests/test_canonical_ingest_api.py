"""Integration tests for canonical ingest attempts and versioned contracts."""

import asyncio
import json
from datetime import date, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.canonical import FuturesContinuousEOD, Instrument, MarketDataEOD
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    DQIssue,
    IngestionAttempt,
    IngestionRun,
    MissingDeliveryAlert,
    NormalizationJob,
    NormalizationOutbox,
    SourceClient,
)
from app.services.canonical_ingestion import (
    CanonicalIngestRejectionError,
    accept_canonical_ingest,
)
from app.services.delivery_policy import (
    DeliveryPolicyResult,
    evaluate_delivery_policy,
)
from app.services.ingestion import (
    IngestionService,
    lock_ingestion_idempotency_scope,
)
from app.services.ingestion_attempts import (
    IngestionAttemptService,
    reconcile_stale_ingestion_attempts,
)
from app.services.ingress_contracts import validate_ingress_request
from app.services.normalization_queue import execute_normalization
from app.services.source_clients import create_source_client
from app.utils import utc_now, uuid7


def _dataset_config() -> dict:
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {
            "market": "TW",
            "asset_class": "equity",
            "currency": "TWD",
        },
        "delivery_expectation": {
            "delivery_mode": "full_snapshot",
            "freshness_hours": 36,
            "minimum_record_count": 1,
            "maximum_count_drop_ratio": 0.1,
        },
    }


def _canonical_request(*, idempotency_key: str = "finlab_tw_equity_eod_20260721") -> dict:
    return {
        "dataset_key": "tw_equity_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": "finlab",
        "request_key": "finlab_tw_equity_eod_20260721_01",
        "idempotency_key": idempotency_key,
        "fetched_at": "2026-07-21T08:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2026-07-21",
                "delivery_mode": "full_snapshot",
                "declared_record_count": 1,
            },
            "data": [
                {
                    "symbol": "2330",
                    "source_symbol": "2330 TT Equity",
                    "trade_date": "2026-07-21",
                    "name": "台積電",
                    "currency": "TWD",
                    "open": "1000",
                    "high": "1020",
                    "low": "995",
                    "close": "1015",
                    "volume": 32_100_000,
                }
            ],
        },
    }


def _futures_request() -> dict:
    return {
        "dataset_key": "wtx_eod",
        "schema_id": "futures_continuous_eod",
        "schema_version": 1,
        "source": "bloomberg",
        "request_key": "bloomberg_wtx_eod_20260721_01",
        "idempotency_key": "bloomberg_wtx_eod_20260721",
        "fetched_at": "2026-07-21T08:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2026-07-21",
                "delivery_mode": "full_snapshot",
                "declared_record_count": 1,
            },
            "data": [
                {
                    "symbol": "TX",
                    "source_symbol": "TXA Index",
                    "trade_date": "2026-07-21",
                    "name": "臺股期貨近月",
                    "open": "23000",
                    "high": "23200",
                    "low": "22900",
                    "close": "23150",
                    "volume": 80_000,
                    "open_interest": 120_000,
                    "active_contract_code": "TXF202607",
                    "roll_rule": "front_month",
                    "roll_adjustment": "12.5",
                }
            ],
        },
    }


async def _seed_dataset(test_session, *, config: dict | None = None) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW Equity EOD",
            asset_class="equity",
            market="TW",
            frequency="daily",
            is_active=True,
            config=config if config is not None else _dataset_config(),
        )
    )
    await test_session.commit()


async def _seed_futures_dataset(test_session) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key="wtx_eod",
            name="WTX Continuous EOD",
            asset_class="future",
            market="WTX",
            frequency="daily",
            is_active=True,
            config={
                "schema_id": "futures_continuous_eod",
                "accepted_schema_versions": [1],
                "current_schema_version": 1,
                "schema_enforcement": "audit",
                "defaults": {
                    "market": "WTX",
                    "asset_class": "future",
                    "currency": "TWD",
                },
            },
        )
    )
    await test_session.commit()


async def _execute_run(test_session, test_engine, run_id: UUID) -> None:
    job = (
        await test_session.execute(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).scalar_one()
    await execute_normalization(
        run_id,
        job.delivery_id,
        database_url=test_engine.url.render_as_string(hide_password=False),
    )
    test_session.expire_all()


@pytest.mark.asyncio
async def test_canonical_ingest_persists_attempt_raw_run_and_job(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 202
    body = response.json()
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    run = await test_session.get(IngestionRun, UUID(body["run_id"]))
    raw = await test_session.get(RawMarketPayload, run.raw_payload_id)
    assert attempt is not None
    assert attempt.status == "accepted"
    assert attempt.run_id == run.run_id
    assert run.schema_id == "market_eod"
    assert run.schema_version == 1
    assert raw.schema_id == "market_eod"
    assert raw.schema_version == 1
    assert raw.payload["data"][0]["trade_date"] == "2026-07-21"
    assert (
        await test_session.scalar(
            select(func.count())
            .select_from(NormalizationJob)
            .where(NormalizationJob.run_id == run.run_id)
        )
        == 1
    )
    assert (
        await test_session.scalar(
            select(func.count())
            .select_from(NormalizationOutbox)
            .where(NormalizationOutbox.run_id == run.run_id)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_canonical_full_snapshot_resolves_exact_missing_delivery_alert(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    alert = MissingDeliveryAlert(
        dataset_key="tw_equity_eod",
        source="finlab",
        schema_id="market_eod",
        schema_version=1,
        expected_data_date=date(2026, 7, 21),
        status="open",
        first_detected_at=utc_now(),
        last_detected_at=utc_now(),
    )
    test_session.add(alert)
    await test_session.commit()

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(idempotency_key="resolve_missing_alert"),
    )

    assert response.status_code == 202
    await test_session.refresh(alert)
    assert alert.status == "resolved"
    assert alert.resolved_at is not None


@pytest.mark.asyncio
async def test_dataset_discovery_exposes_contract_declaration(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)

    response = await client.get("/api/v1/source/datasets", headers=source_headers)

    assert response.status_code == 200
    dataset = response.json()["data"][0]
    assert dataset["schema_id"] == "market_eod"
    assert dataset["accepted_schema_versions"] == [1]
    assert dataset["current_schema_version"] == 1
    assert dataset["schema_enforcement"] == "audit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema_id", "expected_title", "has_currency_rule"),
    [
        ("market_eod", "MarketEODIngressRequest", True),
        (
            "futures_continuous_eod",
            "FuturesContinuousEODIngressRequest",
            False,
        ),
    ],
)
async def test_versioned_contract_schema_endpoint_is_machine_readable(
    client: AsyncClient,
    source_headers: dict,
    schema_id: str,
    expected_title: str,
    has_currency_rule: bool,
):
    url = f"/api/v1/source/contracts/{schema_id}/versions/1"

    first = await client.get(url, headers=source_headers)
    second = await client.get(url, headers=source_headers)

    assert first.status_code == 200
    assert first.content == second.content
    schema = first.json()
    assert schema["$id"] == f"urn:findb:ingress-contract:{schema_id}:v1"
    assert schema["title"] == expected_title
    assert schema["properties"]["schema_id"]["const"] == schema_id
    assert schema["x-findb-contract-scope"]["sufficient_for_api_acceptance"] is False
    assert schema["x-findb-contract-scope"]["additional_acceptance_boundaries"]
    assert schema["x-findb-transformations"][0]["id"] == ("normalization.strings.strip_whitespace")
    assert (
        any(
            rule["id"] == "market.currency.row_or_dataset_default"
            for rule in schema["x-findb-semantic-rules"]
        )
        is has_currency_rule
    )


@pytest.mark.asyncio
async def test_contract_schema_endpoint_requires_source_auth(client: AsyncClient):
    response = await client.get("/api/v1/source/contracts/market_eod/versions/1")

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unknown_contract_schema_returns_404(
    client: AsyncClient,
    source_headers: dict,
):
    response = await client.get(
        "/api/v1/source/contracts/market_eod/versions/99",
        headers=source_headers,
    )

    assert response.status_code == 404
    assert "market_eod.v99" in response.json()["detail"]


@pytest.mark.asyncio
async def test_openapi_publishes_contract_route_without_prevalidating_ingest_body(
    client: AsyncClient,
):
    schema = (await client.get("/openapi.json")).json()

    assert "/api/v1/source/contracts/{schema_id}/versions/{schema_version}" in schema["paths"]
    assert "requestBody" not in schema["paths"]["/api/v1/source/ingest"]["post"]


@pytest.mark.asyncio
async def test_canonical_market_eod_normalizes_without_provider_specific_fields(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    run_id = UUID(response.json()["run_id"])

    await _execute_run(test_session, test_engine, run_id)

    instrument = (
        await test_session.execute(select(Instrument).where(Instrument.symbol == "2330"))
    ).scalar_one()
    eod = (
        await test_session.execute(
            select(MarketDataEOD).where(MarketDataEOD.instrument_id == instrument.instrument_id)
        )
    ).scalar_one()
    run = await test_session.get(IngestionRun, run_id)
    assert instrument.market == "TW"
    assert instrument.asset_class == "equity"
    assert instrument.currency == "TWD"
    assert str(eod.close) == "1015.00000000"
    assert eod.source == "finlab"
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_canonical_futures_contract_routes_by_schema_not_provider_payload(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_futures_dataset(test_session)
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_futures_request(),
    )
    assert response.status_code == 202
    run_id = UUID(response.json()["run_id"])

    await _execute_run(test_session, test_engine, run_id)

    instrument = (
        await test_session.execute(select(Instrument).where(Instrument.symbol == "TX"))
    ).scalar_one()
    eod = (
        await test_session.execute(
            select(FuturesContinuousEOD).where(
                FuturesContinuousEOD.instrument_id == instrument.instrument_id
            )
        )
    ).scalar_one()
    run = await test_session.get(IngestionRun, run_id)
    assert instrument.market == "WTX"
    assert instrument.currency == "TWD"
    assert str(eod.close) == "23150.00000000"
    assert eod.open_interest == 120_000
    assert eod.active_contract_code == "TXF202607"
    assert str(eod.roll_adjustment) == "12.50000000"
    assert eod.source == "bloomberg"
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_canonical_futures_raw_rerun_preserves_optional_contract_fields(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_futures_dataset(test_session)
    accepted = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_futures_request(),
    )
    original_run_id = UUID(accepted.json()["run_id"])
    await _execute_run(test_session, test_engine, original_run_id)

    rerun = await client.post(
        f"/api/v1/source/runs/{original_run_id}/rerun",
        headers=source_headers,
    )
    assert rerun.status_code == 202
    rerun_id = UUID(rerun.json()["run_id"])
    await _execute_run(test_session, test_engine, rerun_id)

    eod = (await test_session.execute(select(FuturesContinuousEOD))).scalar_one()
    rerun_row = await test_session.get(IngestionRun, rerun_id)
    assert eod.open_interest == 120_000
    assert eod.active_contract_code == "TXF202607"
    assert str(eod.roll_adjustment) == "12.50000000"
    assert eod.run_id == rerun_id
    assert rerun_row is not None
    assert rerun_row.status == "completed"


@pytest.mark.asyncio
async def test_futures_contract_requires_dataset_default_currency_before_persistence(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_futures_dataset(test_session)
    dataset = (
        await test_session.execute(
            select(DatasetRegistry).where(DatasetRegistry.dataset_key == "wtx_eod")
        )
    ).scalar_one()
    config = dict(dataset.config)
    config["defaults"] = {**config["defaults"], "currency": None}
    dataset.config = config
    await test_session.commit()

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_futures_request(),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CURRENCY_REQUIRED"
    attempt = await test_session.get(IngestionAttempt, UUID(response.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
async def test_canonical_raw_rerun_preserves_schema_lineage_and_routing(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_dataset(test_session)
    accepted = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    original_run_id = UUID(accepted.json()["run_id"])
    rerun = await client.post(
        f"/api/v1/source/runs/{original_run_id}/rerun",
        headers=source_headers,
    )
    rerun_id = UUID(rerun.json()["run_id"])

    await _execute_run(test_session, test_engine, rerun_id)

    rerun_row = await test_session.get(IngestionRun, rerun_id)
    assert rerun.status_code == 202
    assert rerun_row.schema_id == "market_eod"
    assert rerun_row.schema_version == 1
    assert rerun_row.status == "completed"


@pytest.mark.asyncio
async def test_worker_routes_using_persisted_schema_version(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
):
    await _seed_dataset(test_session)
    accepted = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    run_id = UUID(accepted.json()["run_id"])
    run = await test_session.get(IngestionRun, run_id)
    raw = await test_session.get(RawMarketPayload, run.raw_payload_id)
    raw.schema_version = 2
    await test_session.commit()

    await _execute_run(test_session, test_engine, run_id)

    await test_session.refresh(run)
    assert run.status == "failed"
    assert run.failure_code == "NORMALIZER_NOT_CONFIGURED"
    assert await test_session.scalar(select(func.count()).select_from(MarketDataEOD)) == 0


@pytest.mark.asyncio
async def test_schema_invalid_request_is_durably_rejected(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    value = _canonical_request()
    value["payload"]["data"][0]["price"] = {"last": 1015, "secret": "do-not-leak"}

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INGRESS_SCHEMA_INVALID"
    assert "do-not-leak" not in body["error"]["message"]
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == "INGRESS_SCHEMA_INVALID"
    assert attempt.http_status == 422
    assert attempt.run_id is None
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("default_currency", "row_currency", "expected_status"),
    [
        pytest.param("TWD", "TWD", 202, id="default-present-row-present"),
        pytest.param("TWD", None, 202, id="default-present-row-missing"),
        pytest.param(None, "TWD", 202, id="default-missing-row-present"),
        pytest.param(None, None, 422, id="default-missing-row-missing"),
    ],
)
async def test_market_currency_resolves_from_dataset_default_or_every_row(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    default_currency: str | None,
    row_currency: str | None,
    expected_status: int,
):
    config = _dataset_config()
    config["defaults"]["currency"] = default_currency
    await _seed_dataset(test_session, config=config)
    value = _canonical_request()
    if row_currency is None:
        value["payload"]["data"][0].pop("currency")
    else:
        value["payload"]["data"][0]["currency"] = row_currency

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == expected_status
    if expected_status == 202:
        attempt = await test_session.get(
            IngestionAttempt,
            UUID(response.json()["attempt_id"]),
        )
        assert attempt.status == "accepted"
        assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 1
        assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 1
    else:
        assert response.json()["error"]["code"] == "CURRENCY_REQUIRED"
        attempt = await test_session.get(
            IngestionAttempt,
            UUID(response.json()["attempt_id"]),
        )
        assert attempt.status == "rejected"
        assert attempt.failure_code == "CURRENCY_REQUIRED"
        assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
        assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0
        assert await test_session.scalar(select(func.count()).select_from(NormalizationJob)) == 0


@pytest.mark.asyncio
async def test_market_currency_rejects_mixed_rows_without_dataset_default(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    config = _dataset_config()
    config["defaults"]["currency"] = None
    await _seed_dataset(test_session, config=config)
    value = _canonical_request()
    second_row = dict(value["payload"]["data"][0])
    second_row["symbol"] = "2317"
    second_row.pop("currency")
    value["payload"]["data"].append(second_row)
    value["payload"]["batch"]["declared_record_count"] = 2

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CURRENCY_REQUIRED"
    attempt = await test_session.get(IngestionAttempt, UUID(response.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("count", "DECLARED_RECORD_COUNT_MISMATCH"),
        ("duplicate", "DUPLICATE_DELIVERY_KEY"),
    ],
)
async def test_semantic_validation_uses_stable_codes_and_durable_attempts(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    mutation: str,
    expected_code: str,
):
    value = _canonical_request()
    if mutation == "count":
        value["payload"]["batch"]["declared_record_count"] = 2
    else:
        value["payload"]["data"].append(dict(value["payload"]["data"][0]))
        value["payload"]["batch"]["declared_record_count"] = 2

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code
    attempt = await test_session.get(IngestionAttempt, UUID(response.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == expected_code
    assert attempt.error_message is not None
    assert len(attempt.error_message) <= 1_000
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_code", "expected_message"),
    [
        (
            "count",
            "DECLARED_RECORD_COUNT_MISMATCH",
            "declared_record_count must equal",
        ),
        (
            "duplicate",
            "DUPLICATE_DELIVERY_KEY",
            "duplicate (symbol, trade_date)",
        ),
    ],
)
async def test_special_validation_code_and_message_select_the_same_error(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    mutation: str,
    expected_code: str,
    expected_message: str,
):
    value = _canonical_request()
    value["source"] = "Invalid Source"
    if mutation == "count":
        value["payload"]["batch"]["declared_record_count"] = 2
    else:
        value["payload"]["data"].append(dict(value["payload"]["data"][0]))
        value["payload"]["batch"]["declared_record_count"] = 2

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == expected_code
    assert expected_message in body["error"]["message"]
    assert "String should match pattern" not in body["error"]["message"]
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == expected_code
    assert attempt.error_message == body["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_message"),
    [
        ("sequence", "sequence and sequence_count must be provided together"),
        ("ohlc", "high must not be below"),
        ("coverage", "coverage dates are required"),
    ],
)
async def test_representative_semantic_rules_are_enforced_after_attempt_creation(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    mutation: str,
    expected_message: str,
):
    value = _canonical_request()
    if mutation == "sequence":
        value["payload"]["batch"]["sequence"] = 1
    elif mutation == "ohlc":
        value["payload"]["data"][0]["high"] = "900"
    else:
        value["payload"]["data"][0]["trade_date"] = "2026-07-20"

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INGRESS_SCHEMA_INVALID"
    assert expected_message in body["error"]["message"]
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == "INGRESS_SCHEMA_INVALID"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0


@pytest.mark.asyncio
async def test_invalid_json_is_durably_rejected(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    response = await client.post(
        "/api/v1/source/ingest",
        headers={**source_headers, "Content-Type": "application/json"},
        content=b'{"dataset_key":',
    )

    assert response.status_code == 422
    body = response.json()
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert body["error"]["code"] == "INGRESS_SCHEMA_INVALID"
    assert attempt.status == "rejected"
    assert attempt.dataset_key is None
    assert attempt.request_sha256 is not None


@pytest.mark.asyncio
async def test_unknown_dataset_rejection_has_attempt_id(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    value = _canonical_request()
    value["dataset_key"] = "unknown_dataset"

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "DATASET_NOT_FOUND"
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.dataset_key == "unknown_dataset"
    assert attempt.status == "rejected"


@pytest.mark.asyncio
async def test_dataset_without_contract_is_rejected_and_recorded(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session, config={"source_format": "legacy"})

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "DATASET_CONTRACT_NOT_CONFIGURED"
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_update",
    [
        {"defaults": None},
        {"defaults": {"market": "US", "asset_class": "equity", "currency": "TWD"}},
        {"defaults": {"market": "TW", "asset_class": "etf", "currency": "TWD"}},
    ],
)
async def test_invalid_or_conflicting_contract_scope_is_rejected_before_queueing(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    config_update: dict,
):
    config = _dataset_config()
    config.update(config_update)
    await _seed_dataset(test_session, config=config)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DATASET_CONTRACT_NOT_CONFIGURED"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(NormalizationJob)) == 0


@pytest.mark.asyncio
async def test_duplicate_contract_creates_new_attempt_but_reuses_run(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    second = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["run_id"] == first.json()["run_id"]
    assert second.json()["attempt_id"] != first.json()["attempt_id"]
    second_attempt = await test_session.get(
        IngestionAttempt,
        UUID(second.json()["attempt_id"]),
    )
    assert second_attempt.status == "duplicate"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 1
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 1


@pytest.mark.asyncio
async def test_idempotency_mismatch_is_rejected_without_new_run(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    changed = _canonical_request()
    changed["payload"]["data"][0]["close"] = "1016"
    second = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=changed,
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    attempt = await test_session.get(IngestionAttempt, UUID(second.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 1


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_reused_by_different_source(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    first = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )
    changed_source = _canonical_request()
    changed_source["source"] = "bloomberg"
    second = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=changed_source,
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"


@pytest.mark.asyncio
async def test_unbounded_schema_version_does_not_break_attempt_persistence(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    value = _canonical_request()
    value["schema_version"] = 10**100

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    attempt = await test_session.get(IngestionAttempt, UUID(response.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.schema_version is None


@pytest.mark.asyncio
async def test_whitespace_schema_id_is_exact_dispatch_and_durably_unsupported(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    value = _canonical_request()
    value["schema_id"] = " market_eod "

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "INGRESS_SCHEMA_UNSUPPORTED"
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == "INGRESS_SCHEMA_UNSUPPORTED"
    assert attempt.schema_id == "market_eod"
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0


@pytest.mark.asyncio
async def test_attempt_status_endpoint_returns_rejection(
    client: AsyncClient,
    source_headers: dict,
):
    value = _canonical_request()
    value["schema_version"] = 2
    rejected = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    response = await client.get(
        f"/api/v1/source/attempts/{rejected.json()['attempt_id']}",
        headers=source_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["failure_code"] == "INGRESS_SCHEMA_UNSUPPORTED"


@pytest.mark.asyncio
async def test_attempt_status_is_scoped_to_authenticated_source_client(
    client: AsyncClient,
    source_headers: dict,
    test_session,
):
    await _seed_dataset(test_session)
    _, finlab_key = await create_source_client(
        test_session,
        name="FinLab fetcher",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100,
        rate_limit_window=60,
    )
    _, other_key = await create_source_client(
        test_session,
        name="Other fetcher",
        source_name="other",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100,
        rate_limit_window=60,
    )
    header_name = next(iter(source_headers))
    rejected = await client.post(
        "/api/v1/source/ingest",
        headers={header_name: finlab_key},
        json={**_canonical_request(), "source": "bloomberg"},
    )
    attempt_id = rejected.json()["attempt_id"]

    owner_response = await client.get(
        f"/api/v1/source/attempts/{attempt_id}",
        headers={header_name: finlab_key},
    )
    other_response = await client.get(
        f"/api/v1/source/attempts/{attempt_id}",
        headers={header_name: other_key},
    )

    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "SOURCE_IDENTITY_MISMATCH"
    assert owner_response.status_code == 200
    assert other_response.status_code == 404


@pytest.mark.asyncio
async def test_unexpected_processing_error_marks_attempt_rejected(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    monkeypatch,
):
    await _seed_dataset(test_session)

    async def fail_ingest(*args, **kwargs):
        raise RuntimeError("sensitive internal failure")

    monkeypatch.setattr(
        "app.services.canonical_ingestion.IngestionService.ingest_contract",
        fail_ingest,
    )
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "sensitive" not in response.json()["error"]["message"]
    attempt = await test_session.get(IngestionAttempt, UUID(response.json()["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_database_failure_before_attempt_returns_retryable_503(
    client: AsyncClient,
    source_headers: dict,
    monkeypatch,
):
    async def fail_begin(*args, **kwargs):
        raise OperationalError("INSERT", {}, RuntimeError("database unavailable"))

    monkeypatch.setattr(
        "app.services.canonical_ingestion.IngestionAttemptService.begin",
        fail_begin,
    )
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.json() == {
        "success": False,
        "attempt_id": None,
        "error": {
            "code": "DATABASE_UNAVAILABLE",
            "message": "Ingestion is temporarily unavailable",
        },
    }


@pytest.mark.asyncio
async def test_claim_failure_after_commit_returns_persisted_attempt_id(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    monkeypatch,
):
    async def fail_claim(*args, **kwargs):
        raise OperationalError("SELECT FOR UPDATE", {}, RuntimeError("claim failed"))

    monkeypatch.setattr(test_session, "scalar", fail_claim)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert response.json()["attempt_id"] is not None
    attempt = await test_session.get(
        IngestionAttempt,
        UUID(response.json()["attempt_id"]),
    )
    assert attempt is not None
    assert attempt.status == "received"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_failure", ["exception", "missing"])
async def test_non_database_claim_failure_returns_internal_error_with_attempt_id(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    monkeypatch,
    claim_failure: str,
):
    async def fail_claim(*args, **kwargs):
        if claim_failure == "exception":
            raise RuntimeError("claim invariant failed")
        return None

    monkeypatch.setattr(test_session, "scalar", fail_claim)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["attempt_id"] is not None
    assert "Retry-After" not in response.headers
    attempt = await test_session.get(
        IngestionAttempt,
        UUID(response.json()["attempt_id"]),
    )
    assert attempt is not None
    assert attempt.status == "received"


@pytest.mark.asyncio
async def test_non_database_rejection_audit_failure_returns_internal_error(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    monkeypatch,
):
    value = _canonical_request()
    value["schema_version"] = 2

    async def fail_reject(*args, **kwargs):
        raise RuntimeError("audit programming failure")

    monkeypatch.setattr(IngestionAttemptService, "reject", fail_reject)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=value,
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["attempt_id"] is not None
    assert "Retry-After" not in response.headers
    attempt = await test_session.get(
        IngestionAttempt,
        UUID(response.json()["attempt_id"]),
    )
    assert attempt is not None
    assert attempt.status == "received"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_status", "expected_code"),
    [
        ("database", 503, "DATABASE_UNAVAILABLE"),
        ("internal", 500, "INTERNAL_ERROR"),
    ],
)
async def test_reconciler_winning_after_rollback_preserves_stable_error_envelope(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
    monkeypatch,
    failure_kind: str,
    expected_status: int,
    expected_code: str,
):
    await _seed_dataset(test_session)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    original_reject = IngestionAttemptService.reject

    async def fail_ingest(*args, **kwargs):
        if failure_kind == "database":
            raise OperationalError("INSERT", {}, RuntimeError("database unavailable"))
        raise RuntimeError("internal failure")

    async def abort_before_reject(self, attempt_id, **kwargs):
        async with session_factory() as other_session:
            attempt = await other_session.get(
                IngestionAttempt,
                attempt_id,
                with_for_update=True,
            )
            assert attempt is not None
            attempt.status = "aborted"
            attempt.failure_code = "ATTEMPT_INTERRUPTED"
            attempt.error_message = "Request processing did not reach a terminal state"
            attempt.completed_at = utc_now()
            attempt.updated_at = utc_now()
            await other_session.commit()
        return await original_reject(self, attempt_id, **kwargs)

    monkeypatch.setattr(
        "app.services.canonical_ingestion.IngestionService.ingest_contract",
        fail_ingest,
    )
    monkeypatch.setattr(IngestionAttemptService, "reject", abort_before_reject)

    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_canonical_request(),
    )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    attempt_id = UUID(response.json()["attempt_id"])
    attempt = await test_session.get(IngestionAttempt, attempt_id)
    await test_session.refresh(attempt)
    assert attempt.status == "aborted"
    assert attempt.failure_code == "ATTEMPT_INTERRUPTED"


@pytest.mark.asyncio
async def test_stale_received_attempts_are_reconciled_in_bounded_batches(test_session):
    stale = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=10),
        updated_at=utc_now() - timedelta(minutes=10),
    )
    recent = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    stale_two = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=9),
        updated_at=utc_now() - timedelta(minutes=9),
    )
    stale_three = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=8),
        updated_at=utc_now() - timedelta(minutes=8),
    )
    test_session.add_all([stale, stale_two, stale_three, recent])
    await test_session.commit()

    assert (
        await reconcile_stale_ingestion_attempts(
            test_session,
            stale_seconds=300,
            batch_size=2,
        )
        == 2
    )
    assert (
        await reconcile_stale_ingestion_attempts(
            test_session,
            stale_seconds=300,
            batch_size=2,
        )
        == 1
    )
    await test_session.refresh(stale)
    await test_session.refresh(recent)
    assert stale.status == "aborted"
    assert stale.failure_code == "ATTEMPT_INTERRUPTED"
    assert stale.completed_at is not None
    assert recent.status == "received"


@pytest.mark.asyncio
async def test_live_attempt_claim_is_skipped_until_owner_releases(
    test_session,
    test_engine,
):
    stale = IngestionAttempt(
        attempt_id=uuid7(),
        status="received",
        created_at=utc_now() - timedelta(minutes=10),
        updated_at=utc_now() - timedelta(minutes=10),
    )
    test_session.add(stale)
    await test_session.commit()
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as active_session:
        claimed = await active_session.scalar(
            select(IngestionAttempt)
            .where(IngestionAttempt.attempt_id == stale.attempt_id)
            .with_for_update()
        )
        assert claimed is not None
        async with session_factory() as reconcile_session:
            assert (
                await reconcile_stale_ingestion_attempts(
                    reconcile_session,
                    stale_seconds=300,
                    batch_size=10,
                )
                == 0
            )
        await active_session.rollback()

    async with session_factory() as reconcile_session:
        assert (
            await reconcile_stale_ingestion_attempts(
                reconcile_session,
                stale_seconds=300,
                batch_size=10,
            )
            == 1
        )


@pytest.mark.asyncio
async def test_reconciliation_never_reverses_terminal_attempt(test_session):
    accepted = IngestionAttempt(
        attempt_id=uuid7(),
        status="accepted",
        http_status=202,
        created_at=utc_now() - timedelta(minutes=10),
        updated_at=utc_now() - timedelta(minutes=10),
        completed_at=utc_now() - timedelta(minutes=9),
    )
    test_session.add(accepted)
    await test_session.commit()

    assert (
        await reconcile_stale_ingestion_attempts(
            test_session,
            stale_seconds=300,
            batch_size=10,
        )
        == 0
    )
    await test_session.refresh(accepted)
    assert accepted.status == "accepted"
    assert accepted.failure_code is None


def _policy_config(*, count_action: str, minimum: int) -> dict:
    config = _dataset_config()
    config["delivery_expectation"] = {
        "delivery_mode": "full_snapshot",
        "record_count": {
            "minimum_record_count": minimum,
            "maximum_count_drop_ratio": 0.1,
            "action": count_action,
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": "disabled",
        },
        "latest_date": {
            "calendar_market": "TW",
            "timezone": "Asia/Taipei",
            "market_close_time": "13:30:00",
            "availability_grace_minutes": 120,
            "action": "disabled",
        },
    }
    return config


@pytest.mark.asyncio
async def test_delivery_policy_reject_persists_only_bounded_attempt_details(
    client: AsyncClient,
    source_headers: dict,
    test_session: AsyncSession,
) -> None:
    await _seed_dataset(
        test_session,
        config=_policy_config(count_action="reject", minimum=2),
    )

    response = await client.post(
        "/api/v1/source/ingest", headers=source_headers, json=_canonical_request()
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "BATCH_RECORD_COUNT_DROP"
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    assert attempt.status == "rejected"
    assert attempt.failure_details["outcome"] == "reject"
    assert "2330" not in str(attempt.failure_details)
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 0
    assert await test_session.scalar(select(func.count()).select_from(NormalizationJob)) == 0
    assert await test_session.scalar(select(func.count()).select_from(NormalizationOutbox)) == 0
    assert await test_session.scalar(select(func.count()).select_from(DQIssue)) == 0


@pytest.mark.asyncio
async def test_delivery_policy_warning_persists_one_aggregate_dq_issue(
    client: AsyncClient,
    source_headers: dict,
    test_session: AsyncSession,
) -> None:
    await _seed_dataset(
        test_session,
        config=_policy_config(count_action="warn", minimum=2),
    )

    response = await client.post(
        "/api/v1/source/ingest", headers=source_headers, json=_canonical_request()
    )

    assert response.status_code == 202
    body = response.json()
    run = await test_session.get(IngestionRun, UUID(body["run_id"]))
    attempt = await test_session.get(IngestionAttempt, UUID(body["attempt_id"]))
    issues = list(
        (await test_session.execute(select(DQIssue).where(DQIssue.run_id == run.run_id)))
        .scalars()
        .all()
    )
    assert run.policy_outcome == "warn"
    assert run.batch_data_date.isoformat() == "2026-07-21"
    assert run.delivery_mode == "full_snapshot"
    assert attempt.failure_details["outcome"] == "warn"
    assert len(issues) == 1
    assert issues[0].issue_type == "INGRESS_DELIVERY_POLICY_WARNING"
    assert issues[0].raw_data["violations"][0]["code"] == "BATCH_RECORD_COUNT_DROP"
    attempt_response = await client.get(
        f"/api/v1/source/attempts/{attempt.attempt_id}", headers=source_headers
    )
    assert attempt_response.status_code == 200
    assert attempt_response.json()["failure_details"]["outcome"] == "warn"


@pytest.mark.asyncio
async def test_exact_duplicate_is_returned_before_dynamic_policy_changes(
    client: AsyncClient,
    source_headers: dict,
    test_session: AsyncSession,
) -> None:
    await _seed_dataset(
        test_session,
        config=_policy_config(count_action="reject", minimum=1),
    )
    request = _canonical_request()
    first = await client.post("/api/v1/source/ingest", headers=source_headers, json=request)
    assert first.status_code == 202
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    dataset.config = _policy_config(count_action="reject", minimum=2)
    await test_session.commit()

    duplicate = await client.post("/api/v1/source/ingest", headers=source_headers, json=request)

    assert duplicate.status_code == 202
    assert duplicate.json()["run_id"] == first.json()["run_id"]
    assert duplicate.json()["message"] == "Duplicate idempotency_key, returning existing run"


@pytest.mark.asyncio
async def test_concurrent_exact_deliveries_create_one_run_without_duplicate_warning(
    test_session: AsyncSession,
    test_engine,
) -> None:
    await _seed_dataset(
        test_session,
        config=_policy_config(count_action="warn", minimum=1),
    )
    request_body = _canonical_request(idempotency_key="concurrent-policy-delivery")
    request = validate_ingress_request(request_body)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as first_session, session_factory() as second_session:
        first_attempt = await IngestionAttemptService(first_session).begin(
            request_body, json.dumps(request_body).encode()
        )
        second_attempt = await IngestionAttemptService(second_session).begin(
            request_body, json.dumps(request_body).encode()
        )
        results = await asyncio.gather(
            IngestionService(first_session).ingest_contract(request, first_attempt.attempt_id),
            IngestionService(second_session).ingest_contract(request, second_attempt.attempt_id),
        )

    assert sorted(result[2] for result in results) == [False, True]
    assert results[0][0] == results[1][0]
    test_session.expire_all()
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 1
    assert await test_session.scalar(select(func.count()).select_from(IngestionRun)) == 1
    assert await test_session.scalar(select(func.count()).select_from(DQIssue)) == 0


@pytest.mark.asyncio
async def test_waiting_duplicate_rechecks_after_scope_lock_before_dynamic_policy(
    test_session: AsyncSession,
    test_engine,
    monkeypatch,
) -> None:
    """A lock waiter must see the winner before evaluating changed policy state."""
    await _seed_dataset(
        test_session,
        config=_policy_config(count_action="warn", minimum=1),
    )
    request_body = _canonical_request(idempotency_key="lock-waiter-policy-bypass")
    request = validate_ingress_request(request_body)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as first_session, session_factory() as second_session:
        first_attempt = await IngestionAttemptService(first_session).begin(
            request_body, json.dumps(request_body).encode()
        )
        second_attempt = await IngestionAttemptService(second_session).begin(
            request_body, json.dumps(request_body).encode()
        )
        await lock_ingestion_idempotency_scope(
            first_session,
            source_client_id=None,
            dataset_key=request.dataset_key,
            idempotency_key=request.idempotency_key,
        )

        second_reached_idempotency_lock = asyncio.Event()
        second_policy_calls = 0

        async def observed_idempotency_lock(
            db,
            *,
            source_client_id,
            dataset_key,
            idempotency_key,
        ):
            if db is second_session:
                second_reached_idempotency_lock.set()
            await lock_ingestion_idempotency_scope(
                db,
                source_client_id=source_client_id,
                dataset_key=dataset_key,
                idempotency_key=idempotency_key,
            )

        async def reject_changed_policy_for_waiter(db, contract_request, expectation):
            nonlocal second_policy_calls
            if db is second_session:
                second_policy_calls += 1
                return DeliveryPolicyResult(
                    outcome="reject",
                    primary_code="BATCH_RECORD_COUNT_DROP",
                    baseline_status="active",
                    evaluated_at=utc_now(),
                )
            return await evaluate_delivery_policy(db, contract_request, expectation)

        monkeypatch.setattr(
            "app.services.ingestion.lock_ingestion_idempotency_scope",
            observed_idempotency_lock,
        )
        monkeypatch.setattr(
            "app.services.ingestion.evaluate_delivery_policy",
            reject_changed_policy_for_waiter,
        )

        second_task = asyncio.create_task(
            IngestionService(second_session).ingest_contract(
                request,
                second_attempt.attempt_id,
            )
        )
        await asyncio.wait_for(second_reached_idempotency_lock.wait(), timeout=2)

        first_result = await IngestionService(first_session).ingest_contract(
            request,
            first_attempt.attempt_id,
        )
        second_result = await asyncio.wait_for(second_task, timeout=2)

    assert first_result[2] is False
    assert second_result == (first_result[0], first_result[1], True)
    assert second_policy_calls == 0


@pytest.mark.asyncio
async def test_cross_source_waiter_returns_mismatch_before_dynamic_policy(
    test_session: AsyncSession,
    test_engine,
    monkeypatch,
) -> None:
    """The DB unique scope wins over source-specific policy lock scopes."""
    await _seed_dataset(
        test_session,
        config=_policy_config(count_action="warn", minimum=1),
    )
    source_client_id = uuid7()
    test_session.add(
        SourceClient(
            client_id=source_client_id,
            name="Shared cross-source test client",
            source_name="shared",
            key_hash="a" * 64,
        )
    )
    await test_session.commit()
    first_body = _canonical_request(idempotency_key="cross-source-lock-waiter")
    second_body = _canonical_request(idempotency_key="cross-source-lock-waiter")
    second_body["source"] = "bloomberg"
    second_body["request_key"] = "bloomberg-cross-source-lock-waiter"
    first_request = validate_ingress_request(first_body)
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as first_session, session_factory() as second_session:
        first_session.info["source_client_id"] = source_client_id
        second_session.info["source_client_id"] = source_client_id
        first_attempt = await IngestionAttemptService(first_session).begin(
            first_body, json.dumps(first_body).encode()
        )
        await lock_ingestion_idempotency_scope(
            first_session,
            source_client_id=source_client_id,
            dataset_key=first_request.dataset_key,
            idempotency_key=first_request.idempotency_key,
        )

        second_reached_idempotency_lock = asyncio.Event()
        second_policy_calls = 0

        async def observed_idempotency_lock(
            db,
            *,
            source_client_id,
            dataset_key,
            idempotency_key,
        ):
            if db is second_session:
                second_reached_idempotency_lock.set()
            await lock_ingestion_idempotency_scope(
                db,
                source_client_id=source_client_id,
                dataset_key=dataset_key,
                idempotency_key=idempotency_key,
            )

        async def reject_changed_policy_for_waiter(db, contract_request, expectation):
            nonlocal second_policy_calls
            if db is second_session:
                second_policy_calls += 1
                return DeliveryPolicyResult(
                    outcome="reject",
                    primary_code="BATCH_RECORD_COUNT_DROP",
                    baseline_status="active",
                    evaluated_at=utc_now(),
                )
            return await evaluate_delivery_policy(db, contract_request, expectation)

        monkeypatch.setattr(
            "app.services.ingestion.lock_ingestion_idempotency_scope",
            observed_idempotency_lock,
        )
        monkeypatch.setattr(
            "app.services.ingestion.evaluate_delivery_policy",
            reject_changed_policy_for_waiter,
        )

        second_task = asyncio.create_task(
            accept_canonical_ingest(second_session, json.dumps(second_body).encode())
        )
        await asyncio.wait_for(second_reached_idempotency_lock.wait(), timeout=2)

        first_result = await IngestionService(first_session).ingest_contract(
            first_request,
            first_attempt.attempt_id,
        )
        with pytest.raises(CanonicalIngestRejectionError) as rejection:
            await asyncio.wait_for(second_task, timeout=2)

    assert first_result[2] is False
    assert rejection.value.status_code == 409
    assert rejection.value.code == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert second_policy_calls == 0
