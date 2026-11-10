"""Provider adapters: capabilities, failure classes and context-only acquisition."""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from execution_config_support import publication
from findb_fetcher.execution_metadata import acquisition_feeds
from findb_fetcher.full_market_errors import (
    DatedUniversePendingError,
    MemberUnavailableError,
    ProviderHTTPError,
)
from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.provider_adapters import (
    ADAPTERS,
    AcquisitionContext,
    FactSetAdapter,
    FailureKind,
    FinLabAdapter,
    ProviderAdapter,
    ProviderFailure,
    TaifexAdapter,
    TwelveDataAdapter,
    adapter_for,
)
from findb_fetcher.providers.factset import FactSetOperationalError
from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabDatasetTable,
    FinLabOperationalError,
    FinLabTargetNotReadyError,
    FinLabValueError,
)
from findb_fetcher.providers.taifex import TaifexOperationalError
from findb_fetcher.providers.twelve_data import TwelveDataResponseError
from findb_fetcher.raw_storage import RawObject

DAY = date(2026, 10, 1)
FETCHED = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)
FIXTURE = json.loads((Path(__file__).parent / "fixtures/execution_config.v1.json").read_text())


def _feed(provider: str, dataset: str | None = None) -> dict[str, Any]:
    """A Source-published feed plus its packaged slot, never a hand-written subset."""
    feeds = publication(provider)["feeds"]
    (feed,) = [f for f in feeds if dataset is None or f["dataset_key"] == dataset][:1]
    (installed,) = [
        f for f in acquisition_feeds(provider) if f["dataset_key"] == feed["dataset_key"]
    ]
    return {**feed, "slot_id": installed["delivery"]["slot_id"]}


class _Raw:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    def __call__(self, raw: bytes, symbol: str, *, payload_format: str = "json") -> RawObject:
        self.calls.append((raw, symbol, payload_format))
        digest = hashlib.sha256(raw).hexdigest()
        return RawObject(f"r2://{'a' * 32}/raw/{digest}.json", digest, len(raw))


def _context(feed: dict[str, Any], **changes: Any) -> tuple[AcquisitionContext, _Raw, list]:
    raw, observed = _Raw(), []
    (installed,) = [
        f for f in acquisition_feeds(feed["provider"]) if f["dataset_key"] == feed["dataset_key"]
    ]
    values: dict[str, Any] = {
        "dataset_key": feed["dataset_key"],
        "trade_date": DAY,
        "fetched_at": FETCHED,
        "feed": feed,
        "delivery": {
            "slot_id": feed["slot_id"],
            "scheduled_for": FETCHED.isoformat(),
            "target_data_date": DAY.isoformat(),
            "work_item_id": "historical:item",
        },
        "contract": installed["contract"],
        "limits": {"max_response_bytes": 100000},
        "http": httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        "persist": raw,
        "observe_response": lambda size, declared=0: observed.append((size, declared)),
        "rate_limited": lambda: observed.append("rate_limited"),
        "snapshot": lambda url, provider_response=False: pytest.fail("no snapshot expected"),
    }
    values.update(changes)
    return AcquisitionContext(**values), raw, observed


def test_registry_covers_every_published_provider_with_its_capabilities():
    assert set(ADAPTERS) == set(FIXTURE)
    for provider in FIXTURE:
        adapter = adapter_for(provider)
        assert adapter.provider == provider
        assert adapter.scheduled_maintenance is (provider != "taifex")
        assert adapter.batch is (provider == "factset")
        assert (adapter.dated_universe_code is not None) is (provider == "taifex")
        assert (adapter.outage_retry_seconds is not None) is (provider == "taifex")
        assert (adapter.shared_response_prefix is not None) is (provider == "finlab")
    retired = adapter_for("shioaji")
    assert type(retired) is ProviderAdapter and retired.classify(RuntimeError(), "member") == (
        ProviderFailure(FailureKind.PASSTHROUGH)
    )


def _td(status=None, *, category=None, provider_code=None):
    return TwelveDataResponseError(
        "private provider text", status_code=status, provider_code=provider_code, category=category
    )


