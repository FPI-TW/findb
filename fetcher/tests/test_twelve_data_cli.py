"""Retired entry points cannot acquire, load secrets, or rewrite state."""

import importlib
import json

import pytest

from findb_fetcher import provider_work
from findb_fetcher.config import ConfigError, require_provider_work_protocol
from findb_fetcher.provider_process import provider_environment


@pytest.mark.parametrize(
    "module",
    [
        "scheduler_cli",
        "finlab_scheduler_cli",
        "shioaji_scheduler_cli",
        "taifex_pilot",
        "twelve_data_cli",
        "finlab_smoke_cli",
        "shioaji_smoke_cli",
        "shioaji_staging_cli",
    ],
)
@pytest.mark.parametrize("arguments", [[], ["--deliver"], ["--check"], ["--initialize-state"]])
def test_retired_entrypoints_never_touch_existing_state(
    module, arguments, tmp_path, monkeypatch, capsys
):
    state = tmp_path / "checkpoint.sqlite3"
    original = b"retained checkpoint/prepared/receipt/budget evidence"
    state.write_bytes(original)
    monkeypatch.setenv("FETCHER_STATE_PATH", str(state))
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "false")
    assert importlib.import_module(f"findb_fetcher.{module}").main(arguments) == 2
    output = capsys.readouterr()
    body = json.loads(output.err)
    assert body["status"] == "retired"
    assert body["code"] == "PROVIDER_WORK_REQUIRED"
    assert output.out == ""
    assert state.read_bytes() == original
    assert list(tmp_path.iterdir()) == [state]


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex", "factset"])
def test_owner_is_required_for_every_provider(provider, monkeypatch):
    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        provider_work.require_owner(provider)
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        provider_work.require_acquisition(provider)


@pytest.mark.parametrize("value", ["false", "0", "", "TRUE", "invalid"])
def test_removed_protocol_setting_rejected_before_claim(value, monkeypatch, tmp_path):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", value)

    class NoIO:
        def request(self, *args, **kwargs):
            pytest.fail("invalid config performed Source I/O")

    with pytest.raises(ConfigError), provider_work.claimed_work(NoIO(), "finlab", root=tmp_path):
        pytest.fail("invalid config acquired work")
    assert not list(tmp_path.iterdir())


def test_absent_setting_propagates_required_protocol_to_children(monkeypatch):
    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    require_provider_work_protocol()
    assert provider_environment()["PROVIDER_WORK_REQUIRED"] == "true"


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex", "factset"])
@pytest.mark.parametrize("drain", [False, True])
def test_daily_cycle_and_drain_only_dispatch_queue(provider, drain, monkeypatch):
    from findb_fetcher.full_market_runtime import FullMarketRuntime

    runtime = FullMarketRuntime.__new__(FullMarketRuntime)
    runtime.provider = provider
    calls = []
    runtime._queued_cycle = lambda **kwargs: calls.append(kwargs) or {"status": "queued"}
    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    result = runtime.drain() if drain else runtime.cycle()
    assert result == (None if drain else {"status": "queued"})
    assert calls == ([{"include_health": False}] if drain else [{}])


def test_source_delivery_without_owner_rejects_before_transport(monkeypatch):
    from findb_fetcher.client import PreparedDelivery, _provider_work_headers

    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    with pytest.raises(provider_work.ProviderWorkRecoveryBlockedError):
        _provider_work_headers(
            PreparedDelivery(b"original-bytes", "original", "scope", "market_eod", 1)
        )


@pytest.mark.parametrize("setting", [None, "false", "true"])
def test_child_cannot_select_legacy_descriptor_bypass(setting, tmp_path, monkeypatch):
    import os

    from findb_fetcher.full_market_state import QuotaBlockedError
    from findb_fetcher.providers import shioaji_control

    if setting is None:
        monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("PROVIDER_WORK_REQUIRED", setting)
    fd = os.open(tmp_path / "account-lock", os.O_CREAT | os.O_RDWR, 0o600)
    monkeypatch.setattr(shioaji_control, "recv_handle", lambda conn: fd)
    with pytest.raises(QuotaBlockedError, match="whole-work descriptor"):
        shioaji_control.ChildBoundary(object(), {"work_required": False})
    with pytest.raises(OSError):
        os.fstat(fd)
