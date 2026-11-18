"""FinLab account usage read once per intraday round, without a table download (#601)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from findb_fetcher import acquisition, provider_process
from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.provider_adapters.finlab import FinLabAdapter
from findb_fetcher.provider_process import ProviderProcessError, supervised_call
from findb_fetcher.providers.finlab import FINLAB_USAGE_PROBE_DATASET, FinLabSdkGateway
from provider_worker_support import fake_finlab, install_fake_sdk

# A table get would download; the usage read must never reach it.
NO_DOWNLOAD = 'raise AssertionError("usage read downloaded a table")'


def _usage_read(tmp_path, monkeypatch, metadata: str):
    install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab(NO_DOWNLOAD, metadata=metadata))
    return supervised_call("finlab.usage", {"token": "offline"}, timeout=20)


def test_the_read_asks_for_a_copy_dated_now_and_returns_the_reported_usage(tmp_path, monkeypatch):
    metadata = (
        "import datetime\n"
        "((name, saved),) = request_map.items()\n"
        f"assert name == {FINLAB_USAGE_PROBE_DATASET!r}, name\n"
        "age = datetime.datetime.now(datetime.timezone.utc) - saved\n"
        "assert abs(age.total_seconds()) < 60, age\n"
        "return {name: types.SimpleNamespace(quota=203.44, limit_size=5000, url=None)}"
    )
    assert _usage_read(tmp_path, monkeypatch, metadata) == {"used_mb": 203.44, "limit_mb": 5000.0}


def test_a_reply_without_usage_reads_as_none(tmp_path, monkeypatch):
    assert _usage_read(tmp_path, monkeypatch, "return {}") is None


@pytest.mark.parametrize(
    "error,code",
    [("finlab.exceptions.QuotaExceededError", "quota"), ("finlab.exceptions.AuthError", "auth")],
)
def test_quota_and_auth_refusals_keep_their_class(tmp_path, monkeypatch, error, code):
    with pytest.raises(ProviderProcessError) as raised:
        _usage_read(tmp_path, monkeypatch, f"raise {error}('private detail')")
    assert raised.value.code == code


@contextmanager
def _operation(samples: list):
    yield SimpleNamespace(usage=samples.append, dispatch_checkpoint=lambda: None)


def _logged(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_the_gateway_records_the_usage_under_the_dataset(monkeypatch, capsys):
    samples: list = []
    monkeypatch.setattr(acquisition, "Acquisition", lambda *a, **k: _operation(samples))
    monkeypatch.setattr(
        provider_process,
        "supervised_call",
        lambda name, payload, **kw: (
            {"used_mb": 203.44, "limit_mb": 5000.0}
            if name == "finlab.usage" and payload == {"token": "offline-token"}
            else pytest.fail("unexpected worker call")
        ),
    )

    FinLabSdkGateway(api_token="offline-token").record_usage("tw_equity_tick")

    [sample] = samples
    assert sample["bytes"] == int(203.44 * 1024 * 1024)
    assert _logged(capsys) == [
        {"finlab_quota": {"dataset": "tw_equity_tick", "used_mb": 203.4, "limit_mb": 5000.0}}
    ]


@pytest.mark.parametrize(
    "fault,code",
    [
        ("timeout", "process_timeout"),
        ("no_usage", "bad_response"),
        ("account_quota", "account_quota"),
    ],
)
def test_a_failed_read_is_one_log_line_and_never_raises(monkeypatch, capsys, fault, code):
    samples: list = []

    def operation(*args, **kwargs):
        if fault == "account_quota":
            raise QuotaBlockedError("account cooldown", quota_wait=True)
        return _operation(samples)

    def call(name, payload, **kwargs):
        if fault == "timeout":
            raise ProviderProcessError("no answer", "process_timeout")
        return None

    monkeypatch.setattr(acquisition, "Acquisition", operation)
    monkeypatch.setattr(provider_process, "supervised_call", call)

    FinLabSdkGateway(api_token="offline-token").record_usage("tw_etf_bar_1m")

    assert samples == []
    assert _logged(capsys) == [
        {"finlab_quota_unavailable": {"dataset": "tw_etf_bar_1m", "code": code}}
    ]


@pytest.mark.parametrize(
    "dataset,read",
    [
        ("tw_equity_bar_1m", True),
        ("tw_etf_tick", True),
        # Daily tables report their usage with every get already.
        ("tw_equity_eod", False),
    ],
)
def test_only_intraday_rounds_read_the_usage(monkeypatch, dataset, read):
    adapter = FinLabAdapter()
    reads: list[str] = []
    monkeypatch.setattr(
        adapter, "gateway", lambda observe: SimpleNamespace(record_usage=reads.append)
    )

    adapter.round_finished(dataset)

    assert reads == ([dataset] if read else [])


def test_an_unavailable_gateway_never_fails_the_round(monkeypatch):
    adapter = FinLabAdapter()

    def unavailable(observe):
        raise RuntimeError("FinLab pinned SDK is unavailable")

    monkeypatch.setattr(adapter, "gateway", unavailable)

    adapter.round_finished("tw_equity_tick")


def test_a_daily_round_reports_to_the_adapter_before_acceptance_closes(tmp_path):
    from test_full_market import _runtime
    from test_full_market_error_outcomes import _SettleOwner

    runtime, _, _ = _runtime(tmp_path)
    order: list[str] = []
    runtime.adapter.round_finished = lambda dataset: order.append(f"round:{dataset}")

    class Owner(_SettleOwner):
        work = {"kind": "daily", "dataset_key": "us_equity_eod"}

        def request(self, action, *args):
            order.append(action)
            return {}

    try:
        runtime._settle_queued_round(Owner(), outcome="retry")
        assert order[:2] == ["round:us_equity_eod", "close"]
    finally:
        runtime.close()


def test_other_work_kinds_do_not_report_a_round(tmp_path):
    from test_full_market import _runtime
    from test_full_market_error_outcomes import _SettleOwner

    runtime, _, _ = _runtime(tmp_path)
    rounds: list[str] = []
    runtime.adapter.round_finished = rounds.append

    class Owner(_SettleOwner):
        work = {"kind": "maintenance", "dataset_key": "us_equity_eod"}

    try:
        runtime._settle_queued_round(Owner(), outcome="completed")
        assert rounds == []
    finally:
        runtime.close()


def _stopped(*args, **kwargs):
    from findb_fetcher.provider_work import ProviderWorkAcquisitionStopped

    raise ProviderWorkAcquisitionStopped("provider acquisition is stopped")


def test_a_stopped_control_denies_the_read_without_ending_the_round(monkeypatch, capsys):
    # The denial is a BaseException; it escaped before and crash-looped the consumer
    # on staging, 2026-11-18, every time a stopped round settled.
    monkeypatch.setattr(acquisition, "Acquisition", _stopped)

    FinLabSdkGateway(api_token="offline-token").record_usage("tw_equity_bar_1m")

    assert _logged(capsys) == [
        {"finlab_quota_unavailable": {"dataset": "tw_equity_bar_1m", "code": "acquisition_stopped"}}
    ]


def test_a_stopped_round_still_settles_through_the_adapter(monkeypatch, capsys):
    adapter = FinLabAdapter()
    monkeypatch.setattr(adapter, "gateway", lambda observe: FinLabSdkGateway(api_token="offline"))
    monkeypatch.setattr(acquisition, "Acquisition", _stopped)

    adapter.round_finished("tw_equity_bar_1m")

    assert _logged(capsys)[0]["finlab_quota_unavailable"]["code"] == "acquisition_stopped"


def test_an_unprovable_ownership_boundary_is_not_swallowed(monkeypatch):
    from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

    def blocked(*args, **kwargs):
        raise ProviderWorkRecoveryBlockedError("ownership recovery required")

    monkeypatch.setattr(acquisition, "Acquisition", blocked)

    with pytest.raises(ProviderWorkRecoveryBlockedError):
        FinLabSdkGateway(api_token="offline-token").record_usage("tw_equity_tick")