@pytest.mark.parametrize(
    ("adapter", "exc", "phase", "expected"),
    [
        # Twelve Data member loop.
        (TwelveDataAdapter, _td(429), "member", ProviderFailure(FailureKind.QUOTA_WAIT, reason="rate_limited")),
        (TwelveDataAdapter, _td(200, provider_code=429), "member", ProviderFailure(FailureKind.QUOTA_WAIT, reason="rate_limited")),
        (TwelveDataAdapter, _td(404), "member", ProviderFailure(FailureKind.MEMBER, no_data=True)),
        (TwelveDataAdapter, _td(200, category="no_data"), "member", ProviderFailure(FailureKind.MEMBER, no_data=True)),
        (TwelveDataAdapter, _td(503), "member", ProviderFailure(FailureKind.ROUND_RETRY)),
        (TwelveDataAdapter, _td(None), "member", ProviderFailure(FailureKind.ROUND_RETRY)),
        # Twelve Data daily rounds and maintenance share the HTTP retry rule.
        (TwelveDataAdapter, _td(503), "round", ProviderFailure(FailureKind.ROUND_RETRY, http=True, status_code=503)),
        (TwelveDataAdapter, _td(None), "maintenance", ProviderFailure(FailureKind.ROUND_RETRY, http=True)),
        (TwelveDataAdapter, _td(401), "maintenance", ProviderFailure(FailureKind.WORK_FAILED, http=True, status_code=401)),
        (TwelveDataAdapter, FinLabOperationalError("x"), "round", ProviderFailure(FailureKind.PASSTHROUGH)),
        # FinLab: target-not-ready waits for publication; maintenance codes are fixed.
        (FinLabAdapter, FinLabTargetNotReadyError("x"), "member", ProviderFailure(FailureKind.ROUND_RETRY, wait="publication", code="finlab_target_not_ready")),
        (FinLabAdapter, FinLabOperationalError("x"), "member", ProviderFailure(FailureKind.PASSTHROUGH)),
        (FinLabAdapter, FinLabOperationalError("x"), "round", ProviderFailure(FailureKind.PASSTHROUGH)),
        (FinLabAdapter, FinLabOperationalError("x"), "maintenance", ProviderFailure(FailureKind.ROUND_RETRY, code="MAINTENANCE_FINLAB_OPERATIONAL")),
        (FinLabAdapter, FinLabConfigError("x"), "maintenance", ProviderFailure(FailureKind.WORK_FAILED, code="MAINTENANCE_FINLAB_CONFIG")),
        # TAIFEX: a shared report outage never charges the member.
        (TaifexAdapter, httpx.ReadError("x"), "member", ProviderFailure(FailureKind.ROUND_RETRY, wait="outage")),
        (TaifexAdapter, DatedUniversePendingError("x"), "member", ProviderFailure(FailureKind.ROUND_RETRY, wait="outage")),
        (TaifexAdapter, TaifexOperationalError("x", status_code=429, retry_after=900), "member", ProviderFailure(FailureKind.ROUND_RETRY, wait="outage", retry_after=900)),
        (TaifexAdapter, ProviderHTTPError("x", 503), "member", ProviderFailure(FailureKind.ROUND_RETRY, wait="outage")),
        (TaifexAdapter, ProviderHTTPError("x", 403), "member", ProviderFailure(FailureKind.PASSTHROUGH)),
        (TaifexAdapter, TaifexOperationalError("x"), "member", ProviderFailure(FailureKind.PASSTHROUGH)),
        (TaifexAdapter, TaifexOperationalError("x"), "round", ProviderFailure(FailureKind.WORK_FAILED, http=True)),
        (TaifexAdapter, TaifexOperationalError("x", status_code=503), "round", ProviderFailure(FailureKind.ROUND_RETRY, http=True, status_code=503)),
        (TaifexAdapter, TaifexOperationalError("x", status_code=503), "maintenance", ProviderFailure(FailureKind.PASSTHROUGH)),
        # FactSet: the operational code is the maintenance failure code.
        (FactSetAdapter, FactSetOperationalError("FACTSET_QUERY_UNAVAILABLE", retryable=True), "round", ProviderFailure(FailureKind.ROUND_RETRY, code="FACTSET_QUERY_UNAVAILABLE")),
        (FactSetAdapter, FactSetOperationalError("FACTSET_QUERY_REJECTED", retryable=False), "maintenance", ProviderFailure(FailureKind.WORK_FAILED, code="FACTSET_QUERY_REJECTED")),
        (FactSetAdapter, FactSetOperationalError("FACTSET_QUERY_REJECTED", retryable=False), "member", ProviderFailure(FailureKind.PASSTHROUGH)),
    ],
)  # fmt: skip
def test_adapter_classifies_only_its_own_provider_failures(adapter, exc, phase, expected):
    assert adapter().classify(exc, phase) == expected


def test_provider_diagnostics_are_bounded_classes():
    assert TwelveDataAdapter().diagnostic(_td(404)) == {
        "category": "not_found",
        "http_status": 404,
        "provider_code": None,
        "dispatched": None,
    }
    assert FinLabAdapter().diagnostic(FinLabValueError("price_invalid", "private")) == {
        "category": "finlab_value:price_invalid"
    }
    assert TaifexAdapter().diagnostic(TaifexOperationalError("private")) is None


