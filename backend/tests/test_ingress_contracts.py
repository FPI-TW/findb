"""Tests for provider-neutral, versioned ingress contracts."""

import hashlib
import json
from datetime import timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas.ingress import FuturesContinuousEODIngressRequest, MarketEODIngressRequest
from app.services.canonical_ingestion import _select_validation_error
from app.services.ingestion import _select_normalizer_for_payload
from app.services.ingress_contracts import (
    DatasetContractDeclaration,
    UnsupportedIngressContractError,
    get_contract_json_schema,
    parse_dataset_contract_declaration,
    supported_contracts,
    validate_ingress_request,
)
from app.services.normalize import (
    FuturesContinuousEODContractNormalizer,
    FuturesContinuousNormalizer,
    MarketEODContractNormalizer,
)
from scripts.seed_data import DATASETS


def _market_eod_request() -> dict:
    return {
        "dataset_key": "tw_equity_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": "finlab",
        "request_key": "finlab_tw_equity_eod_20260721_01",
        "idempotency_key": "finlab_tw_equity_eod_20260721",
        "fetched_at": "2026-07-21T16:00:00+08:00",
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
                    "open": "1000.0",
                    "high": "1020.0",
                    "low": "995.0",
                    "close": "1015.0",
                    "volume": 32_100_000,
                    "turnover": "32480000000",
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
                    "open": "23000",
                    "high": "23200",
                    "low": "22900",
                    "close": "23150",
                    "volume": 80_000,
                    "open_interest": 120_000,
                    "active_contract_code": "TXF202607",
                    "roll_rule": "front_month",
                    "roll_adjustment": "1.75",
                }
            ],
        },
    }


def test_market_eod_v1_accepts_provider_neutral_payload():
    request = MarketEODIngressRequest.model_validate(_market_eod_request())

    assert request.fetched_at.utcoffset() == timezone.utc.utcoffset(request.fetched_at)
    assert request.fetched_at.hour == 8
    assert request.payload.data[0].close == Decimal("1015.0")
    assert request.payload.data[0].source_symbol == "2330 TT Equity"


@pytest.mark.parametrize("currency", ["USDT", "USDC"])
def test_currency_rule_accepts_uppercase_alphanumeric_for_rows_and_defaults(
    currency: str,
):
    value = _market_eod_request()
    value["payload"]["data"][0]["currency"] = currency
    request = MarketEODIngressRequest.model_validate(value)
    declaration = DatasetContractDeclaration.model_validate(
        {
            "schema_id": "market_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "defaults": {
                "market": "TW",
                "asset_class": "equity",
                "currency": currency,
            },
        }
    )

    assert request.payload.data[0].currency == currency
    assert declaration.defaults.currency == currency


def test_currency_rule_rejects_non_alphanumeric_for_rows_and_defaults():
    value = _market_eod_request()
    value["payload"]["data"][0]["currency"] = "$$$"
    with pytest.raises(ValidationError):
        MarketEODIngressRequest.model_validate(value)
    with pytest.raises(ValidationError):
        DatasetContractDeclaration.model_validate(
            {
                "schema_id": "market_eod",
                "accepted_schema_versions": [1],
                "current_schema_version": 1,
                "defaults": {
                    "market": "TW",
                    "asset_class": "equity",
                    "currency": "$$$",
                },
            }
        )


def test_ingestion_attempt_reconciliation_settings_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(INGESTION_ATTEMPT_STALE_SECONDS=0)
    with pytest.raises(ValidationError):
        Settings(INGESTION_ATTEMPT_RECONCILE_BATCH_SIZE=0)


@pytest.mark.parametrize("field", ["schema_id", "schema_version"])
def test_contract_identifier_is_required(field: str):
    value = _market_eod_request()
    del value[field]

    with pytest.raises(ValidationError):
        MarketEODIngressRequest.model_validate(value)


def test_envelope_requires_lowercase_stable_source():
    value = _market_eod_request()
    value["source"] = "Bloomberg"

    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        MarketEODIngressRequest.model_validate(value)


def test_market_eod_rejects_provider_specific_nested_fields():
    value = _market_eod_request()
    value["payload"]["data"][0]["price"] = {"last": 1015}

    with pytest.raises(ValidationError, match="extra_forbidden"):
        MarketEODIngressRequest.model_validate(value)


