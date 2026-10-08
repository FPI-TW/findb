"""Fixed runtime feed identities constrain otherwise valid registry declarations."""

from copy import deepcopy

import pytest

from app.models.registry import DatasetRegistry
from app.services import ingress_contracts
from app.services.full_market_governance import FULL_MARKET_FEEDS, full_market_configuration
from app.services.ingress_contracts import parse_dataset_contract_declaration
from app.services.source_clients import PROVIDER_DATASET_SCOPE
from scripts.seed_data import DATASETS

RUNTIME_FEEDS = [
    ("twelve_data", "us_equity_eod", "market_eod", "US", "equity", "daily"),
    ("twelve_data", "hk_equity_eod", "market_eod", "HK", "equity", "daily"),
    ("finlab", "tw_equity_eod", "market_eod", "TW", "equity", "daily"),
    ("finlab", "tw_etf_eod", "market_eod", "TW", "etf", "daily"),
    ("shioaji", "tw_equity_minute", "market_minute", "TW", "equity", "minute"),
    ("shioaji", "tw_etf_minute", "market_minute", "TW", "etf", "minute"),
    ("taifex", "tw_futures_eod", "futures_eod", "TW", "future", "daily"),
]


def runtime_dataset(dataset_key, enabled):
    dataset = DatasetRegistry(
        **deepcopy(next(item for item in DATASETS if item["dataset_key"] == dataset_key))
    )
    dataset.is_active = True
    dataset.config["full_market"].update(
        enabled=enabled,
        readiness_approved=enabled,
        mode="acceptance" if enabled else "off",
        activation_date="2026-12-31" if enabled else None,
    )
    return dataset


def change_runtime_identity(dataset, invalid):
    config = deepcopy(dataset.config)
    if invalid == "contract":
        config["schema_id"] = (
            "market_eod" if config["schema_id"] == "market_minute" else "market_minute"
        )
        if config["schema_id"] == "market_minute":
            config["defaults"].update(market_timezone="Asia/Taipei", price_adjustment="none")
    elif invalid in {"market", "asset_class"}:
        value = "CN" if invalid == "market" else "index"
        setattr(dataset, invalid, value)
        config["defaults"][invalid] = value
    else:
        dataset.frequency = "monthly"
    dataset.config = config


def test_runtime_descriptors_cover_exact_supported_provider_scope():
    assert set(FULL_MARKET_FEEDS) == {
        key for datasets in PROVIDER_DATASET_SCOPE.values() for key in datasets
    }


@pytest.mark.parametrize("provider,key,schema,market,asset,frequency", RUNTIME_FEEDS)
@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_v1_feed_declarations_are_accepted(
    provider, key, schema, market, asset, frequency, enabled
):
    dataset = runtime_dataset(key, enabled)
    declaration = parse_dataset_contract_declaration(dataset.config)
    assert declaration is not None
    assert (declaration.schema_id, declaration.accepted_schema_versions) == (schema, [1])
    assert (dataset.market, dataset.asset_class, dataset.frequency) == (market, asset, frequency)
    opted_in, activated, errors, blockers = full_market_configuration(dataset, provider)
    assert (opted_in, activated, errors) == (False, enabled, [])
    assert blockers == []


@pytest.mark.parametrize("provider,key,schema,market,asset,frequency", RUNTIME_FEEDS)
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("invalid", ["contract", "market", "asset_class", "frequency"])
def test_registered_self_consistent_declaration_cannot_change_runtime_identity(
    provider, key, schema, market, asset, frequency, enabled, invalid
):
    dataset = runtime_dataset(key, enabled)
    change_runtime_identity(dataset, invalid)
    # The mismatches are accepted by the generic registered-contract parser.
    assert parse_dataset_contract_declaration(dataset.config) is not None
    _, _, errors, _ = full_market_configuration(dataset, provider)
    assert errors == [
        "runtime_contract_mismatch" if invalid == "contract" else "runtime_feed_scope_mismatch"
    ]


@pytest.mark.parametrize("accepted,current,blocked", [([2], 2, True), ([1, 2], 2, False)])
def test_runtime_version_must_be_accepted_even_if_another_version_is_registered(
    monkeypatch, accepted, current, blocked
):
    # Only v1 is registered today. Model a future registered v2 to exercise
    # runtime acceptance independently of generic unsupported-version rejection.
    models = dict(ingress_contracts._CONTRACT_MODELS)
    models[("market_eod", 2)] = models[("market_eod", 1)]
    monkeypatch.setattr(ingress_contracts, "_CONTRACT_MODELS", models)
    dataset = runtime_dataset("tw_equity_eod", True)
    dataset.config.update(accepted_schema_versions=accepted, current_schema_version=current)
    assert parse_dataset_contract_declaration(dataset.config) is not None
    _, _, errors, _ = full_market_configuration(dataset, "finlab")
    assert errors == (["runtime_contract_mismatch"] if blocked else [])


def test_feed_cannot_be_moved_to_another_provider_scope():
    dataset = runtime_dataset("tw_equity_eod", True)
    dataset.config["allowed_sources"] = ["twelve_data"]
    _, _, errors, _ = full_market_configuration(dataset, "twelve_data")
    assert errors == ["runtime_feed_scope_mismatch"]
