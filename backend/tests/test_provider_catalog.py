"""Catalog publication boundaries, invalid declarations and consumer projections."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from app.config import Settings
from app.provider_catalog import ProviderCatalog, load_catalog
from scripts.export_provider_catalog import CATALOG_PATH, build_artifacts, export

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def declaration():
    return json.loads((ROOT / CATALOG_PATH).read_text())


def test_feed_markets_match_backend_timezone_authority(declaration):
    from app.services.effective_calendar import MARKETS, market_timezone
    from app.services.historical_backfill import PROVIDER_TIMEZONES

    provider_zones: dict[str, set[str]] = {}
    for feed in ProviderCatalog.model_validate(declaration).feeds:
        zone = market_timezone(feed.calendar_market).key
        assert MARKETS[feed.market][0] == zone, feed.dataset_key
        if feed.schedule is not None:
            assert feed.schedule.timezone == zone, feed.dataset_key
        provider_zones.setdefault(feed.provider, set()).add(zone)
    # Historical cutoffs are provider-keyed, so one provider must stay in one timezone.
    assert all(len(zones) == 1 for zones in provider_zones.values())
    assert {provider: zones.pop() for provider, zones in provider_zones.items()} == {
        provider: zone for provider, zone in PROVIDER_TIMEZONES.items() if provider != "shioaji"
    }
    assert PROVIDER_TIMEZONES["shioaji"] == "Asia/Taipei"


def test_unknown_market_timezone_fails_closed():
    from app.services.effective_calendar import market_timezone

    with pytest.raises(ValueError, match="unsupported calendar market"):
        market_timezone("TW_FUTURES")


def test_supported_scopes_contracts_and_deadlines(declaration):
    catalog = load_catalog(ROOT / CATALOG_PATH, contracts_dir=ROOT / "contracts")
    assert {(f.provider, f.dataset_key) for f in catalog.feeds} == {
        ("twelve_data", "us_equity_eod"),
        ("factset", "hk_equity_eod"),
        ("finlab", "tw_equity_eod"),
        ("finlab", "tw_etf_eod"),
        ("taifex", "tw_futures_eod"),
    }
    feeds = {f.dataset_key: f for f in catalog.feeds}
    assert feeds["hk_equity_eod"].contract.current_version == 2
    assert feeds["hk_equity_eod"].contract.accepted_versions == [1, 2]
    # ETF historical backfills the Full release cohort; it needs no static universe file.
    assert feeds["tw_etf_eod"].capabilities.historical == "executable"
    assert feeds["tw_etf_eod"].historical_execution is None
    us = feeds["us_equity_eod"]
    assert us.schedule.timezone == "America/New_York"
    assert us.schedule.local_time == "17:00"
    assert us.completion_deadline.local_time == "09:00"
    assert us.completion_deadline.trade_date_day_offset == 1
    assert us.bounded_monitor.deadline.local_time == "09:15"
    assert us.publication_retry_window_seconds == 10800
    for provider in catalog.providers:
        assert provider.calendar_switch_env in Settings.model_fields


@pytest.mark.parametrize(
    "fault",
    [
        "provider_duplicate",
        "feed_duplicate",
        "unknown_provider",
        "unknown_image",
        "image_chain",
        "duplicate_suffix",
        "duplicate_role",
        "missing_daily_consumer",
        "missing_historical_consumer",
        "unknown_version",
        "boolean_version",
        "unknown_field",
        "secret_value",
        "missing_field",
        "bad_timezone",
        "bad_clock",
        "bad_slot",
        "boolean_window",
        "current_not_accepted",
        "duplicate_contract_version",
        "draining_acquisition",
        "retired_source",
        "historical_without_source",
        "missing_schedule",
        "missing_deadline",
        "disabled_schedule",
        "unsupported_historical_execution",
        "bounded_without_deadline",
    ],
)
def test_invalid_metadata_fails_closed(declaration, fault):
    p, f = declaration["providers"][0], declaration["feeds"][0]
    if fault == "provider_duplicate":
        declaration["providers"].append(deepcopy(p))
    elif fault == "feed_duplicate":
        declaration["feeds"].append(deepcopy(f))
    elif fault == "unknown_provider":
        f["provider"] = "unknown"
    elif fault == "unknown_image":
        p["image_key"] = "unknown"
    elif fault == "image_chain":
        p["image_key"] = "taifex"
    elif fault == "duplicate_suffix":
        declaration["providers"][1]["container_suffix"] = p["container_suffix"]
    elif fault == "duplicate_role":
        p["consumer_roles"].append("historical")
    elif fault == "missing_daily_consumer":
        p["consumer_roles"].remove("full_market")
    elif fault == "missing_historical_consumer":
        p["consumer_roles"].remove("historical")
    elif fault == "unknown_version":
        declaration["schema_version"] = 2
    elif fault == "boolean_version":
        declaration["schema_version"] = True
    elif fault == "unknown_field":
        f["enabled"] = True
    elif fault == "secret_value":
        p["source_credential"]["api_key"] = "do-not-publish"
    elif fault == "missing_field":
        del f["market"]
    elif fault == "bad_timezone":
        f["schedule"]["timezone"] = "Unknown/Zone"
    elif fault == "bad_clock":
        f["schedule"]["local_time"] = "25:00"
    elif fault == "bad_slot":
        f["delivery"]["slot_id"] = "afternoon"
    elif fault == "boolean_window":
        f["publication_retry_window_seconds"] = True
    elif fault == "current_not_accepted":
        f["contract"]["current_version"] = 2
    elif fault == "duplicate_contract_version":
        f["contract"]["accepted_versions"] = [1, 1]
    elif fault == "draining_acquisition":
        f["lifecycle"] = "draining"
    elif fault == "retired_source":
        f.update(lifecycle="retired", schedule=None, historical_execution=None)
        f["capabilities"].update(acquisition=False, historical="unsupported")
    elif fault == "historical_without_source":
        f.update(schedule=None)
        f["capabilities"].update(source=False, acquisition=False)
    elif fault == "missing_schedule":
        f["schedule"] = None
    elif fault == "missing_deadline":
        f["completion_deadline"] = None
    elif fault == "disabled_schedule":
        f["capabilities"]["acquisition"] = False
    elif fault == "unsupported_historical_execution":
        f["capabilities"]["historical"] = "unsupported"
    elif fault == "bounded_without_deadline":
        f["bounded_monitor"]["deadline"] = None
    with pytest.raises(ValueError):
        ProviderCatalog.model_validate(declaration)


@pytest.mark.parametrize("schema_id,version", [("unknown", 1), ("market_eod", 99)])
def test_contract_reference_must_be_published(declaration, tmp_path, schema_id, version):
    declaration["feeds"][0]["contract"] = {
        "schema_id": schema_id,
        "current_version": version,
        "accepted_versions": [version],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(declaration))
    with pytest.raises(ValueError, match="unknown contract reference"):
        load_catalog(path, contracts_dir=ROOT / "contracts")


def test_contract_bytes_are_verified(tmp_path):
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    (tmp_path / "contracts/market_eod/v1.schema.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        load_catalog(ROOT / CATALOG_PATH, contracts_dir=tmp_path / "contracts")


def test_semantic_order_is_deterministic(declaration):
    first = build_artifacts(ProviderCatalog.model_validate(declaration))
    declaration["providers"].reverse()
    declaration["feeds"].reverse()
    for provider in declaration["providers"]:
        provider["consumer_roles"].reverse()
        provider["credential_env_names"].reverse()
    for feed in declaration["feeds"]:
        feed["contract"]["accepted_versions"].reverse()
    assert build_artifacts(ProviderCatalog.model_validate(declaration)) == first


def test_time_changes_do_not_republish_fetcher_dashboard_or_inventory(declaration):
    previous = build_artifacts(ProviderCatalog.model_validate(declaration))
    declaration["feeds"][0]["schedule"]["local_time"] = "18:00"
    declaration["feeds"][0]["completion_deadline"]["local_time"] = "10:00"
    current = build_artifacts(ProviderCatalog.model_validate(declaration))
    assert {path for path in previous if current[path] != previous[path]} == {
        Path("backend/app/generated/provider_metadata.v1.json"),
    }


def test_historical_defaults_match_existing_execution_inputs(declaration):
    catalog = ProviderCatalog.model_validate(declaration)
    for profile, filename in (
        ("local", "daily_scheduler.v2.json"),
        ("staging", "daily_scheduler.v2.json"),
        ("production", "daily_scheduler.production.v3.json"),
    ):
        manifest = json.loads(
            (ROOT / "fetcher/tests/fixtures/legacy_schedules" / filename).read_text()
        )
        old = {(f["provider"], f["dataset_key"]): f for f in manifest["feeds"]}
        for feed in catalog.feeds:
            if feed.historical_execution is None:
                continue
            execution = getattr(feed.historical_execution, profile).model_dump()
            assert execution == {
                key: old[(feed.provider, feed.dataset_key)][key] for key in execution
            }


def test_retirement_removes_new_options_without_reintroducing_consumers(declaration):
    provider = declaration["providers"][0]
    provider["consumer_roles"] = []
    feed = declaration["feeds"][0]
    feed.update(lifecycle="retired", schedule=None, historical_execution=None)
    feed["capabilities"].update(source=False, acquisition=False, historical="unsupported")
    feed["bounded_monitor"].update(enabled=False)
    artifacts = build_artifacts(ProviderCatalog.model_validate(declaration))
    ui = artifacts[Path("dashboard/src/lib/generated/provider-catalog.ts")]
    assert "twelve_data:" not in ui
    inventory = json.loads(artifacts[Path("infra/deploy/generated/provider_inventory.v1.json")])
    assert not any(c["provider"] == "twelve_data" for c in inventory["consumers"])
    # TAIFEX still uses the shared image, despite the owner's consumers retiring.
    assert "twelve_data" in inventory["images"]


def test_fetcher_projection_contains_no_schedules(declaration):
    artifacts = build_artifacts(ProviderCatalog.model_validate(declaration))
    body = artifacts[Path("fetcher/src/findb_fetcher/data/execution_descriptors.v1.json")]
    for field in (
        "schedule",
        "scheduled_time",
        "source_timezone",
        "local_time",
        "target_date_lag_days",
        "completion_deadline",
        "bounded_monitor",
        "publication_retry_window_seconds",
        "rate_limit_requests",
    ):
        assert f'"{field}"' not in body
    assert '"slot_id"' in body  # Existing delivery contract only; never a clock.
    for feed in json.loads(body)["feeds"]:
        profiles = feed["historical_execution"]
        if profiles:
            for profile in profiles.values():
                assert (ROOT / "fetcher/configs" / profile["universe_file"]).is_file()


def test_inventory_preserves_image_sharing_and_all_roles(declaration):
    artifacts = build_artifacts(ProviderCatalog.model_validate(declaration))
    inventory = json.loads(artifacts[Path("infra/deploy/generated/provider_inventory.v1.json")])
    assert inventory["images"] == ["factset", "finlab", "twelve_data"]
    assert len(inventory["consumers"]) == 8
    assert {c["name"] for c in inventory["consumers"] if c["provider"] == "taifex"} == {
        "findb-full-market-taifex",
        "findb-historical-taifex",
    }
    assert all(
        c["image_key"] == "twelve_data" for c in inventory["consumers"] if c["provider"] == "taifex"
    )


def test_new_feed_and_drain_derive_consumer_views(declaration):
    provider = deepcopy(declaration["providers"][0])
    provider.update(provider="example", container_suffix="example", image_key="example")
    provider["source_credential"].update(name="fetcher-example", hash_env="EXAMPLE_SOURCE_HASH")
    feed = deepcopy(declaration["feeds"][0])
    feed.update(provider="example", dataset_key="example_eod")
    declaration["providers"].append(provider)
    declaration["feeds"].append(feed)
    artifacts = build_artifacts(ProviderCatalog.model_validate(declaration))
    inventory = json.loads(artifacts[Path("infra/deploy/generated/provider_inventory.v1.json")])
    assert len(inventory["consumers"]) == 10
    assert (
        'example: ["example_eod"]'
        in artifacts[Path("dashboard/src/lib/generated/provider-catalog.ts")]
    )
    feed.update(lifecycle="draining", schedule=None, historical_execution=None)
    feed["capabilities"].update(acquisition=False, historical="unsupported")
    # Draining is not an instruction to remove installed consumers or Source scope.
    drained = build_artifacts(ProviderCatalog.model_validate(declaration))
    assert (
        json.loads(drained[Path("infra/deploy/generated/provider_inventory.v1.json")])["consumers"]
        == inventory["consumers"]
    )


def test_all_artifacts_are_checked_and_check_is_read_only(tmp_path):
    shutil.copytree(ROOT / "contracts", tmp_path / "contracts")
    (tmp_path / CATALOG_PATH).parent.mkdir(parents=True)
    shutil.copyfile(ROOT / CATALOG_PATH, tmp_path / CATALOG_PATH)
    assert export(tmp_path, check=False) == []
    assert export(tmp_path, check=True) == []
    paths = build_artifacts(load_catalog(ROOT / CATALOG_PATH, contracts_dir=ROOT / "contracts"))
    for relative in paths:
        path = tmp_path / relative
        original = path.read_text()
        path.write_text(original + " ")
        assert export(tmp_path, check=True) == [relative]
        assert path.read_text() == original + " "
        path.unlink()
        assert export(tmp_path, check=True) == [relative]
        assert not path.exists()
        path.write_text(original)


def test_fetcher_adapter_scopes_match_catalog_without_clocks(declaration):
    from findb_fetcher.execution_metadata import acquisition_feeds
    from findb_fetcher.full_market_cli import _SCOPES

    catalog = ProviderCatalog.model_validate(declaration)
    assert _SCOPES == {
        p.provider: {f.dataset_key for f in catalog.feeds if f.provider == p.provider}
        for p in catalog.providers
    }
    assert len(acquisition_feeds()) == len(catalog.feeds)
    assert all(
        "scheduled_time" not in f and "source_timezone" not in f for f in acquisition_feeds()
    )


def test_backend_publication_is_consumable_by_independent_fetcher():
    from findb_fetcher.execution_config import resolve_execution_config

    from app.config import get_settings
    from app.services.feed_schedule import execution_config
    from app.services.provider_metadata import source_scopes

    for provider, scopes in source_scopes().items():
        published = execution_config(provider, scopes, "full_market").model_dump(mode="json")
        result = resolve_execution_config(
            published,
            provider=provider,
            environment=get_settings().APP_ENVIRONMENT,
            profile="full_market",
        )
        assert {f["dataset_key"] for f in result.feeds} == set(scopes)
        assert all("slot_id" not in f for f in published["feeds"])
