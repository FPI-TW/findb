"""Historical settings never read daily clocks, gates, or schedule snapshots."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from findb_fetcher import execution_metadata as metadata
from findb_fetcher.config import ConfigError, FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.schedule import load_schedule_manifest

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
CONTRACTS = CONFIGS.parents[1] / "contracts"


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
@pytest.mark.parametrize("provider", ["twelve_data", "finlab"])
def test_non_clock_settings_match_previous_historical_manifest(monkeypatch, environment, provider):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    legacy = CONFIGS.parent / "tests/fixtures/legacy_schedules"
    monkeypatch.setattr(metadata, "CONFIGS_DIR", legacy)
    path = legacy / (
        "daily_scheduler.production.v3.json"
        if environment == "production"
        else "daily_scheduler.v2.json"
    )
    old = next(f for f in load_schedule_manifest(path).feeds if f.provider == provider)
    new = metadata.historical_execution(provider, old.dataset_key)
    assert asdict(new) == asdict(old.execution_descriptor())
    assert not any(
        hasattr(new, k)
        for k in (
            "scheduled_time",
            "scheduled_local_time",
            "timezone_name",
            "target_date_lag_days",
            "enabled",
            "target_date",
            "grace_deadline",
        )
    )


@pytest.mark.parametrize(
    "provider,dataset",
    [
        ("finlab", "tw_etf_eod"),
        ("twelve_data", "hk_equity_eod"),
        ("shioaji", "tw_equity_minute"),
        ("unknown", "us_equity_eod"),
    ],
)
def test_unsupported_scope_remains_closed(provider, dataset):
    with pytest.raises(metadata.ExecutionMetadataError):
        metadata.historical_feed(provider, dataset)


def _write(monkeypatch, tmp_path, value):
    body = dict(value)
    body.pop("projection_sha256", None)
    value["projection_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    target = tmp_path / "metadata.json"
    target.write_text(json.dumps(value))
    monkeypatch.setattr(metadata, "METADATA_PATH", target)
    return target


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "duplicate_feed",
        "duplicate_provider",
        "retired",
        "digest",
        "missing",
        "duplicate_key",
    ],
)
def test_installed_metadata_fails_closed(monkeypatch, tmp_path, fault):
    value = json.loads(metadata.METADATA_PATH.read_text())
    if fault == "version":
        value["schema_version"] = 2
    if fault == "duplicate_feed":
        value["feeds"].append(value["feeds"][0])
    if fault == "duplicate_provider":
        value["providers"].append(value["providers"][0])
    if fault == "retired":
        value["feeds"][0]["lifecycle"] = "retired"
    target = _write(monkeypatch, tmp_path, value)
    if fault == "digest":
        target.write_text(
            target.read_text().replace('"projection_sha256": "', '"projection_sha256": "bad')
        )
    if fault == "missing":
        target.unlink()
    if fault == "duplicate_key":
        target.write_text(
            target.read_text().replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
            )
        )
    with pytest.raises(metadata.ExecutionMetadataError):
        metadata.historical_feed("factset", "hk_equity_eod")


def test_environment_mismatch_is_rejected_without_schedule(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("DEPLOYMENT_TARGET", "staging")
    with pytest.raises(ConfigError):
        metadata.historical_feed("factset", "hk_equity_eod")


def test_finlab_universe_override_is_shared_with_estimation(monkeypatch, tmp_path):
    monkeypatch.setenv("FETCHER_FINLAB_UNIVERSE_FILE", str(tmp_path / "reviewed.json"))
    result = metadata.historical_execution("finlab", "tw_equity_eod")
    assert result.universe_file == tmp_path / "reviewed.json"


@pytest.mark.parametrize("provider", ["factset", "taifex"])
def test_whole_market_historical_bootstrap_does_not_load_daily_config(
    monkeypatch, tmp_path, provider
):
    from findb_fetcher import factset_historical, full_market_cli, taifex_historical

    module = factset_historical if provider == "factset" else taifex_historical
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("FETCHER_BOOTSTRAP_CONFIG", "/missing/full-market.json")
    monkeypatch.setenv("FETCHER_HISTORICAL_SCHEDULE_FILE", "/missing/daily.json")
    monkeypatch.setenv("FETCHER_HISTORICAL_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(full_market_cli, "load_config", lambda *a: pytest.fail("daily config read"))
    config = FetcherConfig("https://source.example", "test-key", contracts_dir=CONTRACTS)
    monkeypatch.setattr(module.FetcherConfig, "from_env", lambda: config)
    monkeypatch.setattr(module.RawStorageConfig, "from_env", lambda: object())
    monkeypatch.setattr(module, "R2RawPayloadStore", lambda *a, **kw: object())
    if provider == "factset":
        monkeypatch.setattr(module.FactSetConfig, "from_env", lambda: object())
        monkeypatch.setattr(module, "FactSetGateway", lambda *a: object())
    runner = (
        module.FactSetHistoricalRunner if provider == "factset" else module.TaifexHistoricalRunner
    ).from_env()
    assert runner.feed == {
        "slot_id": "asia_pacific_markets_window"
        if provider == "factset"
        else "taiwan_market_window"
    }
    if provider == "factset":
        runner.close()
    else:
        assert isinstance(runner.registry, ContractRegistry)
        runner.http.close()
        runner.source.__exit__(None, None, None)


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "factset", "taifex"])
def test_deployment_historical_check_works_without_schedule_files(monkeypatch, provider):
    from findb_fetcher import full_market_cli, historical_backfill_cli, schedule

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("SOURCE_API_URL", "https://source.example")
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "test-key")
    monkeypatch.setenv("FETCHER_HISTORICAL_SCHEDULE_FILE", "/missing/daily.json")
    monkeypatch.setenv("FETCHER_BOOTSTRAP_CONFIG", "/missing/full.json")
    monkeypatch.setattr(schedule, "load_schedule_manifest", lambda *a: pytest.fail("schedule read"))
    monkeypatch.setattr(full_market_cli, "load_config", lambda *a: pytest.fail("full config read"))
    assert historical_backfill_cli.main(["--provider", provider, "--check"]) == 0


def test_deployment_check_rejects_missing_packaged_metadata(monkeypatch, tmp_path):
    from findb_fetcher import historical_backfill_cli

    monkeypatch.setenv("SOURCE_API_URL", "https://source.example")
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "test-key")
    monkeypatch.setattr(metadata, "METADATA_PATH", tmp_path / "missing.json")
    with pytest.raises(metadata.ExecutionMetadataError):
        historical_backfill_cli.main(["--provider", "taifex", "--check"])


def _changed_descriptor(monkeypatch, tmp_path, change):
    value = json.loads(metadata.METADATA_PATH.read_text())
    change(value)
    return _write(monkeypatch, tmp_path, value)


def _provider_row(value, provider):
    return next(row for row in value["providers"] if row["provider"] == provider)


def test_platform_limits_are_read_from_installed_descriptor(monkeypatch, tmp_path):
    from findb_fetcher.platform_limits import limits

    def change(value):
        _provider_row(value, "twelve_data")["platform_limits"]["requests_per_minute"] = 60
        _provider_row(value, "finlab")["platform_limits"]["max_response_bytes"] = 4096

    _changed_descriptor(monkeypatch, tmp_path, change)
    assert limits("twelve_data")["requests_per_minute"] == 60
    assert limits("twelve_data")["requests_per_second"] == 1
    assert limits("finlab")["max_response_bytes"] == 4096
    assert limits("finlab")["requests_per_second"] is None


@pytest.mark.parametrize(
    "fault",
    ["zero_rate", "boolean_rate", "float_bytes", "extra_field", "duplicate_name", "missing"],
)
def test_invalid_provider_descriptor_fails_closed(monkeypatch, tmp_path, fault):
    from findb_fetcher.platform_limits import limits

    def change(value):
        row = _provider_row(value, "twelve_data")
        if fault == "zero_rate":
            row["platform_limits"]["requests_per_minute"] = 0
        if fault == "boolean_rate":
            row["platform_limits"]["requests_per_minute"] = True
        if fault == "float_bytes":
            row["platform_limits"]["max_response_bytes"] = 1.5
        if fault == "extra_field":
            row["platform_limits"]["requests_per_day"] = 1
        if fault == "duplicate_name":
            row["credential_env_names"] *= 2
        if fault == "missing":
            del row["credential_env_names"]

    _changed_descriptor(monkeypatch, tmp_path, change)
    with pytest.raises(metadata.ExecutionMetadataError):
        limits("twelve_data")


@pytest.mark.parametrize("provider", ["shioaji", "unknown"])
def test_unpublished_provider_has_no_limits_or_identity(tmp_path, provider):
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.platform_limits import account_identity, limits

    with pytest.raises(ValueError):
        limits(provider)
    with pytest.raises(ValueError):
        account_identity(provider, FullMarketState(tmp_path / "account.sqlite3"))


def test_factset_identity_settings_are_published_without_password():
    from findb_fetcher.platform_limits import FACTSET_IDENTITY_ENV_NAMES

    names = metadata.provider_descriptor("factset")["credential_env_names"]
    assert set(FACTSET_IDENTITY_ENV_NAMES) < set(names)
    assert not any("PASSWORD" in name for name in FACTSET_IDENTITY_ENV_NAMES)


def test_single_credential_identity_uses_published_setting(monkeypatch, tmp_path):
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.platform_limits import account_identity

    monkeypatch.setenv("TWELVE_DATA_API_KEY", "old-name")
    monkeypatch.setenv("RENAMED_TWELVE_DATA_KEY", "new-name")
    monkeypatch.setenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(tmp_path / "none.json"))
    expected = account_identity(
        "twelve_data", FullMarketState(tmp_path / "a.sqlite3"), credential="new-name"
    )

    def rename(value):
        _provider_row(value, "twelve_data")["credential_env_names"] = ["RENAMED_TWELVE_DATA_KEY"]

    _changed_descriptor(monkeypatch, tmp_path, rename)
    assert account_identity("twelve_data", FullMarketState(tmp_path / "b.sqlite3")) == expected

    def ambiguous(value):
        _provider_row(value, "twelve_data")["credential_env_names"] = ["A_KEY", "B_KEY"]

    _changed_descriptor(monkeypatch, tmp_path, ambiguous)
    with pytest.raises(ValueError, match="undefined"):
        account_identity("twelve_data", FullMarketState(tmp_path / "c.sqlite3"))


def test_cli_provider_choices_follow_installed_descriptor(monkeypatch, tmp_path):
    from findb_fetcher import historical_backfill_cli, production_backfill_cli

    def change(value):
        value["providers"] = [row for row in value["providers"] if row["provider"] != "taifex"]
        value["feeds"] = [row for row in value["feeds"] if row["provider"] != "taifex"]
        for row in value["feeds"]:
            if row["provider"] == "finlab":
                row["capabilities"]["historical"] = "unsupported"

    _changed_descriptor(monkeypatch, tmp_path, change)
    assert "taifex" not in metadata.provider_ids()
    # FinLab keeps daily adapter metadata but offers no historical choice.
    assert "finlab" in metadata.provider_ids()
    assert metadata.historical_provider_ids() == ("factset", "twelve_data")
    for provider in ("taifex", "finlab"):
        with pytest.raises(SystemExit) as raised:
            historical_backfill_cli.main(["--provider", provider, "--check"])
        assert raised.value.code == 2
        with pytest.raises(ValueError):
            production_backfill_cli.build_parser().parse_args(
                ["--provider", provider, "--dataset-key", "x"]
                + ["--start-date", "2026-01-01", "--end-date", "2026-01-01"]
            )


def test_account_governor_cli_rejects_unpublished_provider(monkeypatch, tmp_path):
    from findb_fetcher import account_governor

    monkeypatch.setattr(
        "sys.argv",
        [
            "findb-account-governor",
            "inspect-acquisition",
            "--governor",
            "/x",
            "--provider",
            "shioaji",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        account_governor.main()
    assert raised.value.code == 2