def test_market_eod_rejects_declared_record_count_mismatch():
    value = _market_eod_request()
    value["payload"]["batch"]["declared_record_count"] = 2

    with pytest.raises(ValidationError, match="declared_record_count") as caught:
        MarketEODIngressRequest.model_validate(value)

    assert caught.value.errors(include_input=False)[0]["type"] == ("declared_record_count_mismatch")


def test_market_eod_rejects_duplicate_natural_key():
    value = _market_eod_request()
    value["payload"]["data"].append(dict(value["payload"]["data"][0]))
    value["payload"]["batch"]["declared_record_count"] = 2

    with pytest.raises(ValidationError, match=r"duplicate \(symbol, trade_date\)") as caught:
        MarketEODIngressRequest.model_validate(value)

    assert caught.value.errors(include_input=False)[0]["type"] == "duplicate_delivery_key"


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("high", "990", "high must not be below"),
        ("low", "1016", "low must not be above"),
        ("close", "-1", "greater_than_equal"),
    ],
)
def test_market_eod_rejects_invalid_prices(field: str, invalid_value: str, message: str):
    value = _market_eod_request()
    value["payload"]["data"][0][field] = invalid_value

    with pytest.raises(ValidationError, match=message):
        MarketEODIngressRequest.model_validate(value)


def test_multi_date_payload_requires_coverage():
    value = _market_eod_request()
    value["payload"]["data"][0]["trade_date"] = "2026-07-20"

    with pytest.raises(ValidationError, match="coverage dates are required"):
        MarketEODIngressRequest.model_validate(value)


def test_backfill_coverage_accepts_multiple_dates():
    value = _market_eod_request()
    value["payload"]["batch"].update(
        {
            "delivery_mode": "backfill",
            "data_date": "2026-07-21",
            "coverage_start_date": "2026-07-20",
            "coverage_end_date": "2026-07-21",
            "declared_record_count": 2,
        }
    )
    second_row = dict(value["payload"]["data"][0])
    second_row["trade_date"] = "2026-07-20"
    value["payload"]["data"].append(second_row)

    request = MarketEODIngressRequest.model_validate(value)

    assert len(request.payload.data) == 2


@pytest.mark.parametrize(
    ("batch_update", "message"),
    [
        ({"sequence": 1}, "sequence and sequence_count must be provided together"),
        (
            {"sequence": 2, "sequence_count": 1},
            "sequence must be less than or equal to sequence_count",
        ),
        (
            {"coverage_start_date": "2026-07-21"},
            "coverage_start_date and coverage_end_date must be provided together",
        ),
        (
            {
                "coverage_start_date": "2026-07-22",
                "coverage_end_date": "2026-07-21",
            },
            "coverage_start_date must not be after coverage_end_date",
        ),
        (
            {
                "delivery_mode": "backfill",
                "coverage_start_date": "2026-07-20",
                "coverage_end_date": "2026-07-20",
            },
            "backfill data_date must equal coverage_end_date",
        ),
    ],
)
def test_batch_semantic_relationships_are_enforced(
    batch_update: dict,
    message: str,
):
    value = _market_eod_request()
    value["payload"]["batch"].update(batch_update)

    with pytest.raises(ValidationError, match=message):
        MarketEODIngressRequest.model_validate(value)


def test_coverage_must_contain_every_row_date():
    value = _market_eod_request()
    value["payload"]["data"][0]["trade_date"] = "2026-07-19"
    value["payload"]["batch"].update(
        {
            "coverage_start_date": "2026-07-20",
            "coverage_end_date": "2026-07-21",
        }
    )

    with pytest.raises(ValidationError, match="coverage dates must include every row trade_date"):
        MarketEODIngressRequest.model_validate(value)


def test_contract_payload_obeys_configured_item_limit():
    settings = get_settings()
    original_limit = settings.SOURCE_MAX_DATA_ITEMS
    settings.SOURCE_MAX_DATA_ITEMS = 0

    try:
        with pytest.raises(ValidationError, match="maximum size of 1 items"):
            value = _market_eod_request()
            value["payload"]["data"].append({**value["payload"]["data"][0], "symbol": "2317"})
            value["payload"]["batch"]["declared_record_count"] = 2
            MarketEODIngressRequest.model_validate(value)
    finally:
        settings.SOURCE_MAX_DATA_ITEMS = original_limit


