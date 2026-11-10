"""Daily publication is complete, per-round and independent of prepared recovery."""

from copy import deepcopy
from datetime import datetime

import httpx
import pytest

from execution_config_support import publication, signed
from findb_fetcher.config import FetcherConfig
from findb_fetcher.execution_config import resolve_execution_config
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from test_full_market import _runtime


def resolve(value):
    return resolve_execution_config(
        value, provider="finlab", environment="local", profile="full_market"
    )


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "environment",
        "profile",
        "provider",
        "missing",
        "extra",
        "empty",
        "duplicate",
        "adapter",
        "contract",
        "timezone",
        "clock",
        "lag",
        "window",
        "deadline",
        "monitor",
        "slot",
        "digest",
    ],
)
def test_invalid_complete_snapshot_fails_closed(fault):
    value = publication("finlab", profile="full_market", environment="local")
    feed = value["feeds"][0]
    if fault == "version":
        value["schema_version"] = 2
    elif fault in {"environment", "provider"}:
        value[fault] = "unknown"
    elif fault == "profile":
        value["execution_profile"] = "pilot"  # Retired profile fails closed.
    elif fault == "missing":
        value["feeds"].pop()
    elif fault == "extra":
        value["extra"] = True
    elif fault == "empty":
        value["feeds"] = []
    elif fault == "duplicate":
        value["feeds"].append(deepcopy(feed))
    elif fault == "adapter":
        feed["dataset_key"] = "unsupported"
    elif fault == "contract":
        feed["contract"]["current_version"] = 999
    elif fault == "timezone":
        feed["source_timezone"] = "Not/AZone"
    elif fault == "clock":
        feed["scheduled_time"] = "25:00"
    elif fault == "lag":
        feed["target_date_lag_days"] = True
    elif fault == "window":
        feed["publication_retry_window_seconds"] = 0
    elif fault == "deadline":
        feed["completion_deadline"]["local_time"] = "invalid"
    elif fault == "monitor":
        feed["bounded_monitor"] = {"enabled": True, "deadline": None}
    elif fault == "slot":
        feed["slot_id"] = "taiwan_market_window"
    signed(value)
    if fault == "digest":
        value["schedule_digest"] = "0" * 64
    with pytest.raises((ValueError, KeyError)):
        resolve(value)


@pytest.mark.parametrize("fault", [None, "timezone", "digest"])
def test_maintenance_scope_may_be_narrower_but_never_unvalidated(fault):
    value = publication("finlab", profile="full_market", environment="local")
    value["feeds"].pop()
    if fault == "timezone":
        value["feeds"][0]["source_timezone"] = "Not/AZone"
    signed(value)
    if fault == "digest":
        value["schedule_digest"] = "0" * 64
    if fault is None:
        with pytest.raises(ValueError, match="Incomplete execution config scope"):
            resolve(value)
        snapshot = resolve_execution_config(
            value,
            provider="finlab",
            environment="local",
            profile="full_market",
            complete_scope=False,
        )
        assert [feed["dataset_key"] for feed in snapshot.feeds] == ["tw_equity_eod"]
    else:
        with pytest.raises((ValueError, KeyError)):
            resolve_execution_config(
                value,
                provider="finlab",
                environment="local",
                profile="full_market",
                complete_scope=False,
            )


def test_snapshot_is_detached_and_has_complete_authorized_scopes():
    value = publication("finlab", profile="full_market", environment="local")
    snapshot = resolve(value)
    value["feeds"][0]["scheduled_time"] = "00:00"
    assert snapshot.feeds[0]["scheduled_time"] != "00:00"
    assert {f["dataset_key"] for f in snapshot.feeds} == {"tw_equity_eod", "tw_etf_eod"}
    assert all("slot_id" not in feed for feed in value["feeds"])


