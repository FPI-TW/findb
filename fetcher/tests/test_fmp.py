"""FMP market indices: one header-keyed request per index and date, mapped to market_eod v2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_errors import MemberUnavailableError, ReadinessBlockedError
from findb_fetcher.provider_adapters.base import (
    AcquisitionContext,
    FailureKind,
    UniverseContext,
)
from findb_fetcher.provider_adapters.fmp import INDEX_UNIVERSES, FMPAdapter
from findb_fetcher.providers import fmp
from findb_fetcher.providers.fmp import (
    FMPClient,
    FMPConfig,
    FMPPayloadError,
    FMPResponseError,
    FMPRowError,
    FMPRowNotPublishedError,
    contract_row,
    target_row,
)
from findb_fetcher.raw_storage import RawObject, attach_raw_object

KEY = "test-fmp-key-0123456789"
FRIDAY = date(2026, 11, 20)
# Saturday 08:30 Taipei, the US group's run for Friday.
FETCHED = datetime(2026, 11, 21, 0, 30, tzinfo=timezone.utc)
DATASET = "us_index_eod"


def _row(symbol: str = "^GSPC", day: date = FRIDAY, **changes: Any) -> dict[str, Any]:
    row = {
        "symbol": symbol,
        "date": day.isoformat(),
        "open": 6538.76,
        "high": 6552.57,
        "low": 6521.92,
        "close": 6538.76,
        "volume": 2_889_474_867,
        "change": 4.1,
        "changePercent": 0.06,
        "vwap": 6537.75,
    }
    row.update(changes)
    return row


class _FMP:
    """FMP behind one MockTransport: a queue of responses per path."""

    def __init__(self, **queues: list[httpx.Response]) -> None:
        self.queues = queues
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.queues[request.url.path.rsplit("/", 1)[1].replace("-", "_")]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _json(value: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(value).encode(), headers=headers)


class _Raw:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    def __call__(self, raw: bytes, symbol: str, *, payload_format: str = "json") -> RawObject:
        self.calls.append((raw, symbol, payload_format))
        digest = hashlib.sha256(raw).hexdigest()
        return RawObject(f"r2://{'a' * 32}/raw/fmp/{DATASET}/{digest}.json", digest, len(raw))


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", KEY)


def _context(server: _FMP, **changes: Any) -> tuple[AcquisitionContext, _Raw, list]:
    raw, observed = _Raw(), []
    values: dict[str, Any] = {
        "dataset_key": DATASET,
        "trade_date": FRIDAY,
        "fetched_at": FETCHED,
        "feed": {"provider": "fmp", "dataset_key": DATASET},
        "delivery": {
            "slot_id": "western_markets_window",
            "scheduled_for": FETCHED.isoformat(),
            "target_data_date": FRIDAY.isoformat(),
            "work_item_id": "fp1:" + "a" * 32,
        },
        "contract": {"schema_id": "market_eod", "current_version": 2},
        "limits": {"max_response_bytes": 16 * 1024 * 1024},
        "http": httpx.Client(transport=httpx.MockTransport(server)),
        "persist": raw,
        "observe_response": lambda size, declared=0: observed.append((size, declared)),
        "rate_limited": lambda: observed.append("rate_limited"),
        "snapshot": lambda url, provider_response=False: pytest.fail("no snapshot expected"),
    }
    values.update(changes)
    return AcquisitionContext(**values), raw, observed


def _member(symbol: str = "^GSPC", currency: str = "USD") -> dict[str, Any]:
    return {
        "member_key": symbol,
        "symbol": symbol,
        "provider_symbol": symbol,
        "exchange": "SNP",
        "currency": currency,
        "asset_class": "index",
        "mapping_status": "mapped",
    }


def _validated(request: dict[str, Any], stored: RawObject, contracts_dir: Path) -> dict[str, Any]:
    """The request as the runtime sends it: keyed and citing the raw object."""
    request = {**request, "request_key": "full:x", "idempotency_key": "full:x"}
    attach_raw_object(request, stored)
    ContractRegistry(contracts_dir).validate("market_eod", 2, request)
    return request


def test_the_key_travels_only_in_the_header_and_never_in_errors() -> None:
    server = _FMP(full=[_json([_row()]), _json({"Error Message": f"bad key {KEY}"}, 401)])
    client = FMPClient(
        FMPConfig.from_env(), client=httpx.Client(transport=httpx.MockTransport(server))
    )

    raw = client.index_daily("^GSPC", FRIDAY)
    with pytest.raises(FMPResponseError) as failed:
        client.index_daily("^GSPC", FRIDAY)

    assert json.loads(raw) == [_row()]
    first = server.requests[0]
    assert first.headers["apikey"] == KEY and KEY not in str(first.url)
    assert dict(first.url.params) == {"symbol": "^GSPC", "from": "2026-11-20", "to": "2026-11-20"}
    assert failed.value.category == "auth_failed" and KEY not in str(failed.value)
    assert "[redacted]" in str(failed.value)
    assert KEY not in repr(FMPConfig.from_env())


@pytest.mark.parametrize(
    ("status", "category"),
    [(429, "rate_limited"), (402, "plan_restricted"), (503, "server_error"), (404, "http_error")],
)
def test_http_failures_keep_only_a_bounded_category(status, category) -> None:
    limited: list[str] = []
    server = _FMP(full=[_json({"message": "no"}, status, **{"Retry-After": "7"})])
    client = FMPClient(
        FMPConfig.from_env(), client=httpx.Client(transport=httpx.MockTransport(server))
    )
    with pytest.raises(FMPResponseError) as failed:
        client.index_daily("^GSPC", FRIDAY, on_http_rate_limited=lambda: limited.append("x"))
    assert (failed.value.category, failed.value.status_code) == (category, status)
    assert failed.value.retry_after == 7
    assert limited == (["x"] if status == 429 else [])


def test_a_response_above_the_bound_is_refused() -> None:
    server = _FMP(full=[_json([_row()] * 50)])
    client = FMPClient(
        FMPConfig.from_env(), client=httpx.Client(transport=httpx.MockTransport(server))
    )
    with pytest.raises(FMPResponseError) as failed:
        client.index_daily("^GSPC", FRIDAY, max_response_bytes=100)
    assert failed.value.category == "response_too_large"


@pytest.mark.parametrize("symbol", ["GSPC", "^gspc", "^GS PC", "^" + "A" * 21])
def test_only_index_symbols_are_requested(symbol) -> None:
    client = FMPClient(
        FMPConfig.from_env(), client=httpx.Client(transport=httpx.MockTransport(_FMP()))
    )
    with pytest.raises(ValueError, match="index symbol"):
        client.index_daily(symbol, FRIDAY)


def test_only_the_target_dated_row_of_the_symbol_counts() -> None:
    served = [_row(day=date(2026, 11, 19)), _row("^NDX"), _row()]
    assert target_row(json.dumps(served).encode(), symbol="^GSPC", day=FRIDAY) == _row()
    # FMP serves the date only once it has a row; before that the list is empty.
    with pytest.raises(FMPRowNotPublishedError):
        target_row(b"[]", symbol="^GSPC", day=FRIDAY)
    with pytest.raises(FMPRowNotPublishedError):
        target_row(json.dumps(served[:2]).encode(), symbol="^GSPC", day=FRIDAY)
    with pytest.raises(FMPPayloadError, match="repeats"):
        target_row(json.dumps([_row(), _row()]).encode(), symbol="^GSPC", day=FRIDAY)
    for malformed in (b"{", b'{"Error Message": "x"}', b"[1]"):
        with pytest.raises(FMPPayloadError):
            target_row(malformed, symbol="^GSPC", day=FRIDAY)


def test_index_levels_and_volume_map_to_market_eod_unchanged() -> None:
    row = contract_row(_row(), symbol="^GSPC", currency="USD")
    assert row == {
        "symbol": "^GSPC",
        "source_symbol": "^GSPC",
        "trade_date": "2026-11-20",
        "currency": "USD",
        "open": "6538.76",
        "high": "6552.57",
        "low": "6521.92",
        "close": "6538.76",
        "volume": 2_889_474_867,
    }
    # FMP's zero is kept as zero: no other source serves index volume.
    assert contract_row(_row(volume=0), symbol="^VIX", currency="USD")["volume"] == 0
    assert contract_row(_row(volume=12.0), symbol="^GSPC", currency="USD")["volume"] == 12
    native = _row()
    del native["volume"]
    assert contract_row(native, symbol="^GSPC", currency="USD")["volume"] is None
    for changes in (
        {"close": None},
        {"open": 0},
        {"high": "NaN"},
        {"low": True},
        {"close": 1e13},
        {"open": [1]},
        {"volume": -1},
        {"volume": 1.5},
        {"volume": "100"},
    ):
        with pytest.raises(FMPRowError):
            contract_row(_row(**changes), symbol="^GSPC", currency="USD")


def test_a_member_is_fetched_kept_and_delivered_as_one_index_row(contracts_dir) -> None:
    served = json.dumps([_row()]).encode()
    server = _FMP(full=[httpx.Response(200, content=served)])
    context, raw, observed = _context(server)

    result = FMPAdapter().acquire(_member(), context)

    assert raw.calls == [(served, "^GSPC", "json")]
    assert observed == [(len(served), 0)]
    assert result.request is not None and result.raw_object is not None
    request = _validated(result.request, result.raw_object, contracts_dir)
    assert request["source"] == "fmp" and request["dataset_key"] == DATASET
    assert request["payload"]["batch"]["source_raw_sha256"] == hashlib.sha256(served).hexdigest()
    assert request["payload"]["data"] == [contract_row(_row(), symbol="^GSPC", currency="USD")]


def test_an_unpublished_or_invalid_row_still_cites_what_fmp_served() -> None:
    server = _FMP(full=[_json([]), _json([_row(close=None)])])
    context, raw, _ = _context(server)
    adapter = FMPAdapter()
    with pytest.raises(FMPRowNotPublishedError):
        adapter.acquire(_member(), context)
    with pytest.raises(MemberUnavailableError, match="fmp_value_invalid"):
        adapter.acquire(_member(), context)
    assert [call[0] for call in raw.calls] == [b"[]", json.dumps([_row(close=None)]).encode()]


@pytest.mark.parametrize(
    ("exc", "phase", "kind", "wait"),
    [
        (FMPRowNotPublishedError("x"), "member", FailureKind.ROUND_RETRY, "publication"),
        (FMPPayloadError("x"), "member", FailureKind.ROUND_RETRY, None),
        (FMPResponseError("x", category="rate_limited", status_code=429), "member",
         FailureKind.QUOTA_WAIT, None),
        (FMPResponseError("x", category="plan_restricted", status_code=402), "member",
         FailureKind.MEMBER, None),
        (FMPResponseError("x", category="server_error", status_code=503), "member",
         FailureKind.ROUND_RETRY, None),
        (FMPResponseError("x", category="transport"), "member", FailureKind.ROUND_RETRY, None),
        (FMPResponseError("x", category="server_error", status_code=503), "maintenance",
         FailureKind.ROUND_RETRY, None),
        (FMPResponseError("x", category="auth_failed", status_code=401), "maintenance",
         FailureKind.WORK_FAILED, None),
        # Only a member waits for quota; maintenance and rounds settle a retry.
        (FMPResponseError("x", category="rate_limited", status_code=429), "maintenance",
         FailureKind.ROUND_RETRY, None),
        (FMPResponseError("x", category="rate_limited", status_code=429), "round",
         FailureKind.ROUND_RETRY, None),
        (FMPPayloadError("x"), "maintenance", FailureKind.ROUND_RETRY, None),
        (FMPResponseError("x", category="transport"), "round", FailureKind.ROUND_RETRY, None),
        (FMPResponseError("x", category="response_too_large"), "round",
         FailureKind.WORK_FAILED, None),
        (ValueError("other"), "member", FailureKind.PASSTHROUGH, None),
    ],
)  # fmt: skip
def test_failures_are_classified_by_scope(exc, phase, kind, wait) -> None:
    failure = FMPAdapter().classify(exc, phase)
    assert (failure.kind, failure.wait) == (kind, wait)


def test_historical_outcomes_are_deterministic_only_for_the_row() -> None:
    adapter = FMPAdapter()
    # A past date FMP has no row for stays missing; a bad row is invalid.
    assert adapter.member_outcome(FMPRowNotPublishedError("x")) == ("missing", "no_source_row")
    assert adapter.member_outcome(MemberUnavailableError("fmp_value_invalid")) == (
        "invalid",
        "row_structure_invalid",
    )
    # Rate limits and outages retry the date.
    assert adapter.member_outcome(FMPResponseError("x", category="rate_limited")) is None
    assert adapter.member_outcome(FMPPayloadError("x")) is None
    assert adapter.diagnostic(FMPResponseError("x", category="server_error", status_code=502)) == {
        "category": "server_error",
        "http_status": 502,
    }


def _listing(**changes: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {"symbol": symbol, "name": symbol, "exchange": "HKSE" if ccy == "HKD" else "SNP",
         "currency": ccy}
        for group in INDEX_UNIVERSES.values()
        for symbol, ccy in group.items()
    ] + [{"symbol": "^TWII", "name": "TSEC", "exchange": "TAI", "currency": "TWD"}]  # fmt: skip
    return [
        {**row, **changes.get(row["symbol"], {})}
        for row in rows
        if changes.get(row["symbol"]) != {}
    ]


def _universe_context(server: _FMP, observed_at: datetime = FETCHED) -> UniverseContext:
    return UniverseContext(
        dataset_keys=list(INDEX_UNIVERSES),
        observed_at=observed_at,
        zones={"hk_index_eod": ZoneInfo("Asia/Hong_Kong"), "us_index_eod": ZoneInfo("Asia/Taipei")},
        snapshot=lambda url, provider_response=False: pytest.fail("no official snapshot"),
        catalogue=lambda **kwargs: pytest.fail("no runtime catalogue"),
        catalogue_evidence=lambda url: pytest.fail("no runtime catalogue"),
        persist=lambda *args, **kwargs: pytest.fail("evidence is persisted by the runtime"),
        observe_response=lambda *args, **kwargs: None,
        snapshot_objects={},
        refresh_limits=lambda: None,
        pace=lambda: None,
        http=httpx.Client(transport=httpx.MockTransport(server)),
        target_date_lags={"hk_index_eod": 0, "us_index_eod": 1},
    )


# Packaged descriptors carry no schedule: the lag comes only from Source.
FEEDS = [{"dataset_key": "hk_index_eod"}, {"dataset_key": "us_index_eod"}]


def test_the_universe_is_the_fixed_groups_as_fmp_lists_them() -> None:
    listing = json.dumps(_listing()).encode()
    server = _FMP(index_list=[httpx.Response(200, content=listing)])

    groups = FMPAdapter().build_universe(FEEDS, _universe_context(server))

    assert len(server.requests) == 1 and server.requests[0].headers["apikey"] == KEY
    hk, us = groups["hk_index_eod"], groups["us_index_eod"]
    url = "https://financialmodelingprep.com/stable/index-list"
    assert hk.evidence == us.evidence == {url: listing}
    # Saturday 08:30 Taipei lists Friday's New York members; Hong Kong lists its own date.
    assert (hk.effective_date, us.effective_date) == (date(2026, 11, 21), FRIDAY)
    assert [m["symbol"] for m in us.members] == list(INDEX_UNIVERSES["us_index_eod"])
    assert hk.members[0] == {
        "symbol": "^HSI",
        "provider_symbol": "^HSI",
        "exchange": "HKSE",
        "currency": "HKD",
        "asset_class": "index",
        "mapping_status": "mapped",
    }


@pytest.mark.parametrize("changes", [{"^NYA": {}}, {"^HSI": {"currency": "USD"}}])
def test_a_missing_or_redenominated_index_stops_the_refresh(changes) -> None:
    server = _FMP(index_list=[_json(_listing(**changes))])
    with pytest.raises(ReadinessBlockedError, match="does not carry"):
        FMPAdapter().build_universe(FEEDS, _universe_context(server))


def test_the_refresh_needs_the_governed_client() -> None:
    context = replace(_universe_context(_FMP()), http=None)
    with pytest.raises(ReadinessBlockedError, match="governed HTTP client"):
        FMPAdapter().build_universe(FEEDS, context)
    with pytest.raises(ReadinessBlockedError, match="no index list"):
        FMPAdapter().build_universe([{"dataset_key": "tw_index_eod"}], _universe_context(_FMP()))


@pytest.mark.parametrize(
    "lags", [None, {"hk_index_eod": 0}, {"hk_index_eod": 0, "us_index_eod": -1}]
)
def test_the_effective_date_needs_the_published_lag(lags) -> None:
    server = _FMP(index_list=[_json(_listing())])
    context = replace(_universe_context(server), target_date_lags=lags)
    with pytest.raises(ReadinessBlockedError, match="published target date lag"):
        FMPAdapter().build_universe(FEEDS, context)


def test_a_runtime_refresh_dates_new_york_members_by_the_published_lag(tmp_path, monkeypatch):
    """The whole refresh path: Source publishes 08:30 Taipei with lag 1 for New York."""
    from test_provider_universe_golden import FIXED, _refresh

    listing = json.dumps(_listing()).encode()
    result = _refresh(
        tmp_path,
        monkeypatch,
        "fmp",
        {"https://financialmodelingprep.com/stable/index-list": listing},
        ["hk_index_eod", "us_index_eod"],
    )
    bodies = {body["dataset_key"]: body for body in map(json.loads, result["submitted"])}
    # 01:30 UTC is 09:30 in both Hong Kong and Taipei on 2026-10-02.
    assert FIXED.isoformat() == "2026-10-02T01:30:00+00:00"
    assert bodies["hk_index_eod"]["effective_date"] == "2026-10-02"
    assert bodies["us_index_eod"]["effective_date"] == "2026-10-01"
    assert bodies["us_index_eod"]["source_timezone"] == "Asia/Taipei"
    assert {m["symbol"] for m in bodies["us_index_eod"]["members"]} == set(
        INDEX_UNIVERSES["us_index_eod"]
    )


def test_a_restarted_attempt_replays_only_a_response_with_the_target_row(tmp_path) -> None:
    from types import SimpleNamespace

    from findb_fetcher import provider_work

    server = _FMP(full=[_json([]), _json([_row()])])
    client = FMPClient(
        FMPConfig.from_env(), client=httpx.Client(transport=httpx.MockTransport(server))
    )
    path = tmp_path / "work.sqlite3"

    def owner() -> SimpleNamespace:
        journal = provider_work.WorkJournal(path, "work")
        return SimpleNamespace(journal=journal, provider="fmp", dispatch_observations=0)

    token = provider_work._current.set(owner())
    try:
        assert client.index_daily("^GSPC", FRIDAY) == b"[]"
        provider_work._current.set(owner())
        # An empty answer authorizes no replay: the next attempt asks FMP again.
        published = client.index_daily("^GSPC", FRIDAY)
        provider_work._current.set(owner())
        assert client.index_daily("^GSPC", FRIDAY) == published
    finally:
        provider_work._current.reset(token)
    assert json.loads(published) == [_row()] and len(server.requests) == 2


def test_fmp_journal_bodies_retire_with_their_raw_copies() -> None:
    from findb_fetcher import provider_work

    assert "fmp" in provider_work._R2_BACKED_RESPONSES
    assert fmp.FMP_DAILY_RESPONSE == "fmp_index_daily"


def test_a_historical_date_delivers_each_index_through_the_member_runner(monkeypatch, tmp_path):
    from findb_fetcher import member_historical
    from findb_fetcher.member_historical import CURRENT_RELEASE_COHORT, MemberHistoricalRunner
    from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig
    from test_member_historical import _S3, _Delivery, _item, _Owner

    monkeypatch.setattr(
        member_historical,
        "historical_feed",
        lambda provider, dataset: {
            "provider": provider,
            "dataset_key": dataset,
            "contract": {"schema_id": "market_eod", "current_version": 2},
            "delivery": {"slot_id": "western_markets_window"},
        },
    )
    day = date(2021, 3, 5)
    served = {"^GSPC": [_row(day=day)], "^NYA": []}
    server_requests: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params["symbol"]
        server_requests.append(symbol)
        return _json(served[symbol])

    members = [_member("^GSPC"), _member("^NYA")]
    item = replace(
        _item("fmp", DATASET, members),
        market="US",
        trade_date=day,
    )
    # One list, current when the request was created, serves every date of it.
    item = replace(
        item,
        universe_snapshot={**item.universe_snapshot, "cohort": CURRENT_RELEASE_COHORT},
    )
    owner = _Owner(tmp_path, item)
    monkeypatch.setattr(member_historical, "active", lambda: owner)
    s3, delivery = _S3(), _Delivery()
    runner = MemberHistoricalRunner(
        "fmp",
        delivery=delivery,
        raw_store=R2RawPayloadStore(
            RawStorageConfig(
                account_id="a" * 32,
                bucket="findb-raw-test",
                access_key_id="k",
                secret_access_key="s",
            ),
            client=s3,
        ),
        http=httpx.Client(transport=httpx.MockTransport(answer)),
        adapter=FMPAdapter(),
    )
    try:
        result = runner.run(item)
    finally:
        runner.close()

    assert server_requests == ["^GSPC", "^NYA"]
    outcomes = {row["member_key"]: row for row in result.result_evidence["members"]}
    assert outcomes["^GSPC"]["outcome"] == "data"
    # A past date without a row is missing, citing the empty answer FMP gave.
    assert (outcomes["^NYA"]["outcome"], outcomes["^NYA"]["reason"]) == (
        "missing",
        "no_source_row",
    )
    assert outcomes["^NYA"]["raw"]["sha256"] == hashlib.sha256(b"[]").hexdigest()
    assert [put["Metadata"]["source-symbol"] for put in s3.puts] == ["^GSPC", "^NYA"]
    (body,) = delivery.prepared
    assert body["payload"]["batch"]["delivery_mode"] == "backfill"
    assert body["payload"]["data"][0]["symbol"] == "^GSPC"
    assert body["payload"]["data"][0]["trade_date"] == "2021-03-05"
