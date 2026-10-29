"""v2 isolates relational OHLC failures without relaxing the ingress shape."""

from copy import deepcopy
from itertools import product
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.canonical import Instrument, InstrumentStats, MarketDataEOD
from app.models.raw import RawMarketPayload
from app.models.registry import DeliveryMemberEvidence, DQIssue, IngestionRun
from app.schemas.ingress import MarketEODRow, MarketEODRowV2
from app.services.ingress_contracts import get_contract_json_schema, validate_ingress_request
from app.services.normalize.contracts import MarketEODV2ContractNormalizer
from tests.test_canonical_ingest_api import (
    _canonical_request,
    _dataset_config,
    _execute_run,
    _seed_dataset,
)


@pytest.mark.parametrize(("open_", "high", "low"), product([None, "1", "2"], repeat=3))
def test_v2_nullable_ohlc_dq_matches_v1_relational_validation(open_, high, low):
    row = {
        "symbol": "00700",
        "source_symbol": "J3TRKQ-L",
        "trade_date": "2026-07-21",
        "open": open_,
        "high": high,
        "low": low,
        "close": "1",
    }
    validated = MarketEODRowV2.model_validate(row)
    normalizer = MarketEODV2ContractNormalizer(None, _dataset_config())
    record = normalizer.map_fields({"data": [validated.model_dump(mode="json")]})[0]
    errors = [
        issue for issue in normalizer.dq_validator.validate_eod(record) if issue.severity == "error"
    ]
    try:
        MarketEODRow.model_validate(row)
    except ValidationError:
        assert errors
        assert all(issue.raw_data["source_symbol"] == "J3TRKQ-L" for issue in errors)
    else:
        assert not errors


@pytest.mark.parametrize(
    "mutation",
    ["negative", "missing_close", "null_close", "duplicate", "count", "date", "extra", "nonfinite"],
)
def test_v2_preserves_structural_rejections(mutation):
    value = _canonical_request()
    value["schema_version"] = 2
    row = value["payload"]["data"][0]
    if mutation == "negative":
        row["low"] = "-1"
    elif mutation == "missing_close":
        del row["close"]
    elif mutation == "null_close":
        row["close"] = None
    elif mutation == "duplicate":
        value["payload"]["data"].append(deepcopy(row))
        value["payload"]["batch"]["declared_record_count"] = 2
    elif mutation == "count":
        value["payload"]["batch"]["declared_record_count"] = 2
    elif mutation == "date":
        row["trade_date"] = "2026-07-20"
    elif mutation == "extra":
        row["provider_extra"] = "unsupported"
    else:
        row["high"] = "NaN"
    with pytest.raises(ValidationError):
        validate_ingress_request(value)


def test_v2_only_removes_relational_ingress_semantics():
    v1 = get_contract_json_schema("market_eod", 1)
    v2 = get_contract_json_schema("market_eod", 2)
    assert v2["$defs"]["MarketEODRowV2"]["properties"] == v1["$defs"]["MarketEODRow"]["properties"]
    assert v2["$defs"]["MarketEODRowV2"]["required"] == v1["$defs"]["MarketEODRow"]["required"]
    assert v2["x-findb-semantic-rules"] == [
        rule for rule in v1["x-findb-semantic-rules"] if not rule["id"].startswith("row.ohlc.")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("include_valid", [False, True])
async def test_v2_dq_preserves_raw_and_existing_prices_without_false_evidence(
    owned_client, source_headers, test_session, test_engine, include_valid
):
    config = _dataset_config()
    config["accepted_schema_versions"] = [1, 2]
    await _seed_dataset(test_session, config=config)
    original = await owned_client.post(
        "/api/v1/source/ingest", headers=source_headers, json=_canonical_request()
    )
    assert original.status_code == 202, original.text
    original_id = UUID(original.json()["run_id"])
    await _execute_run(test_session, test_engine, original_id)
    instrument = (
        await test_session.scalars(select(Instrument).where(Instrument.symbol == "2330"))
    ).one()
    instrument_id = instrument.instrument_id
    before_stats = await test_session.get(InstrumentStats, instrument_id)
    before_price = before_stats.latest_price

    value = _canonical_request(idempotency_key="v2-mixed")
    value["schema_version"] = 2
    invalid = value["payload"]["data"][0]
    invalid.update(open=None, high="1")
    second_invalid = {**invalid, "symbol": "BAD", "source_symbol": "bad-listing"}
    value["payload"]["data"].append(second_invalid)
    if include_valid:
        valid = _canonical_request()["payload"]["data"][0]
        valid.update(symbol="GOOD", source_symbol="good-listing")
        value["payload"]["data"].append(valid)
    value["payload"]["batch"]["declared_record_count"] = len(value["payload"]["data"])
    accepted = await owned_client.post("/api/v1/source/ingest", headers=source_headers, json=value)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    await _execute_run(test_session, test_engine, run_id)

    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed_with_errors"
    assert (run.total_records, run.success_records, run.failed_records) == (
        3 if include_valid else 2,
        int(include_valid),
        2,
    )
    raw = await test_session.get(RawMarketPayload, run.raw_payload_id)
    raw_id = raw.raw_payload_id
    assert raw.payload["data"][0]["high"] == "1"
    errors = list(
        await test_session.scalars(
            select(DQIssue).where(DQIssue.run_id == run_id, DQIssue.severity == "error")
        )
    )
    assert {issue.raw_data["symbol"] for issue in errors} == {"2330", "BAD"}
    assert all(issue.raw_data["source_symbol"] and issue.trade_date for issue in errors)
    assert not await test_session.scalar(select(Instrument).where(Instrument.symbol == "BAD"))
    existing = await test_session.scalar(
        select(MarketDataEOD).where(MarketDataEOD.instrument_id == instrument_id)
    )
    assert existing.run_id == original_id
    assert (await test_session.get(InstrumentStats, instrument_id)).latest_price == before_price
    evidence = set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(DeliveryMemberEvidence.run_id == run_id)
        )
    )
    assert evidence == ({"GOOD"} if include_valid else set())
    writes = list(
        await test_session.scalars(select(MarketDataEOD).where(MarketDataEOD.run_id == run_id))
    )
    assert len(writes) == int(include_valid)

    rerun_response = await owned_client.post(
        f"/api/v1/source/runs/{run_id}/rerun", headers=source_headers
    )
    assert rerun_response.status_code == 202, rerun_response.text
    rerun_id = UUID(rerun_response.json()["run_id"])
    await _execute_run(test_session, test_engine, rerun_id)
    rerun = await test_session.get(IngestionRun, rerun_id)
    assert rerun.schema_version == 2
    assert rerun.raw_payload_id == raw_id
    assert (rerun.status, rerun.success_records, rerun.failed_records) == (
        "completed_with_errors",
        int(include_valid),
        2,
    )
    assert (
        set(
            await test_session.scalars(
                select(DeliveryMemberEvidence.member_key).where(
                    DeliveryMemberEvidence.run_id == rerun_id
                )
            )
        )
        == evidence
    )