def test_contract_payload_obeys_configured_serialized_byte_limit():
    settings = get_settings()
    original_limit = settings.SOURCE_MAX_PAYLOAD_BYTES
    settings.SOURCE_MAX_PAYLOAD_BYTES = 1

    try:
        with pytest.raises(ValidationError, match="maximum size of 1 bytes"):
            MarketEODIngressRequest.model_validate(_market_eod_request())
    finally:
        settings.SOURCE_MAX_PAYLOAD_BYTES = original_limit


def test_futures_continuous_eod_v1_accepts_contract_fields():
    request = FuturesContinuousEODIngressRequest.model_validate(_futures_request())

    row = request.payload.data[0]
    assert row.active_contract_code == "TXF202607"
    assert row.roll_rule == "front_month"


def test_futures_continuous_eod_rejects_missing_roll_rule():
    value = _futures_request()
    del value["payload"]["data"][0]["roll_rule"]

    with pytest.raises(ValidationError):
        FuturesContinuousEODIngressRequest.model_validate(value)


def test_registry_dispatches_explicit_contract_version():
    request = validate_ingress_request(_market_eod_request())

    assert isinstance(request, MarketEODIngressRequest)
    assert supported_contracts() == (("futures_continuous_eod", 1), ("market_eod", 1))


@pytest.mark.parametrize(
    ("schema_id", "title", "schema_specific_rule", "expected_sha256"),
    [
        (
            "market_eod",
            "MarketEODIngressRequest",
            "market.currency.row_or_dataset_default",
            "9b48d9aebf3d3d1f7e619d0a1fea4c86b72e0e046e58122dfa9ac359796781de",
        ),
        (
            "futures_continuous_eod",
            "FuturesContinuousEODIngressRequest",
            "futures.currency.dataset_default_required",
            "8d48f044b57bd529c9940e4373d76683159717f20e61df773a9a33c6f9daa3bd",
        ),
    ],
)
def test_registry_publishes_versioned_deterministic_json_schema(
    schema_id: str,
    title: str,
    schema_specific_rule: str,
    expected_sha256: str,
):
    first = get_contract_json_schema(schema_id, 1)
    second = get_contract_json_schema(schema_id, 1)

    assert first == second
    assert first["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert first["$id"] == f"urn:findb:ingress-contract:{schema_id}:v1"
    assert first["title"] == title
    assert first["x-findb-contract"] == {
        "schema_id": schema_id,
        "schema_version": 1,
    }
    assert first["$defs"]
    assert first["properties"]["schema_id"]["const"] == schema_id
    assert first["properties"]["schema_version"]["const"] == 1
    rules = first["x-findb-semantic-rules"]
    rule_ids = {rule["id"] for rule in rules}
    assert rule_ids == {
        "envelope.fetched_at.timezone_aware",
        "batch.sequence.co_presence",
        "batch.sequence.order",
        "batch.coverage.co_presence",
        "batch.coverage.order",
        "batch.backfill.data_date_equals_coverage_end",
        "row.ohlc.high_bound",
        "row.ohlc.low_bound",
        "payload.coverage.contains_row_dates",
        "payload.declared_record_count.matches_data",
        "payload.delivery_key.unique",
        "payload.data.max_items",
        "payload.serialized.max_bytes",
        "request.body.max_bytes",
        schema_specific_rule,
    }
    for rule in rules:
        assert set(rule) >= {
            "id",
            "scope",
            "description",
            "parameters",
            "context_dependencies",
            "error_code",
        }
    by_id = {rule["id"]: rule for rule in rules}
    assert by_id["payload.delivery_key.unique"]["parameters"]["fields"] == [
        "symbol",
        "trade_date",
    ]
    assert by_id["payload.data.max_items"]["context_dependencies"] == [
        {"kind": "runtime_setting", "name": "SOURCE_MAX_DATA_ITEMS"}
    ]
    assert by_id[schema_specific_rule]["context_dependencies"] == [
        {"kind": "dataset_context", "path": "defaults.currency"}
    ]
    scope = first["x-findb-contract-scope"]
    assert scope["artifact_kind"] == "versioned_request_body_shape_and_semantics"
    assert scope["necessary_for_api_acceptance"] is True
    assert scope["sufficient_for_api_acceptance"] is False
    assert {item["id"] for item in scope["covers"]} == {
        "request_body.json_parsing",
        "request_body.shape",
        "request_body.normalization",
        "request_body.semantics",
    }
    assert set(scope["excludes"]) == {
        "authentication_and_database_credential_lookup",
        "rate_limiting",
        "credential_source_and_dataset_authorization",
        "dataset_registry_state_and_contract_declaration",
        "idempotency_state",
        "infrastructure_availability",
    }
    boundaries = {item["id"]: item for item in scope["additional_acceptance_boundaries"]}
    assert set(boundaries) == {
        "authentication.api_key",
        "authentication.database_lookup",
        "rate_limit.credential_or_client_ip",
        "request.client_ip.available",
        "credential.source_binding",
        "credential.dataset_allowlist",
        "dataset.existence",
        "dataset.active",
        "dataset.contract_declaration",
        "dataset.contract_scope",
        "dataset.accepted_contract_version",
        "idempotency.collision",
        "infrastructure.database_or_internal_failure",
    }
    assert boundaries["authentication.api_key"]["attempt_semantics"] == "not_created"
    assert boundaries["rate_limit.credential_or_client_ip"]["actors"] == [
        "source_client_or_legacy_credential",
        "client_ip",
    ]
    assert boundaries["dataset.existence"]["public_codes"] == ["DATASET_NOT_FOUND"]
    assert (
        boundaries["infrastructure.database_or_internal_failure"]["attempt_semantics"]
        == "depends_on_failure_stage"
    )
    transformations = first["x-findb-transformations"]
    assert [item["id"] for item in transformations] == ["normalization.strings.strip_whitespace"]
    transformed_paths = set(transformations[0]["paths"])
    assert {
        "dataset_key",
        "source",
        "request_key",
        "idempotency_key",
        "payload.batch.source_raw_ref",
        "payload.batch.source_raw_sha256",
        "payload.data[*].symbol",
        "payload.data[*].source_symbol",
        "payload.data[*].name",
    } <= transformed_paths
    assert "schema_id" not in transformed_paths
    assert "schema_version" not in transformed_paths
    assert scope["dispatch_discriminators"] == [
        {
            "path": "schema_id",
            "type": "string",
            "matching": "exact",
            "normalization": "none_before_dispatch",
            "expected": schema_id,
        },
        {
            "path": "schema_version",
            "type": "integer_non_boolean",
            "matching": "exact",
            "normalization": "none_before_dispatch",
            "expected": 1,
        },
    ]
    expected_specific_path = (
        "payload.data[*].currency" if schema_id == "market_eod" else "payload.data[*].roll_rule"
    )
    assert expected_specific_path in transformed_paths
    canonical_json = json.dumps(first, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical_json).hexdigest() == expected_sha256


def test_validation_error_priority_selects_one_structured_error_for_code_and_message():
    errors = [
        {"type": "duplicate_delivery_key", "loc": ("payload",)},
        {"type": "declared_record_count_mismatch", "loc": ("payload",)},
        {"type": "string_pattern_mismatch", "loc": ("source",)},
    ]

    code, selected = _select_validation_error(errors)

    assert code == "DECLARED_RECORD_COUNT_MISMATCH"
    assert selected is errors[1]


def test_contract_normalizes_declared_string_whitespace_before_validation():
    value = _market_eod_request()
    value["request_key"] = "  request-key  "
    value["payload"]["batch"]["source_raw_ref"] = "  s3://bucket/raw.json  "
    value["payload"]["data"][0]["symbol"] = "  2330  "

    request = MarketEODIngressRequest.model_validate(value)

    assert request.request_key == "request-key"
    assert request.payload.batch.source_raw_ref == "s3://bucket/raw.json"
    assert request.payload.data[0].symbol == "2330"


def test_contract_registry_does_not_normalize_dispatch_discriminators():
    value = _market_eod_request()
    value["schema_id"] = " market_eod "

    with pytest.raises(UnsupportedIngressContractError, match=r" market_eod \.v1"):
        validate_ingress_request(value)


def test_registry_does_not_guess_unknown_or_missing_versions():
    value = _market_eod_request()
    value["schema_version"] = 2

    with pytest.raises(UnsupportedIngressContractError, match="market_eod.v2"):
        validate_ingress_request(value)

    del value["schema_version"]
    with pytest.raises(UnsupportedIngressContractError, match="required"):
        validate_ingress_request(value)


@pytest.mark.parametrize("invalid_version", [True, "1", 1.0])
def test_contract_version_requires_an_integer(invalid_version: object):
    value = _market_eod_request()
    value["schema_version"] = invalid_version

    with pytest.raises((ValidationError, UnsupportedIngressContractError)):
        validate_ingress_request(value)


def test_dataset_contract_declaration_defaults_to_audit_mode():
    declaration = DatasetContractDeclaration.model_validate(
        {
            "schema_id": "market_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "defaults": {"market": "TW", "asset_class": "equity"},
        }
    )

    assert declaration.schema_enforcement == "audit"


def test_dataset_contract_declaration_requires_current_version_to_be_accepted():
    with pytest.raises(ValidationError, match="current_schema_version must be accepted"):
        DatasetContractDeclaration.model_validate(
            {
                "schema_id": "market_eod",
                "accepted_schema_versions": [1],
                "current_schema_version": 2,
                "defaults": {"market": "TW", "asset_class": "equity"},
            }
        )


def test_dataset_contract_declaration_rejects_unregistered_contract():
    with pytest.raises(UnsupportedIngressContractError, match="unknown.v1"):
        parse_dataset_contract_declaration(
            {
                "schema_id": "unknown",
                "accepted_schema_versions": [1],
                "current_schema_version": 1,
                "defaults": {"market": "TW", "asset_class": "equity"},
            }
        )


def test_legacy_dataset_config_has_no_contract_declaration():
    assert parse_dataset_contract_declaration({"source_format": "legacy"}) is None


def test_contract_normalizer_routing_requires_schema_id_and_version():
    assert (
        _select_normalizer_for_payload(
            "tw_equity_eod",
            {},
            schema_id="market_eod",
            schema_version=1,
        )
        is MarketEODContractNormalizer
    )
    assert (
        _select_normalizer_for_payload(
            "tw_equity_eod",
            {},
            schema_id="market_eod",
            schema_version=2,
        )
        is None
    )
    assert (
        _select_normalizer_for_payload(
            "tw_equity_eod",
            {},
            schema_id="market_eod",
        )
        is None
    )


def test_futures_contract_normalizer_maps_optional_canonical_fields():
    request = FuturesContinuousEODIngressRequest.model_validate(_futures_request())
    normalizer = FuturesContinuousEODContractNormalizer(
        None,
        {
            "defaults": {"market": "WTX", "asset_class": "future", "currency": "TWD"},
            "_ingest_source": "bloomberg",
        },
    )

    record = normalizer.map_fields(request.payload.model_dump(mode="json"))[0]

    assert record.open_interest == 120_000
    assert record.active_contract_code == "TXF202607"
    assert record.roll_adjustment == Decimal("1.75")


def test_generic_futures_normalizer_defaults_new_fields_with_seeded_full_mapping():
    config = next(
        item["config"] for item in DATASETS if item["dataset_key"] == "futures_continuous_eod"
    )
    normalizer = FuturesContinuousNormalizer(None, config)

    record = normalizer.map_fields(
        {
            "metadata": {"source": "bloomberg"},
            "data": [
                {
                    "symbol": "TX",
                    "ticker": "TXA Index",
                    "trade_date": "2026-07-21",
                    "close": "23150",
                    "open_interest": 120_000,
                    "active_contract_code": "TXF202607",
                    "roll_adjustment": "1.75",
                    "roll_rule": "front_month",
                }
            ],
        }
    )[0]

    assert record.open_interest == 120_000
    assert record.active_contract_code == "TXF202607"
    assert record.roll_adjustment == Decimal("1.75")


@pytest.mark.parametrize(
    "dataset_key",
    [
        "us_equity_eod",
        "tw_equity_eod",
        "tw_etf_eod",
        "futures_continuous_eod",
        "wtx_eod",
    ],
)
def test_initial_dataset_contract_declarations_are_audit_only(dataset_key: str):
    config = next(item["config"] for item in DATASETS if item["dataset_key"] == dataset_key)

    declaration = parse_dataset_contract_declaration(config)

    assert declaration is not None
    assert declaration.schema_enforcement == "audit"
