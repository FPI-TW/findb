"""One comma-separated Twelve Data request serves a part; each member delivers alone."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataResponse,
    TwelveDataResponseError,
    _batch_target_rows_present,
)
from test_provider_adapter_golden import DAY, _clock, _feed, _plan, _runtime, _twelve_payload, _us

NOT_FOUND = {
    "code": 404,
    "message": "**symbol** not found: NOPE. Please specify it correctly.",
    "meta": {"symbol": "AAPL,IBM,NOPE", "interval": "1day", "exchange": ""},
    "status": "error",
}
NO_DATA = {
    "code": 400,
    "message": "No data is available on the specified dates.",
    "status": "error",
}


def _client(handler) -> TwelveDataClient:
    return TwelveDataClient(
        TwelveDataConfig(api_key="provider-secret", base_url="https://provider.example.test"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_batch_is_one_request_charging_one_unit_per_symbol(monkeypatch):
    from findb_fetcher import acquisition

    units: list[int] = []
    original = acquisition.Acquisition.__init__

    def counted(self, *args, **kwargs):
        units.append(kwargs.get("units", 1))
        original(self, *args, **kwargs)

    monkeypatch.setattr(acquisition.Acquisition, "__init__", counted)
    requests: list[httpx.Request] = []
    body = {
        "AAPL": _twelve_payload("AAPL", "11"),
        "IBM": _twelve_payload("IBM", "12"),
        "NOPE": NOT_FOUND,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=body, request=request)

    client = _client(handler)
    batch = client.fetch_daily_batch(("AAPL", "IBM", "NOPE"), start_date=DAY, end_date=DAY)
    assert len(requests) == 1 and units == [3]
    params = requests[0].url.params
    assert params["symbol"] == "AAPL,IBM,NOPE" and params["adjust"] == "none"
    assert (params["start_date"], params["end_date"]) == (DAY.isoformat(), "2026-10-02")
    assert "provider-secret" not in str(requests[0].url)
    # Each symbol reads like its own single request, over the shared exact bytes.
    aapl = client.batch_series(batch, "AAPL")
    assert aapl["meta"]["symbol"] == "AAPL" and aapl.raw_bytes == batch.raw_bytes
    with pytest.raises(TwelveDataResponseError) as missing:
        client.batch_series(batch, "NOPE")
    assert (missing.value.provider_code, missing.value.category) == (404, "not_found")
    assert missing.value.dispatched is True


def test_symbol_errors_keep_single_request_categories():
    client = _client(lambda request: httpx.Response(200, json={}, request=request))
    batch = TwelveDataResponse(
        {"AAPL": NO_DATA, "IBM": {"code": 429, "message": "limit", "status": "error"}},
        raw_bytes=b"{}",
    )
    limited: list[bool] = []
    with pytest.raises(TwelveDataResponseError) as no_data:
        client.batch_series(batch, "AAPL")
    assert no_data.value.category == "no_data"
    with pytest.raises(TwelveDataResponseError) as rate:
        client.batch_series(batch, "IBM", on_http_rate_limited=lambda: limited.append(True))
    assert rate.value.is_rate_limited and limited == [True]


@pytest.mark.parametrize(
    "body",
    [
        {"AAPL": _twelve_payload("AAPL", "11")},  # a requested symbol is missing
        {"AAPL": _twelve_payload("AAPL", "11"), "IBM": {}, "MSFT": {}},  # an extra key
        {"AAPL": _twelve_payload("AAPL", "11"), "IBM": ["not", "an", "object"]},
    ],
)
def test_incomplete_or_malformed_batches_fail_whole(body):
    client = _client(lambda request: httpx.Response(200, json=body, request=request))
    with pytest.raises(TwelveDataResponseError) as error:
        client.fetch_daily_batch(("AAPL", "IBM"), start_date=DAY, end_date=DAY)
    assert error.value.category == "malformed_response"


def test_whole_request_error_keeps_its_category():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 429, "message": "limit", "status": "error"}, request=request
        )

    with pytest.raises(TwelveDataResponseError) as error:
        _client(handler).fetch_daily_batch(("AAPL", "IBM"), start_date=DAY, end_date=DAY)
    assert error.value.is_rate_limited


@pytest.mark.parametrize(
    "symbols", [("AAPL",), ("AAPL", "AAPL"), ("AAPL", "A,B"), tuple(f"S{i}" for i in range(51))]
)
def test_batch_symbols_are_bounded_and_distinct(symbols):
    client = _client(lambda request: pytest.fail("no request for invalid symbols"))
    with pytest.raises(ValueError):
        client.fetch_daily_batch(symbols, start_date=DAY, end_date=DAY)


def test_only_a_fully_served_batch_may_replay():
    arguments = {"symbols": ("AAPL", "IBM"), "start_date": DAY, "end_date": DAY}
    served = {"AAPL": _twelve_payload("AAPL", "11"), "IBM": _twelve_payload("IBM", "12")}
    assert _batch_target_rows_present(served, arguments)
    # A later retry round must ask again instead of replaying a no-data answer.
    assert not _batch_target_rows_present({**served, "IBM": NO_DATA}, arguments)
    stale = {**served, "IBM": {**served["IBM"], "values": []}}
    assert not _batch_target_rows_present(stale, arguments)
    assert not _batch_target_rows_present({"AAPL": served["AAPL"]}, arguments)


def test_part_shares_one_batch_and_each_member_delivers_alone(tmp_path, monkeypatch):
    _clock(monkeypatch)
    monkeypatch.setattr(TwelveDataConfig, "from_env", classmethod(lambda cls: cls(api_key="k")))
    batches: list[tuple[str, ...]] = []

    def fetch_daily_batch(self, symbols, *, start_date, end_date, **kwargs):
        assert (start_date, end_date) == (DAY, DAY)
        batches.append(symbols)
        payload: dict[str, Any] = {
            symbol: NO_DATA if symbol == "NODATA" else _twelve_payload(symbol, "11")
            for symbol in symbols
        }
        return TwelveDataResponse(payload, raw_bytes=canonical_bytes(payload))

    monkeypatch.setattr(TwelveDataClient, "fetch_daily_batch", fetch_daily_batch)
    monkeypatch.setattr(
        TwelveDataClient, "fetch_daily", lambda *args, **kwargs: pytest.fail("one batch per part")
    )
    runtime, source, delivery, s3 = _runtime(tmp_path, "twelve_data")
    keys = ["AAPL", "GAP", "NODATA", "IBM", "DONE"]
    plan = _plan("us_equity_eod", keys)
    release = {
        "release_id": "r",
        "members": [
            *(_us(key) for key in keys if key != "GAP"),
            {**_us("GAP"), "mapping_status": "gap"},
        ],
    }
    runtime.state.record_plan(plan, "twelve_data")
    runtime.state.finish("fp1:us_equity_eod:DONE", status="complete")
    try:
        runtime._deliver_plan(plan, release, _feed("twelve_data", "us_equity_eod"))
    finally:
        runtime.close()
    # Mapping gaps and finished members never join (or spend credits in) the batch.
    assert batches == [("AAPL", "NODATA", "IBM")]
    assert len(s3.puts) == 1
    bodies = [json.loads(body) for body in delivery.prepared]
    assert [body["payload"]["data"][0]["symbol"] for body in bodies] == ["AAPL", "IBM"]
    shas = {body["payload"]["batch"]["source_raw_sha256"] for body in bodies}
    assert len(shas) == 1
    # A symbol's own error stays that member's no-data failure.
    assert [(o["member_key"], o["reason"]) for o in source.outcomes] == [
        ("GAP", "mapping_gap"),
        ("NODATA", "source_error"),
    ]
    with runtime.state.connection() as db:
        rows = dict(
            db.execute(
                "SELECT member_key,status FROM full_work WHERE plan_id=?", (plan["plan_id"],)
            ).fetchall()
        )
    assert rows["AAPL"] == rows["IBM"] == "complete" and rows["NODATA"] == "pending"


def test_unserved_batch_entries_expire_with_their_pass(monkeypatch):
    from findb_fetcher.provider_adapters import twelve_data as adapter_module

    adapter = adapter_module.TwelveDataAdapter()
    clock = [100.0]
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: clock[0])
    adapter.batched[("us_equity_eod", date(2026, 10, 1), "IBM")] = (100.0, None, None)
    adapter.client = TwelveDataClient(TwelveDataConfig(api_key="k"))
    clock[0] += adapter_module.BATCH_SERIES_TTL_SECONDS + 1

    class Context:
        dataset_key, trade_date, batch_candidates = "us_equity_eod", date(2026, 10, 1), None

    try:
        assert adapter._batched_series({"provider_symbol": "IBM"}, Context()) is None
        assert adapter.batched == {}
    finally:
        adapter.close()