def test_round_replaces_snapshot_and_failure_never_reuses_previous(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    runtime, source, _ = _runtime(tmp_path)
    body = publication(scheduled_time="17:00")
    source.execution_config = lambda: body
    observations = []

    def queued(*, snapshot):
        observations.append(snapshot)
        # Backend publishes during the round: its result was already copied.
        body["feeds"][0]["scheduled_time"] = "18:00"
        signed(body)
        assert snapshot.feeds[0]["scheduled_time"] == (
            "17:00" if len(observations) == 1 else "18:00"
        )
        return {}

    runtime._queued_cycle = queued
    runtime.cycle()
    runtime.cycle()
    assert observations[0].digest != observations[1].digest

    def unavailable():
        raise FullMarketProtocolError("unavailable")

    source.execution_config = unavailable
    with pytest.raises(FullMarketProtocolError):
        runtime.cycle()
    assert runtime.schedule_snapshot is None and len(observations) == 2
    runtime.close()


def test_drain_does_not_request_schedule_or_enqueue_new_dates(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from findb_fetcher import provider_work

    runtime, source, _ = _runtime(tmp_path)
    source.execution_config = lambda: pytest.fail("prepared recovery requested daily schedule")
    runtime._ensure_proof = lambda: None
    source.request = lambda method, path: {
        "acquisition_allowed": True,
        "admitted_dataset_keys": ["us_equity_eod"],
    }
    runtime._enqueue = lambda *args: pytest.fail("drain enqueued a new date or maintenance")
    source.universes = lambda *args: pytest.fail("drain planned a date")

    @contextmanager
    def claim(*args, **kwargs):
        yield None

    monkeypatch.setattr(provider_work, "claimed_work", claim)
    runtime.drain()
    runtime.close()


@pytest.mark.parametrize(
    "instant,clock,expected",
    [
        ("2026-03-06T21:59:00+00:00", "17:00", "2026-03-05"),
        ("2026-03-06T22:00:00+00:00", "17:00", "2026-03-06"),
        ("2026-03-09T20:59:00+00:00", "17:00", "2026-03-08"),
        ("2026-03-09T21:00:00+00:00", "17:00", "2026-03-09"),
        ("2026-11-02T22:00:00+00:00", "17:00", "2026-11-02"),
        ("2027-01-01T00:00:00+00:00", "20:00", "2026-12-30"),
    ],
)
def test_published_clock_controls_due_dates_across_dst_and_year(
    tmp_path, monkeypatch, instant, clock, expected
):
    from contextlib import contextmanager

    import findb_fetcher.full_market_runtime as module
    from findb_fetcher import provider_work

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(instant).astimezone(tz)

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    runtime, source, _ = _runtime(tmp_path)
    source.execution_config = lambda: publication(scheduled_time=clock)
    runtime._ensure_proof = lambda: None
    source.request = lambda *args: {
        "acquisition_allowed": True,
        "admitted_dataset_keys": ["us_equity_eod"],
    }
    source.universes = lambda *args: {
        "activation": {"enabled": True, "activation_date": expected},
        "published_release_id": "frozen",
    }
    enqueued = []
    runtime._enqueue = enqueued.append

    @contextmanager
    def claim(*args, **kwargs):
        yield None

    monkeypatch.setattr(provider_work, "claimed_work", claim)
    runtime.cycle()
    daily = [r for r in enqueued if r["kind"] == "daily"]
    assert [row["trade_date"] for row in daily] == [expected]
    assert daily[0]["scope"]["feed"]["scheduled_time"] == clock
    runtime.close()


@pytest.mark.parametrize("fault", [None, "http", "cache", "bytes", "object"])
def test_source_publication_is_bounded_authenticated_and_never_cached(monkeypatch, fault):
    import findb_fetcher.full_market_client as module

    monkeypatch.setattr(module, "source_permit", lambda *args: None)
    requests = []

    def response(request):
        requests.append(request)
        assert request.url.path == "/api/v1/source/execution-config"
        assert dict(request.url.params) == {
            "execution_profile": "full_market",
            "schema_version": "1",
        }
        assert request.headers["X-API-Key"] == "secret"
        if fault == "http":
            return httpx.Response(503, headers={"Retry-After": "12"})
        return httpx.Response(
            200,
            headers={"Cache-Control": "public" if fault == "cache" else "no-store"},
            content=b" " * 65537 if fault == "bytes" else None,
            json=[] if fault == "object" else publication(profile="full_market"),
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        source = FullMarketSourceClient(
            FetcherConfig(source_api_url="https://source.example", source_client_key="secret"),
            client=client,
            execution_profile="full_market",
        )
        if fault:
            with pytest.raises(FullMarketProtocolError) as error:
                source.execution_config()
            if fault == "http":
                assert error.value.retryable and error.value.retry_after == 12
        else:
            assert source.execution_config()["execution_profile"] == "full_market"
            source.execution_config()
            assert len(requests) == 2


@pytest.mark.parametrize(
    "code,kind,status,accepted",
    [
        ("PROVIDER_WORK_FROZEN_FEED", "daily", 409, True),
        ("PROVIDER_WORK_CONFLICT", "daily", 409, False),
        ("PROVIDER_WORK_FROZEN_FEED", "maintenance", 409, False),
        ("PROVIDER_WORK_FROZEN_FEED", "daily", 403, False),
    ],
)
def test_only_proven_same_daily_frozen_feed_can_skip_enqueue(
    tmp_path, code, kind, status, accepted
):
    runtime, source, _ = _runtime(tmp_path)
    calls = []
    body = {"stable_key": "daily:full_market:us_equity_eod:2026-11-09", "kind": kind}

    def conflict(*args):
        calls.append(args)
        raise FullMarketProtocolError("conflict", status_code=status, code=code)

    source.request = conflict
    if accepted:
        runtime._enqueue(body)
        runtime._enqueue(body)
        assert len(calls) == 1
    else:
        with pytest.raises(FullMarketProtocolError):
            runtime._enqueue(body)
        assert not runtime.enqueued_keys
    runtime.close()


def test_metadata_alone_cannot_install_an_unknown_adapter(monkeypatch):
    import findb_fetcher.execution_config as module

    value = publication("finlab", profile="full_market", environment="local")
    value["feeds"] = [value["feeds"][0]]
    value["feeds"][0]["dataset_key"] = "tw_unknown_eod"
    monkeypatch.setattr(module, "acquisition_feeds", lambda provider: value["feeds"])
    signed(value)
    with pytest.raises(ValueError, match="Unknown"):
        resolve(value)
