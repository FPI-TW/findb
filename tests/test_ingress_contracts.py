"""Tests for provider-neutral, versioned ingress contracts."""

from datetime import timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas.ingress import FuturesContinuousEODIngressRequest, MarketEODIngressRequest
from app.services.ingestion import _select_normalizer_for_payload
from app.services.ingress_contracts import (
    DatasetContractDeclaration,
    UnsupportedIngressContractError,
    parse_dataset_contract_declaration,
    supported_contracts,
    validate_ingress_request,
)
from app.services.normalize import MarketEODContractNormalizer
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

    with pytest.raises(ValidationError, match="declared_record_count"):
        MarketEODIngressRequest.model_validate(value)


def test_market_eod_rejects_duplicate_natural_key():
    value = _market_eod_request()
    value["payload"]["data"].append(dict(value["payload"]["data"][0]))
    value["payload"]["batch"]["declared_record_count"] = 2

    with pytest.raises(ValidationError, match=r"duplicate \(symbol, trade_date\)"):
        MarketEODIngressRequest.model_validate(value)


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


@pytest.mark.parametrize(
    "dataset_key",
    ["tw_equity_eod", "tw_etf_eod", "futures_continuous_eod", "wtx_eod"],
)
def test_initial_dataset_contract_declarations_are_audit_only(dataset_key: str):
    config = next(item["config"] for item in DATASETS if item["dataset_key"] == dataset_key)

    declaration = parse_dataset_contract_declaration(config)

    assert declaration is not None
    assert declaration.schema_enforcement == "audit"
