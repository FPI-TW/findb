"""Catalog publication boundaries, invalid declarations and consumer projections."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.provider_catalog import PUBLICATION_RETRY_WINDOW_SECONDS, ProviderCatalog, load_catalog
from scripts.export_provider_catalog import CATALOG_PATH, build_artifacts, export

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def declaration():
    return json.loads((ROOT / CATALOG_PATH).read_text())


def test_feed_markets_match_backend_timezone_authority(declaration):
    from app.services.effective_calendar import MARKETS, market_timezone
    from app.services.historical_backfill import FEED_TIMEZONES, RETIRED_PROVIDER_TIMEZONES

    feed_zones: dict[tuple[str, str], str] = {}
    for feed in ProviderCatalog.model_validate(declaration).feeds:
        zone = market_timezone(feed.calendar_market).key
        assert MARKETS[feed.market][0] == zone, feed.dataset_key
        # A schedule may run in another timezone; dates, closes and cutoffs stay the market's.
        feed_zones[(feed.provider, feed.dataset_key)] = zone
    # Historical cutoffs are feed-keyed, so one provider's feeds may use different markets.
    assert feed_zones == FEED_TIMEZONES
    assert RETIRED_PROVIDER_TIMEZONES == {"shioaji": "Asia/Taipei"}


def test_unknown_market_timezone_fails_closed():
    from app.services.effective_calendar import market_timezone

    with pytest.raises(ValueError, match="unsupported calendar market"):
        market_timezone("TW_FUTURES")


def test_supported_scopes_contracts_and_deadlines(declaration):
    catalog = load_catalog(ROOT / CATALOG_PATH, contracts_dir=ROOT / "contracts")
    assert {(f.provider, f.dataset_key) for f in catalog.feeds} == {
        ("twelve_data", "us_equity_eod"),
        ("factset", "hk_equity_eod"),
        ("hkex", "hk_equity_official_eod"),
        ("finlab", "tw_equity_eod"),
        ("finlab", "tw_etf_eod"),
        ("finlab", "tw_equity_bar_1m"),
        ("finlab", "tw_etf_bar_1m"),
        ("finlab", "tw_equity_tick"),
        ("finlab", "tw_etf_tick"),
        ("taifex", "tw_futures_eod"),
        ("fmp", "hk_index_eod"),
        ("fmp", "us_index_eod"),
    }
    # Intraday feeds deliver minute bars or ticks by reference; nothing else is non-daily.
    assert {
        f.dataset_key: (f.frequency, f.contract.schema_id)
        for f in catalog.feeds
        if f.frequency != "daily"
    } == {
        "tw_equity_bar_1m": ("minute", "intraday_partition"),
        "tw_etf_bar_1m": ("minute", "intraday_partition"),
        "tw_equity_tick": ("tick", "intraday_partition"),
        "tw_etf_tick": ("tick", "intraday_partition"),
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
    # One rule for every acquiring feed: due 12 wall-clock hours after its scheduled start.
    assert {
        f.dataset_key: (
            f.schedule.timezone,
            f.schedule.local_time,
            f.completion_deadline.timezone,
            f.completion_deadline.local_time,
            f.completion_deadline.trade_date_day_offset,
        )
        for f in catalog.feeds
    } == {
        "us_equity_eod": ("America/New_York", "17:00", "America/New_York", "05:00", 1),
        "hk_equity_eod": ("Asia/Hong_Kong", "20:00", "Asia/Hong_Kong", "08:00", 1),
        # HKEX's report is downloadable around 21:00.
        "hk_equity_official_eod": ("Asia/Hong_Kong", "21:05", "Asia/Hong_Kong", "09:05", 1),
        "tw_equity_eod": ("Asia/Taipei", "18:00", "Asia/Taipei", "06:00", 1),
        "tw_etf_eod": ("Asia/Taipei", "18:00", "Asia/Taipei", "06:00", 1),
        "tw_equity_bar_1m": ("Asia/Taipei", "19:15", "Asia/Taipei", "07:15", 1),
        "tw_etf_bar_1m": ("Asia/Taipei", "19:15", "Asia/Taipei", "07:15", 1),
        "tw_equity_tick": ("Asia/Taipei", "19:15", "Asia/Taipei", "07:15", 1),
        "tw_etf_tick": ("Asia/Taipei", "19:15", "Asia/Taipei", "07:15", 1),
        "tw_futures_eod": ("Asia/Taipei", "17:00", "Asia/Taipei", "05:00", 1),
        # Index groups by publication time; New York's is fetched the next Taipei morning.
        "hk_index_eod": ("Asia/Hong_Kong", "20:00", "Asia/Hong_Kong", "08:00", 1),
        "us_index_eod": ("Asia/Taipei", "08:30", "Asia/Taipei", "20:30", 1),
    }
    assert feeds["us_index_eod"].schedule.target_date_lag_days == 1
    assert {f.dataset_key for f in catalog.feeds if f.asset_class == "index"} == {
        "hk_index_eod",
        "us_index_eod",
    }
    assert {f.dataset_key for f in catalog.feeds if f.calendar_required} == {
        "hk_index_eod",
        "us_index_eod",
    }
    assert us.bounded_monitor.deadline.local_time == "09:15"
    # One time point ends a date: an unpublished one is retried until 30 minutes before
    # its deadline (the last no-data round), never by a separate per-feed window.
    # FinLab daily thus still waits past the Fetcher's 22:15 final-table fallback.
    assert {
        f.dataset_key: f.publication_retry_window_seconds
        for f in catalog.feeds
        if f.capabilities.acquisition
    } == dict.fromkeys((f.dataset_key for f in catalog.feeds if f.capabilities.acquisition), 41400)
    assert PUBLICATION_RETRY_WINDOW_SECONDS == 12 * 3600 - 1800
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
        "configured_window",
        "current_not_accepted",
        "duplicate_contract_version",
        "draining_acquisition",
        "retired_source",
        "historical_without_source",
        "missing_schedule",
        "missing_deadline",
        "deadline_off_rule",
        "deadline_other_timezone",
        "deadline_same_day",
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
    elif fault == "configured_window":
        # The retry window follows the deadline; a feed cannot set its own.
        f["publication_retry_window_seconds"] = 14400
    elif fault == "current_not_accepted":
        f["contract"]["current_version"] = 2
    elif fault == "duplicate_contract_version":
        f["contract"]["accepted_versions"] = [1, 1]
    elif fault == "draining_acquisition":
        f["lifecycle"] = "draining"
    elif fault == "retired_source":
        f.update(
            lifecycle="retired",
            schedule=None,
            historical_execution=None,
            historical_cohort=None,
            historical_requests_per_date=None,
        )
        f["capabilities"].update(acquisition=False, historical="unsupported")
    elif fault == "historical_without_source":
        f.update(schedule=None)
        f["capabilities"].update(source=False, acquisition=False)
    elif fault == "missing_schedule":
        f["schedule"] = None
    elif fault == "missing_deadline":
        f["completion_deadline"] = None
    elif fault == "deadline_off_rule":
        f["completion_deadline"]["local_time"] = "09:00"
    elif fault == "deadline_other_timezone":
        f["completion_deadline"]["timezone"] = "Asia/Taipei"
    elif fault == "deadline_same_day":
        f["completion_deadline"]["trade_date_day_offset"] = 0
    elif fault == "disabled_schedule":
        f["capabilities"]["acquisition"] = False
    elif fault == "unsupported_historical_execution":
        f["capabilities"]["historical"] = "unsupported"
        f["historical_cohort"] = None
        f["historical_requests_per_date"] = None
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
    declaration["feeds"][0]["completion_deadline"]["local_time"] = "06:00"
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
    feed.update(
        lifecycle="retired",
        schedule=None,
        historical_execution=None,
        historical_cohort=None,
        historical_requests_per_date=None,
    )
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
    assert len(inventory["consumers"]) == 12
    # The public exchange providers and FMP run on the Twelve Data image.
    for provider in ("taifex", "hkex", "fmp"):
        assert {c["name"] for c in inventory["consumers"] if c["provider"] == provider} == {
            f"findb-full-market-{provider}",
            f"findb-historical-{provider}",
        }
        assert all(
            c["image_key"] == "twelve_data"
            for c in inventory["consumers"]
            if c["provider"] == provider
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
    assert len(inventory["consumers"]) == 14
    assert (
        'example: ["example_eod"]'
        in artifacts[Path("dashboard/src/lib/generated/provider-catalog.ts")]
    )
    feed.update(
        lifecycle="draining",
        schedule=None,
        historical_execution=None,
        historical_cohort=None,
        historical_requests_per_date=None,
    )
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


def test_historical_cohorts_are_catalog_fields():
    from app.services.provider_metadata import provider_catalog

    cohorts = {
        (feed.provider, feed.dataset_key): feed.historical_cohort
        for feed in provider_catalog().feeds
        if feed.capabilities.historical != "unsupported"
    }
    assert cohorts == {
        ("factset", "hk_equity_eod"): "current_release",
        ("hkex", "hk_equity_official_eod"): "current_release",
        # The index lists are fixed: one list serves every date of a request.
        ("fmp", "hk_index_eod"): "current_release",
        ("fmp", "us_index_eod"): "current_release",
        ("finlab", "tw_equity_eod"): "dated_release",
        ("finlab", "tw_etf_eod"): "dated_release",
        ("taifex", "tw_futures_eod"): "daily_report",
        ("twelve_data", "us_equity_eod"): "dated_release",
    }


def test_historical_cohort_rules(declaration):
    feed = next(f for f in declaration["feeds"] if f["dataset_key"] == "tw_equity_bar_1m")
    feed["historical_cohort"] = "dated_release"
    with pytest.raises(ValidationError, match="declare a historical cohort"):
        ProviderCatalog.model_validate(declaration)
    feed["historical_cohort"] = None
    taifex = next(f for f in declaration["feeds"] if f["provider"] == "taifex")
    taifex["historical_cohort"] = None
    with pytest.raises(ValidationError, match="declare a historical cohort"):
        ProviderCatalog.model_validate(declaration)
    # A Backend-owned cohort would make the planner ignore a packaged universe.
    taifex["historical_cohort"] = "daily_report"
    taifex["historical_execution"] = next(
        f for f in declaration["feeds"] if f["provider"] == "twelve_data"
    )["historical_execution"]
    with pytest.raises(ValidationError, match="only dated_release feeds"):
        ProviderCatalog.model_validate(declaration)


def test_feeds_of_one_dataset_share_a_calendar_market(declaration):
    feed = next(f for f in declaration["feeds"] if f["provider"] == "taifex")
    duplicate = {**feed, "provider": "twelve_data", "calendar_market": "TW"}
    duplicate["capabilities"] = {**feed["capabilities"], "historical": "unsupported"}
    duplicate["historical_cohort"] = None
    duplicate["historical_requests_per_date"] = None
    declaration["feeds"].append(duplicate)
    with pytest.raises(ValidationError, match="share a calendar market"):
        ProviderCatalog.model_validate(declaration)


def _seed_with_catalog(monkeypatch, declaration):
    from scripts import seed_data

    catalog = ProviderCatalog.model_validate(declaration)
    monkeypatch.setattr(seed_data, "provider_catalog", lambda: catalog)
    return seed_data


def test_seed_identity_follows_one_catalog_edit(monkeypatch, declaration):
    feed = next(f for f in declaration["feeds"] if f["dataset_key"] == "hk_equity_eod")
    feed["contract"].update(current_version=1, accepted_versions=[1])
    feed["asset_class"] = "etf"
    seed = _seed_with_catalog(monkeypatch, declaration)
    dataset = seed._staged_eod_dataset("hk_equity_eod", "港股日K", currency=None)
    assert dataset["asset_class"] == "etf"
    assert dataset["config"]["defaults"]["asset_class"] == "etf"
    assert dataset["config"]["accepted_schema_versions"] == [1]
    assert dataset["config"]["current_schema_version"] == 1
    assert dataset["config"]["allowed_sources"] == ["factset"]
    assert dataset["config"]["source_precedence"] == ["factset"]
    assert dataset["config"]["full_market"]["calendar_market"] == "HK"
    assert dataset["description"] == "Staged factset market_eod.v1 full-market daily feed"


def test_staged_descriptions_name_the_current_contract_version():
    from scripts.seed_data import DATASETS, provider_catalog

    contracts = {feed.dataset_key: feed.contract for feed in provider_catalog().feeds}
    staged = [item for item in DATASETS if item["description"].startswith("Staged ")]
    assert {item["dataset_key"] for item in staged} >= {"hk_equity_eod", "us_index_eod"}
    for item in staged:
        contract = contracts[item["dataset_key"]]
        label = f" {contract.schema_id}.v{contract.current_version} "
        assert label in item["description"], item["dataset_key"]


@pytest.mark.parametrize("fault", ["second_provider", "source_disabled"])
def test_seed_requires_exactly_one_catalog_source_feed(monkeypatch, declaration, fault):
    feeds = declaration["feeds"]
    index = next(i for i, f in enumerate(feeds) if f["dataset_key"] == "tw_futures_eod")
    if fault == "second_provider":
        extra = deepcopy(feeds[index])
        extra["provider"] = "finlab"
        feeds.append(extra)
    if fault == "source_disabled":
        feeds[index].update(
            lifecycle="retired",
            schedule=None,
            historical_execution=None,
            historical_cohort=None,
            historical_requests_per_date=None,
        )
        feeds[index]["capabilities"].update(
            source=False, acquisition=False, historical="unsupported"
        )
        feeds[index]["bounded_monitor"].update(enabled=False, deadline=None)
    seed = _seed_with_catalog(monkeypatch, declaration)
    with pytest.raises(ValueError, match="exactly one catalog Source feed"):
        seed._staged_eod_dataset("tw_futures_eod", "台灣期貨契約日K", currency="TWD")


def test_universe_modes_are_catalog_fields(monkeypatch, declaration):
    from app.services import universe_refresh
    from app.services.provider_metadata import provider_catalog

    dated = {
        (feed.provider, feed.dataset_key)
        for feed in provider_catalog().feeds
        if feed.universe_mode == "dated"
    }
    assert dated == {("taifex", "tw_futures_eod")}
    assert ("taifex", "tw_futures_eod") not in universe_refresh.refreshable_feeds()
    assert ("twelve_data", "us_equity_eod") in universe_refresh.refreshable_feeds()
    # Declaring a feed dated removes its Owner refresh without a provider list.
    feed = next(f for f in declaration["feeds"] if f["dataset_key"] == "us_equity_eod")
    feed["universe_mode"] = "dated"
    catalog = ProviderCatalog.model_validate(declaration)
    monkeypatch.setattr("app.services.provider_metadata.provider_catalog", lambda: catalog)
    assert ("twelve_data", "us_equity_eod") not in universe_refresh.refreshable_feeds()


def test_historical_requests_per_date_rules(declaration):
    from app.services.provider_metadata import provider_catalog

    assert {
        (feed.provider, feed.dataset_key): feed.historical_requests_per_date
        for feed in provider_catalog().feeds
        if feed.capabilities.historical != "unsupported"
    } == {
        ("factset", "hk_equity_eod"): 1,
        # The Main Board and GEM reports.
        ("hkex", "hk_equity_official_eod"): 2,
        # One request per index.
        ("fmp", "hk_index_eod"): 3,
        ("fmp", "us_index_eod"): 73,
        ("finlab", "tw_equity_eod"): 5,
        ("finlab", "tw_etf_eod"): 5,
        ("taifex", "tw_futures_eod"): 5,
        ("twelve_data", "us_equity_eod"): "per_member",
    }
    taifex = next(f for f in declaration["feeds"] if f["provider"] == "taifex")
    taifex["historical_requests_per_date"] = None
    with pytest.raises(ValidationError, match="declare requests per date"):
        ProviderCatalog.model_validate(declaration)
    # A per-member estimate needs a frozen member list to count.
    taifex["historical_requests_per_date"] = "per_member"
    with pytest.raises(ValidationError, match="dated_release member list"):
        ProviderCatalog.model_validate(declaration)


def test_only_refreshed_market_eod_feeds_deliver_unlisted_symbols(declaration):
    from app.services.provider_metadata import delivers_unlisted, provider_catalog

    assert {
        (feed.provider, feed.dataset_key)
        for feed in provider_catalog().feeds
        if feed.unlisted_symbols == "deliver"
    } == {("finlab", "tw_equity_eod"), ("finlab", "tw_etf_eod")}
    assert delivers_unlisted("finlab", "tw_etf_eod")
    assert not delivers_unlisted("twelve_data", "us_equity_eod")
    assert not delivers_unlisted("finlab", "unknown_dataset")
    # A dated universe is the date's own report; a partition feed has no row symbols.
    for dataset_key in ("tw_futures_eod", "tw_equity_bar_1m"):
        changed = deepcopy(declaration)
        feed = next(f for f in changed["feeds"] if f["dataset_key"] == dataset_key)
        feed["unlisted_symbols"] = "deliver"
        with pytest.raises(ValidationError, match="only refreshed market_eod"):
            ProviderCatalog.model_validate(changed)
