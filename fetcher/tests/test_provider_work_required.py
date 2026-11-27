"""Every provider needs a work owner; the protocol setting cannot be turned off."""

import importlib.metadata
import json

import pytest

from findb_fetcher import provider_work
from findb_fetcher.config import ConfigError, require_provider_work_protocol
from findb_fetcher.execution_metadata import provider_ids
from findb_fetcher.provider_process import provider_environment


def test_only_the_current_commands_are_installed():
    # The retired standalone commands were removed outright; an old name is simply not found.
    commands = {
        entry.name
        for entry in importlib.metadata.entry_points(group="console_scripts")
        if entry.name.startswith("findb-fetch-")
    }
    assert commands == {
        "findb-fetch-runtime",
        "findb-fetch-full-market",
        "findb-fetch-historical-backfill",
        "findb-fetch-backfill-plan",
        "findb-fetch-retired-purge",
    }


@pytest.mark.parametrize("provider", provider_ids())
@pytest.mark.parametrize("setting", [None, "True", "1", "yes"])
def test_owner_is_required_for_every_provider(provider, setting, monkeypatch):
    if setting is None:
        monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("PROVIDER_WORK_REQUIRED", setting)
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        provider_work.require_owner(provider)
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        provider_work.require_acquisition(provider)


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", "", "invalid", " true "])
def test_removed_protocol_setting_rejected_before_claim(value, monkeypatch, tmp_path):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", value)

    class NoIO:
        def request(self, *args, **kwargs):
            pytest.fail("invalid config performed Source I/O")

    with pytest.raises(ConfigError), provider_work.claimed_work(NoIO(), "finlab", root=tmp_path):
        pytest.fail("invalid config acquired work")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "YES", "on", "t", "y"])
def test_required_protocol_accepts_standard_true_values(value, monkeypatch, tmp_path, capsys):
    from pathlib import Path

    from findb_fetcher.full_market_cli import main

    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", value)
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.delenv("DEPLOYMENT_TARGET", raising=False)
    require_provider_work_protocol()
    assert provider_environment()["PROVIDER_WORK_REQUIRED"] == "true"
    assert (
        main(
            [
                "--provider",
                "twelve_data",
                "--config",
                str(Path(__file__).resolve().parents[1] / "configs/bootstrap.local.v1.json"),
                "--state-path",
                str(tmp_path / "unused.sqlite3"),
                "--check",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "invalid"])
def test_protocol_error_distinguishes_false_from_invalid(value, monkeypatch):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", value)
    message = "cannot be false" if value else "valid boolean"
    if value == "invalid":
        message = "valid boolean"
    with pytest.raises(ConfigError, match=message):
        require_provider_work_protocol()


def test_absent_setting_propagates_required_protocol_to_children(monkeypatch):
    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    require_provider_work_protocol()
    assert provider_environment()["PROVIDER_WORK_REQUIRED"] == "true"


@pytest.mark.parametrize("provider", provider_ids())
@pytest.mark.parametrize("drain", [False, True])
def test_daily_cycle_and_drain_only_dispatch_queue(provider, drain, monkeypatch):
    from findb_fetcher.full_market_runtime import FullMarketRuntime

    runtime = FullMarketRuntime.__new__(FullMarketRuntime)
    from types import SimpleNamespace

    from execution_config_support import publication

    runtime.provider = provider
    runtime.execution_profile = "full_market"
    runtime.lane = "default"
    runtime.source = SimpleNamespace(execution_config=lambda: publication(provider))
    calls = []
    runtime._queued_cycle = lambda **kwargs: calls.append(kwargs) or {"status": "queued"}
    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    result = runtime.drain() if drain else runtime.cycle()
    assert result == (None if drain else {"status": "queued"})
    assert calls == (
        [{"include_health": False, "plan_new_dates": False}]
        if drain
        else [{"snapshot": runtime.schedule_snapshot}]
    )


def test_source_delivery_without_owner_rejects_before_transport(monkeypatch):
    from findb_fetcher.client import PreparedDelivery, _provider_work_headers

    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        _provider_work_headers(
            PreparedDelivery(b"original-bytes", "original", "scope", "market_eod", 1)
        )