def test_twelve_data_acquire_uses_only_its_context(monkeypatch):
    """A historical runner can acquire one member without plan, part or owner state."""
    from findb_fetcher.providers.twelve_data import (
        TwelveDataClient,
        TwelveDataConfig,
        TwelveDataResponse,
    )

    payload = {
        "status": "ok",
        "meta": {
            "symbol": "AAPL",
            "type": "Common Stock",
            "currency": "USD",
            "exchange": "NASDAQ",
            "mic_code": "XNGS",
            "interval": "1day",
            "exchange_timezone": "America/New_York",
        },
        "values": [
            {
                "datetime": DAY.isoformat(),
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "2",
                "volume": "10",
            }
        ],
    }
    native = canonical_bytes(payload)
    monkeypatch.setattr(TwelveDataConfig, "from_env", classmethod(lambda cls: cls(api_key="k")))
    monkeypatch.setattr(
        TwelveDataClient,
        "fetch_daily",
        lambda self, symbol, **kwargs: TwelveDataResponse(payload, raw_bytes=native),
    )
    feed = _feed("twelve_data")
    context, raw, observed = _context(feed)
    adapter = TwelveDataAdapter()
    try:
        result = adapter.acquire(
            {"symbol": "AAPL", "provider_symbol": "AAPL", "currency": "USD"}, context
        )
    finally:
        adapter.close()
    assert result.no_trade is None and result.raw_object is not None
    assert raw.calls == [(native, "AAPL", "json")] and observed == [(len(native), 0)]
    request = result.request
    assert request is not None
    assert request["delivery"] == context.delivery
    assert request["delivery"]["slot_id"] == "western_markets_window"
    assert request["payload"]["data"][0]["trade_date"] == DAY.isoformat()


def test_finlab_acquire_reports_no_trade_as_result_without_source(monkeypatch):
    names = ("open", "high", "low", "close", "volume")
    values = {"open": math.nan, "high": math.nan, "low": math.nan, "close": math.nan, "volume": 0}

    class Gateway:
        def fetch_dataset(self, name, *, target_date, symbols):
            field = names[
                [
                    "price:開盤價",
                    "price:最高價",
                    "price:最低價",
                    "price:收盤價",
                    "price:成交股數",
                ].index(name)
            ]
            return FinLabDatasetTable((DAY.isoformat(),), ("1101",), ((values[field],),))

    feed = _feed("finlab", "tw_etf_eod")
    member = {
        "member_key": "1101",
        "symbol": "1101",
        "provider_symbol": "1101",
        "mapping_status": "mapped",
    }
    context, raw, _ = _context(
        feed,
        cohort=[
            member,
            {
                **member,
                "member_key": "GAP",
                "provider_symbol": "GAP",
                "symbol": "GAP",
                "mapping_status": "gap",
            },
        ],
    )
    adapter = FinLabAdapter()
    adapter.gateway = lambda observe: Gateway()  # type: ignore[method-assign]
    result = adapter.acquire(member, context)
    assert result.request is None and result.no_trade is not None
    assert result.no_trade["url"].endswith("dataset=tw_etf_eod")
    assert result.no_trade["observed_at"] == FETCHED.isoformat()
    assert [call[1] for call in raw.calls] == ["dataset_bundle"]
    with pytest.raises(MemberUnavailableError, match="missing_symbol"):
        adapter.acquire({**member, "provider_symbol": "2330", "symbol": "2330"}, context)
    adapter.reset()
    assert adapter.bundles == {}


def test_taifex_contract_version_comes_from_packaged_catalog():
    report = canonical_bytes(
        [
            {
                "Date": DAY.strftime("%Y%m%d"),
                "Contract": "TX",
                "ContractMonth(Week)": "202610",
                "Open": "1",
                "High": "2",
                "Low": "1",
                "Last": "2",
                "Volume": "3",
                "SettlementPrice": "2",
                "OpenInterest": "4",
                "TradingSession": "一般",
            }
        ]
    )
    feed = _feed("taifex")
    context, raw, _ = _context(feed, snapshot=lambda url, provider_response=False: report)
    member = {"contract_code": "TX:202610", "session": "regular", "provider_symbol": "TX:202610"}
    result = TaifexAdapter().acquire(member, context)
    assert result.request is not None
    assert (
        (result.request["schema_id"], result.request["schema_version"])
        == (
            context.contract["schema_id"],  # type: ignore[index]
            context.contract["current_version"],  # type: ignore[index]
        )
        == ("futures_eod", 1)
    )
    assert raw.calls[0] == (report, "exchange_report", "json")


