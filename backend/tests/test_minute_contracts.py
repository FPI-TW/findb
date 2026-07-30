"""Validation coverage for the published, not-yet-routable minute contracts."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.archive import MarketMinuteArchiveManifest
from app.schemas.ingress import MarketMinuteIngressRequest, minute_sequence_key_digest
from app.services.archive_contracts import get_archive_contract_json_schema
from app.services.ingress_contracts import DatasetContractDeclaration, get_contract_json_schema


def _sha256(values: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(values)).encode("utf-8")).hexdigest()


def _minute_request() -> dict:
    digest = minute_sequence_key_digest(
        dataset_key="tw_equity_minute",
        data_date=date(2026, 7, 30),
        snapshot_id="snapshot-20260730",
        sequence=1,
    )
    return {
        "dataset_key": "tw_equity_minute",
        "schema_id": "market_minute",
        "schema_version": 1,
        "source": "shioaji",
        "request_key": f"mmr:{digest}",
        "idempotency_key": f"mms:{digest}",
        "fetched_at": "2026-07-30T06:00:00+08:00",
        "payload": {
            "batch": {
                "data_date": "2026-07-30",
                "delivery_mode": "sequenced_snapshot",
                "declared_record_count": 1,
                "coverage_start_date": "2026-07-30",
                "coverage_end_date": "2026-07-30",
                "snapshot_id": "snapshot-20260730",
                "daily_update_id": "update-20260730",
                "universe_id": "tw-equity-main",
                "symbols_sha256": _sha256(["2330", "0050"]),
                "sequence": 1,
                "sequence_count": 2,
                "provider_usage_before": {"requests_used": 1, "requests_limit": 500},
                "provider_usage_after": {"requests_used": 2, "requests_limit": 500},
                "anomalies": [
                    {
                        "symbol": "2330",
                        "bar_start_time": "2026-07-30T09:00:00+08:00",
                        "field": "volume",
                        "raw_value": "bad-volume",
                        "raw_value_sha256": hashlib.sha256(b"bad-volume").hexdigest(),
                        "reason": "not_a_nonnegative_integer",
                    },
                    {
                        "symbol": "2330",
                        "bar_start_time": "2026-07-30T09:00:00+08:00",
                        "field": "turnover",
                        "raw_value": "bad-turnover",
                        "raw_value_sha256": hashlib.sha256(b"bad-turnover").hexdigest(),
                        "reason": "not_a_nonnegative_decimal",
                    },
                ],
            },
            "symbol_statuses": [
                {"symbol": "2330", "outcome": "data"},
                {"symbol": "0050", "outcome": "expected_no_data", "reason": "halted"},
            ],
            "data": [
                {
                    "symbol": "2330",
                    "trade_date": "2026-07-30",
                    "market_timezone": "Asia/Taipei",
                    "bar_start_time": "2026-07-30T09:00:00+08:00",
                    "bar_end_time": "2026-07-30T09:01:00+08:00",
                    "signal_time": "2026-07-30T09:01:00+08:00",
                    "price_adjustment": "none",
                    "trade_count": None,
                    "open": "1000",
                    "high": "1010",
                    "low": "999",
                    "close": "1005",
                    "volume": None,
                    "turnover": None,
                }
            ],
        },
    }


def test_minute_contract_normalizes_aware_times_to_utc() -> None:
    request = MarketMinuteIngressRequest.model_validate(_minute_request())
    row = request.payload.data[0]

    assert row.bar_start_time.isoformat() == "2026-07-30T01:00:00+00:00"
    assert row.signal_time == row.bar_end_time


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("bar_start_time",), "2026-07-30T09:00:00", "timezone"),
        (("bar_end_time",), "2026-07-30T09:02:00+08:00", "plus one minute"),
    ],
)
def test_minute_contract_rejects_invalid_time_invariants(path, value, message) -> None:
    request = _minute_request()
    request["payload"]["data"][0][path[0]] = value

    with pytest.raises(ValidationError, match=message):
        MarketMinuteIngressRequest.model_validate(request)


def test_minute_contract_rejects_duplicate_key_and_bad_status_checksum() -> None:
    duplicate = _minute_request()
    duplicate["payload"]["data"].append(deepcopy(duplicate["payload"]["data"][0]))
    duplicate["payload"]["batch"]["declared_record_count"] = 2
    with pytest.raises(ValidationError, match="duplicate"):
        MarketMinuteIngressRequest.model_validate(duplicate)

    bad_checksum = _minute_request()
    bad_checksum["payload"]["batch"]["symbols_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="symbols_sha256"):
        MarketMinuteIngressRequest.model_validate(bad_checksum)


def test_minute_contract_requires_complete_ohlc() -> None:
    for field in ("open", "high", "low", "close"):
        request = _minute_request()
        del request["payload"]["data"][0][field]
        with pytest.raises(ValidationError, match=field):
            MarketMinuteIngressRequest.model_validate(request)


def test_minute_anomaly_requires_checksum_existing_null_row_and_usage_pair() -> None:
    bad_checksum = _minute_request()
    bad_checksum["payload"]["batch"]["anomalies"][0]["raw_value_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="raw_value_sha256"):
        MarketMinuteIngressRequest.model_validate(bad_checksum)

    orphan = _minute_request()
    orphan["payload"]["batch"]["anomalies"][0]["bar_start_time"] = "2026-07-30T09:01:00+08:00"
    with pytest.raises(ValidationError, match="existing"):
        MarketMinuteIngressRequest.model_validate(orphan)

    non_null = _minute_request()
    non_null["payload"]["data"][0]["volume"] = 1
    with pytest.raises(ValidationError, match="must be null"):
        MarketMinuteIngressRequest.model_validate(non_null)

    non_null_turnover = _minute_request()
    non_null_turnover["payload"]["data"][0]["turnover"] = "1"
    with pytest.raises(ValidationError, match="must be null"):
        MarketMinuteIngressRequest.model_validate(non_null_turnover)

    for field in ("volume", "turnover"):
        missing_evidence = _minute_request()
        missing_evidence["payload"]["batch"]["anomalies"] = [
            anomaly
            for anomaly in missing_evidence["payload"]["batch"]["anomalies"]
            if anomaly["field"] != field
        ]
        with pytest.raises(ValidationError, match=f"null {field} requires"):
            MarketMinuteIngressRequest.model_validate(missing_evidence)

    duplicate_anomaly = _minute_request()
    duplicate_anomaly["payload"]["batch"]["anomalies"].append(
        deepcopy(duplicate_anomaly["payload"]["batch"]["anomalies"][0])
    )
    with pytest.raises(ValidationError, match="must not repeat"):
        MarketMinuteIngressRequest.model_validate(duplicate_anomaly)

    incomplete_usage = _minute_request()
    del incomplete_usage["payload"]["batch"]["provider_usage_after"]
    with pytest.raises(ValidationError, match="provided together"):
        MarketMinuteIngressRequest.model_validate(incomplete_usage)

    missing_shioaji_usage = _minute_request()
    del missing_shioaji_usage["payload"]["batch"]["provider_usage_before"]
    del missing_shioaji_usage["payload"]["batch"]["provider_usage_after"]
    with pytest.raises(ValidationError, match="shioaji"):
        MarketMinuteIngressRequest.model_validate(missing_shioaji_usage)

    provider_without_usage = _minute_request()
    provider_without_usage["source"] = "other_provider"
    del provider_without_usage["payload"]["batch"]["provider_usage_before"]
    del provider_without_usage["payload"]["batch"]["provider_usage_after"]
    assert (
        MarketMinuteIngressRequest.model_validate(provider_without_usage).source == "other_provider"
    )


def test_minute_identity_and_usage_are_canonical_and_monotonic() -> None:
    bad_request_key = _minute_request()
    bad_request_key["request_key"] = "arbitrary"
    with pytest.raises(ValidationError, match="canonical minute request"):
        MarketMinuteIngressRequest.model_validate(bad_request_key)

    bad_idempotency_key = _minute_request()
    bad_idempotency_key["idempotency_key"] = "arbitrary"
    with pytest.raises(ValidationError, match="canonical minute sequence"):
        MarketMinuteIngressRequest.model_validate(bad_idempotency_key)

    long_snapshot = _minute_request()
    snapshot_id = "s" * 100
    long_snapshot["payload"]["batch"]["snapshot_id"] = snapshot_id
    digest = minute_sequence_key_digest(
        dataset_key="tw_equity_minute",
        data_date=date(2026, 7, 30),
        snapshot_id=snapshot_id,
        sequence=1,
    )
    long_snapshot["request_key"] = f"mmr:{digest}"
    long_snapshot["idempotency_key"] = f"mms:{digest}"
    request = MarketMinuteIngressRequest.model_validate(long_snapshot)
    assert len(request.request_key) == len(request.idempotency_key) == 68
    assert digest == minute_sequence_key_digest(
        dataset_key="tw_equity_minute",
        data_date=date(2026, 7, 30),
        snapshot_id=snapshot_id,
        sequence=1,
    )

    mismatched_identity = _minute_request()
    mismatched_identity["payload"]["batch"]["sequence"] = 2
    with pytest.raises(ValidationError, match="canonical minute request"):
        MarketMinuteIngressRequest.model_validate(mismatched_identity)

    decreasing_usage = _minute_request()
    decreasing_usage["payload"]["batch"]["provider_usage_after"]["requests_used"] = 0
    with pytest.raises(ValidationError, match="must not decrease"):
        MarketMinuteIngressRequest.model_validate(decreasing_usage)

    changed_limit = _minute_request()
    changed_limit["payload"]["batch"]["provider_usage_after"]["requests_limit"] = 400
    with pytest.raises(ValidationError, match="limit must remain stable"):
        MarketMinuteIngressRequest.model_validate(changed_limit)


def _archive_manifest() -> dict:
    object_data = {
        "object_key": "minute/2026-07/chunk-1.parquet",
        "sha256": "a" * 64,
        "byte_size": 42,
    }
    return {
        "schema_id": "market_minute_archive",
        "schema_version": 1,
        "release_id": "tw-minute-202607-final",
        "dataset_key": "tw_equity_minute",
        "source": "tw_recorder_archive",
        "upstream_source": "shioaji",
        "overlap_precedence": "direct_daily_shioaji",
        "trading_calendar_checksum": "f" * 64,
        "coverage_start_month": "2026-06-01",
        "coverage_end_month": "2026-07-01",
        "covered_months": ["2026-06-01", "2026-07-01"],
        "expected_trading_dates": ["2026-06-30", "2026-07-01"],
        "covered_trading_dates": ["2026-06-30", "2026-07-01"],
        "trading_dates_sha256": hashlib.sha256(b"2026-06-30\n2026-07-01").hexdigest(),
        "instruments": ["0050", "2330"],
        "instruments_sha256": _sha256(["0050", "2330"]),
        "sequence_count": 1,
        "chunk_count": 1,
        "row_count": 5_000,
        "checksum_sha256": "b" * 64,
        "chunks": [
            {
                "snapshot_sequence": 1,
                "chunk_sequence": 1,
                "chunk_count": 1,
                "row_count": 5_000,
                "checksum_sha256": "c" * 64,
                "object": deepcopy(object_data),
            }
        ],
        "objects": [deepcopy(object_data)],
        "release_state": "finalized",
        "finalized_at": "2026-08-01T00:00:00Z",
    }


def test_archive_manifest_enforces_continuous_coverage_and_chunk_governance() -> None:
    manifest = MarketMinuteArchiveManifest.model_validate(_archive_manifest())
    assert manifest.finalized_at.tzinfo is not None

    missing_month = _archive_manifest()
    missing_month["covered_months"] = ["2026-07-01"]
    with pytest.raises(ValidationError, match="continuous"):
        MarketMinuteArchiveManifest.model_validate(missing_month)

    too_many_rows = _archive_manifest()
    too_many_rows["chunks"][0]["row_count"] = 5_001
    too_many_rows["row_count"] = 5_001
    with pytest.raises(ValidationError, match="less_than_equal"):
        MarketMinuteArchiveManifest.model_validate(too_many_rows)

    mismatched_object = _archive_manifest()
    mismatched_object["objects"][0]["sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="exactly match"):
        MarketMinuteArchiveManifest.model_validate(mismatched_object)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "other_archive"),
        ("upstream_source", "other_provider"),
        ("overlap_precedence", "archive_wins"),
    ],
)
def test_archive_manifest_requires_fixed_provenance(field: str, value: str) -> None:
    manifest = _archive_manifest()
    manifest[field] = value
    with pytest.raises(ValidationError):
        MarketMinuteArchiveManifest.model_validate(manifest)

    missing_source = _archive_manifest()
    del missing_source["source"]
    with pytest.raises(ValidationError):
        MarketMinuteArchiveManifest.model_validate(missing_source)


def test_archive_manifest_requires_calendar_backed_no_gap_dates() -> None:
    missing_day = _archive_manifest()
    missing_day["covered_trading_dates"] = ["2026-06-30"]
    with pytest.raises(ValidationError, match="exactly equal"):
        MarketMinuteArchiveManifest.model_validate(missing_day)

    unsorted = _archive_manifest()
    unsorted["expected_trading_dates"] = ["2026-07-01", "2026-06-30"]
    with pytest.raises(ValidationError, match="strictly ascending"):
        MarketMinuteArchiveManifest.model_validate(unsorted)

    duplicate = _archive_manifest()
    duplicate["expected_trading_dates"] = ["2026-06-30", "2026-06-30", "2026-07-01"]
    with pytest.raises(ValidationError, match="strictly ascending"):
        MarketMinuteArchiveManifest.model_validate(duplicate)

    out_of_range = _archive_manifest()
    out_of_range["expected_trading_dates"] = ["2026-06-30", "2026-07-01", "2026-08-01"]
    with pytest.raises(ValidationError, match="covered months"):
        MarketMinuteArchiveManifest.model_validate(out_of_range)

    bad_checksum = _archive_manifest()
    bad_checksum["trading_dates_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="trading_dates_sha256"):
        MarketMinuteArchiveManifest.model_validate(bad_checksum)


def test_archive_allows_many_instruments_and_multiple_chunks_per_snapshot() -> None:
    manifest_value = _archive_manifest()
    instruments = [f"{number:04d}" for number in range(100)]
    manifest_value["instruments"] = instruments
    manifest_value["instruments_sha256"] = _sha256(instruments)
    second_object = {
        "object_key": "minute/2026-07/chunk-2.parquet",
        "sha256": "d" * 64,
        "byte_size": 42,
    }
    manifest_value["sequence_count"] = 1
    manifest_value["chunk_count"] = 2
    manifest_value["row_count"] = 5_001
    manifest_value["chunks"].append(
        {
            "snapshot_sequence": 1,
            "chunk_sequence": 2,
            "chunk_count": 2,
            "row_count": 1,
            "checksum_sha256": "e" * 64,
            "object": second_object,
        }
    )
    manifest_value["chunks"][0]["chunk_count"] = 2
    manifest_value["objects"].append(second_object)

    manifest = MarketMinuteArchiveManifest.model_validate(manifest_value)
    assert len(manifest.instruments) == 100
    assert manifest.sequence_count != manifest.chunk_count


def test_minute_dataset_declaration_requires_fixed_defaults() -> None:
    declaration = {
        "schema_id": "market_minute",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "defaults": {
            "market": "TW",
            "asset_class": "equity",
            "market_timezone": "Asia/Taipei",
            "price_adjustment": "none",
        },
    }
    assert (
        DatasetContractDeclaration.model_validate(declaration).defaults.market_timezone
        == "Asia/Taipei"
    )
    del declaration["defaults"]["price_adjustment"]
    with pytest.raises(ValidationError, match="market_minute defaults"):
        DatasetContractDeclaration.model_validate(declaration)


def test_minute_and_archive_schemas_publish_scope_metadata() -> None:
    minute_schema = get_contract_json_schema("market_minute", 1)
    archive_schema = get_archive_contract_json_schema()

    assert minute_schema["$id"] == "urn:findb:ingress-contract:market_minute:v1"
    rules = {item["id"]: item for item in minute_schema["x-findb-semantic-rules"]}
    assert rules["payload.delivery_key.unique"]["parameters"]["fields"] == [
        "symbol",
        "bar_start_time",
    ]
    assert (
        rules["minute.sequence.identity_and_governance"]["parameters"]["delivery_mode"]
        == "sequenced_snapshot"
    )
    assert rules["minute.sequence.identity_and_governance"]["parameters"][
        "request_key_format"
    ].startswith("mmr:")
    assert rules["minute.status_and_anomaly.references"]["parameters"][
        "bidirectional_null_fields"
    ] == ["volume", "turnover"]
    assert archive_schema["x-findb-contract-scope"]["runtime_ingest_support"] == "not_implemented"
    archive_rules = {item["id"] for item in archive_schema["x-findb-semantic-rules"]}
    assert "archive.trading_calendar.strict_no_gap_barrier" in archive_rules
    assert "archive.source.direct_daily_precedence" in archive_rules