@contextmanager
def _claimed(owner):
    yield owner


def _round_runtime(tmp_path, provider):
    from test_full_market import _runtime

    runtime, source, _ = _runtime(tmp_path, provider=provider)
    runtime._ensure_proof = lambda: None
    # Catalog feeds, never the low-level harness field subset.
    runtime.installed_feeds = runtime.feeds = [_feed(provider)]
    source.request = lambda *args, **kwargs: {"acquisition_allowed": False}
    return runtime


@pytest.mark.parametrize(
    ("provider", "exc", "expected"),
    [
        ("twelve_data", _td(503), ("retry", "DAILY_PROVIDER_HTTP_503")),
        ("twelve_data", _td(400), ("failed", "DAILY_PROVIDER_HTTP_400")),
        ("taifex", TaifexOperationalError("bound"), ("failed", "DAILY_PROVIDER_HTTP_UNKNOWN")),
        ("taifex", TaifexOperationalError("x", status_code=502), ("retry", "DAILY_PROVIDER_HTTP_502")),
        ("factset", FactSetOperationalError("FACTSET_QUERY_UNAVAILABLE", retryable=True), ("retry", "DAILY_PROVIDER_UNAVAILABLE")),
        ("factset", FactSetOperationalError("FACTSET_QUERY_REJECTED", retryable=False), ("failed", "DAILY_PROVIDER_REJECTED")),
        ("finlab", FinLabOperationalError("x"), None),
        ("taifex", ProviderHTTPError("snapshot", 503), ("retry", "DAILY_PROVIDER_HTTP_503")),
    ],
)  # fmt: skip
def test_daily_round_failure_codes_come_from_adapter_classes(
    tmp_path, monkeypatch, provider, exc, expected
):
    from findb_fetcher import provider_work

    runtime = _round_runtime(tmp_path, provider)
    owner = SimpleNamespace(
        work={
            "kind": "daily",
            "dataset_key": runtime.feeds[0]["dataset_key"],
            "state": "open",
            "trade_date": "2026-10-15",
            "scope": {"feed": runtime.feeds[0]},
        }
    )
    monkeypatch.setattr(provider_work, "claimed_work", lambda *args, **kwargs: _claimed(owner))

    def plan(*args):
        raise exc

    runtime._plan_for_date = plan
    settled = []
    runtime._settle_queued_round = lambda *args, **kwargs: settled.append(
        (kwargs["outcome"], kwargs["failure_code"])
    )
    try:
        if expected is None:
            with pytest.raises(type(exc)):
                runtime._queued_cycle(plan_new_dates=False)
            assert settled == []
        else:
            runtime._queued_cycle(plan_new_dates=False)
            assert settled == [expected]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("provider", "exc", "expected"),
    [
        ("twelve_data", _td(503), ("retry", "MAINTENANCE_HTTP_503")),
        ("twelve_data", _td(None), ("retry", "MAINTENANCE_HTTP_UNKNOWN")),
        ("twelve_data", _td(401), ("failed", "MAINTENANCE_HTTP_401")),
        ("factset", FactSetOperationalError("FACTSET_QUERY_UNAVAILABLE", retryable=True), ("retry", "FACTSET_QUERY_UNAVAILABLE")),
        ("finlab", FinLabOperationalError("x"), ("retry", "MAINTENANCE_FINLAB_OPERATIONAL")),
        ("finlab", FinLabConfigError("x"), ("failed", "MAINTENANCE_FINLAB_CONFIG")),
        ("taifex", TaifexOperationalError("x", status_code=503), None),
        ("finlab", ProviderHTTPError("listing", 404), ("failed", "MAINTENANCE_HTTP_404")),
    ],
)  # fmt: skip
def test_maintenance_failure_codes_come_from_adapter_classes(tmp_path, provider, exc, expected):
    from findb_fetcher import provider_work as queue

    runtime = _round_runtime(tmp_path, provider)
    owner = SimpleNamespace(
        work={"state": "open", "dataset_key": runtime.installed_feeds[0]["dataset_key"]},
        journal=queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
        dispatch_observations=0,
    )
    settled = []
    runtime._settle_queued_round = lambda *args, **kwargs: settled.append(
        (kwargs["outcome"], kwargs["failure_code"])
    )

    def sync(**kwargs):
        raise exc

    runtime.sync_universes = sync
    try:
        if expected is None:
            with pytest.raises(type(exc)):
                runtime._run_queued_maintenance(owner)
            assert settled == []
        else:
            runtime._run_queued_maintenance(owner)
            assert settled == [expected]
    finally:
        runtime.close()
