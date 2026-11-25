"""One member's identity or report gap blocks only that member, never the round (#462)."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import httpx
import pytest

from findb_fetcher.full_market_universe import canonical_bytes
from test_provider_adapter_golden import (
    DAY,
    _clock,
    _feed,
    _plan,
    _runtime,
    _taifex_row,
    _twelve_payload,
    _us,
)


def _rows(runtime, plan_id: str) -> tuple[dict, list[str]]:
    with runtime.state.connection() as db:
        rows = {
            row["member_key"]: (row["status"], row["reason"])
            for row in db.execute(
                "SELECT member_key,status,reason FROM full_work WHERE plan_id=?", (plan_id,)
            )
        }
        diagnostics = sorted(
            row["category"] for row in db.execute("SELECT category FROM full_work_diagnostic")
        )
    return rows, diagnostics


def test_twelve_data_identity_mismatch_blocks_only_that_member(tmp_path, monkeypatch):
    from findb_fetcher.providers.twelve_data import (
        TwelveDataClient,
        TwelveDataConfig,
        TwelveDataResponse,
    )

    _clock(monkeypatch)
    monkeypatch.setattr(TwelveDataConfig, "from_env", classmethod(lambda cls: cls(api_key="k")))
    calls: list[str] = []

    def fetch_daily(self, symbol, **kwargs):
        calls.append(symbol)
        payload = _twelve_payload(symbol, "11")
        if symbol == "OTHER":
            # The served series belongs to a listing in another currency.
            payload["meta"]["currency"] = "HKD"
        return TwelveDataResponse(payload, raw_bytes=canonical_bytes(payload))

    monkeypatch.setattr(TwelveDataClient, "fetch_daily", fetch_daily)
    # The one-symbol path stays pinned; shared batches have their own tests.
    from findb_fetcher.provider_adapters.twelve_data import TwelveDataAdapter

    monkeypatch.setattr(TwelveDataAdapter, "member_batch_limit", 0)
    runtime, source, delivery, _ = _runtime(tmp_path, "twelve_data")
    keys = ["AAPL", "OTHER", "IBM"]
    plan = _plan("us_equity_eod", keys)
    runtime.state.record_plan(plan, "twelve_data")
    try:
        assert runtime._deliver_plan(
            plan,
            {"release_id": "r", "members": [_us(key) for key in keys]},
            _feed("twelve_data", "us_equity_eod"),
        )
        rows, diagnostics = _rows(runtime, plan["plan_id"])
    finally:
        runtime.close()
    # The round continues: every member is acquired once and the others delivered.
    assert calls == keys
    assert [json.loads(body)["payload"]["data"][0]["symbol"] for body in delivery.prepared] == [
        "AAPL",
        "IBM",
    ]
    assert [(o["member_key"], o["reason"]) for o in source.outcomes] == [("OTHER", "source_error")]
    assert rows == {
        "AAPL": ("complete", None),
        "OTHER": ("manual", "member_unavailable"),
        "IBM": ("complete", None),
    }
    assert diagnostics == ["member:identity_mismatch"]
    assert not runtime.state.has_blocking_manual(plan["plan_id"])
    assert not runtime.state.pending_source_error_keys(plan["plan_id"])


@pytest.mark.parametrize("batched", [False, True])
def test_twelve_data_ohlc_violation_blocks_only_that_member(tmp_path, monkeypatch, batched):
    """A close above the high (LANV on 2026-11-20) once ended every round until the plan failed."""
    from findb_fetcher.provider_adapters.twelve_data import TwelveDataAdapter
    from findb_fetcher.providers.twelve_data import (
        TwelveDataClient,
        TwelveDataConfig,
        TwelveDataResponse,
    )

    _clock(monkeypatch)
    monkeypatch.setattr(TwelveDataConfig, "from_env", classmethod(lambda cls: cls(api_key="k")))
    calls: list[tuple[str, ...]] = []

    def payload(symbol):
        # Above the 12.5 high.
        return _twelve_payload(symbol, "13" if symbol == "BAD" else "11")

    def fetch_daily(self, symbol, **kwargs):
        calls.append((symbol,))
        served = payload(symbol)
        return TwelveDataResponse(served, raw_bytes=canonical_bytes(served))

    def fetch_daily_batch(self, symbols, **kwargs):
        calls.append(symbols)
        served = {symbol: payload(symbol) for symbol in symbols}
        return TwelveDataResponse(served, raw_bytes=canonical_bytes(served))

    monkeypatch.setattr(TwelveDataClient, "fetch_daily", fetch_daily)
    monkeypatch.setattr(TwelveDataClient, "fetch_daily_batch", fetch_daily_batch)
    if not batched:
        monkeypatch.setattr(TwelveDataAdapter, "member_batch_limit", 0)
    runtime, source, delivery, _ = _runtime(tmp_path, "twelve_data")
    keys = ["BAD", "AAPL", "IBM"]
    plan = _plan("us_equity_eod", keys)
    runtime.state.record_plan(plan, "twelve_data")
    try:
        assert runtime._deliver_plan(
            plan,
            {"release_id": "r", "members": [_us(key) for key in keys]},
            _feed("twelve_data", "us_equity_eod"),
        )
        rows, diagnostics = _rows(runtime, plan["plan_id"])
    finally:
        runtime.close()
    assert calls == ([tuple(keys)] if batched else [(key,) for key in keys])
    assert [json.loads(body)["payload"]["data"][0]["symbol"] for body in delivery.prepared] == [
        "AAPL",
        "IBM",
    ]
    assert [(o["member_key"], o["reason"]) for o in source.outcomes] == [("BAD", "source_error")]
    assert rows == {
        "BAD": ("manual", "member_unavailable"),
        "AAPL": ("complete", None),
        "IBM": ("complete", None),
    }
    assert diagnostics == ["member:ohlc_bounds"]
    assert not runtime.state.has_blocking_manual(plan["plan_id"])
    assert not runtime.state.pending_source_error_keys(plan["plan_id"])


def _sqlite_busy(tmp_path) -> sqlite3.OperationalError:
    """The error another consumer's write lock raises once the busy wait runs out."""
    path = tmp_path / "busy.sqlite3"
    holder = sqlite3.connect(path)
    holder.execute("CREATE TABLE t(x)")
    holder.execute("BEGIN IMMEDIATE")
    waiter = sqlite3.connect(path, timeout=0)
    try:
        waiter.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        waiter.close()
        holder.close()
    raise AssertionError("the write lock was not held")


def test_local_sqlite_lock_timeout_defers_only_that_member(tmp_path, monkeypatch):
    """FinLab 1-minute bars on 2026-11-24 ended a round on a shared journal lock."""
    from findb_fetcher.provider_adapters.twelve_data import TwelveDataAdapter
    from findb_fetcher.providers.twelve_data import (
        TwelveDataClient,
        TwelveDataConfig,
        TwelveDataResponse,
    )

    _clock(monkeypatch)
    monkeypatch.setattr(TwelveDataConfig, "from_env", classmethod(lambda cls: cls(api_key="k")))
    busy = _sqlite_busy(tmp_path)
    calls: list[str] = []

    def fetch_daily(self, symbol, **kwargs):
        calls.append(symbol)
        if symbol == "BUSY":
            raise busy
        payload = _twelve_payload(symbol, "11")
        return TwelveDataResponse(payload, raw_bytes=canonical_bytes(payload))

    monkeypatch.setattr(TwelveDataClient, "fetch_daily", fetch_daily)
    monkeypatch.setattr(TwelveDataAdapter, "member_batch_limit", 0)
    runtime, source, delivery, _ = _runtime(tmp_path, "twelve_data")
    keys = ["BUSY", "AAPL", "IBM"]
    plan = _plan("us_equity_eod", keys)
    runtime.state.record_plan(plan, "twelve_data")
    try:
        runtime._deliver_plan(
            plan,
            {"release_id": "r", "members": [_us(key) for key in keys]},
            _feed("twelve_data", "us_equity_eod"),
        )
        rows, diagnostics = _rows(runtime, plan["plan_id"])
        with runtime.state.connection() as db:
            (attempts,) = db.execute(
                "SELECT attempts FROM full_work WHERE member_key='BUSY'"
            ).fetchone()
    finally:
        runtime.close()
    # The round goes on: the others are delivered after the lock timeout.
    assert calls == keys
    assert [json.loads(body)["payload"]["data"][0]["symbol"] for body in delivery.prepared] == [
        "AAPL",
        "IBM",
    ]
    # A local lock says nothing about the provider or the member: no Source
    # outcome, no charge, and the member waits for a later pass.
    assert source.outcomes == []
    assert rows == {
        "BUSY": ("pending", "local_state_busy"),
        "AAPL": ("complete", None),
        "IBM": ("complete", None),
    }
    assert attempts == 0
    assert diagnostics == ["local_state_busy"]
    assert not runtime.state.pending_source_error_keys(plan["plan_id"])


def _taifex_round(tmp_path, monkeypatch, *, effective):
    from findb_fetcher.providers.taifex import parse_report, report_members

    _clock(monkeypatch)
    quiet = {field: "NULL" for field in ("Open", "High", "Low", "Last", "SettlementPrice")}
    report = canonical_bytes(
        [
            _taifex_row("202610", "一般"),
            # A row the exchange printed without any observation.
            _taifex_row("202611", "一般", Volume="NULL", OpenInterest="NULL", **quiet),
            _taifex_row("202612", "一般"),
        ]
    )
    runtime, source, delivery, _ = _runtime(
        tmp_path,
        "taifex",
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=report))
        ),
    )
    members = []
    for row in parse_report(report, target_date=DAY):
        (listed,) = report_members([row])
        members.append(
            {
                **listed,
                "session": row["session"],
                "member_key": f"{row['contract_code']}:{row['session']}",
                "mapping_status": "mapped",
            }
        )
    # A frozen contract the date's report does not list, between the others.
    absent = {
        **members[0],
        "contract_code": "TX:202701",
        "provider_symbol": "TX:202701",
        "member_key": "TX:202701:regular",
    }
    members = [members[0], absent, members[1], members[2]]
    plan = _plan("tw_futures_eod", [member["member_key"] for member in members])
    runtime.state.record_plan(plan, "taifex")
    release = {"release_id": "r", "effective_date": effective.isoformat(), "members": members}
    return runtime, source, delivery, plan, release


def test_taifex_report_gaps_block_only_their_members_on_a_same_date_universe(tmp_path, monkeypatch):
    runtime, source, delivery, plan, release = _taifex_round(tmp_path, monkeypatch, effective=DAY)
    try:
        assert runtime._deliver_plan(plan, release, _feed("taifex", "tw_futures_eod"))
        rows, diagnostics = _rows(runtime, plan["plan_id"])
    finally:
        runtime.close()
    assert [
        json.loads(body)["payload"]["data"][0]["contract_code"] for body in delivery.prepared
    ] == [
        "TX:202610",
        "TX:202612",
    ]
    assert sorted((o["member_key"], o["reason"]) for o in source.outcomes) == [
        ("TX:202611:regular", "source_error"),
        ("TX:202701:regular", "empty_response"),
    ]
    assert (
        rows["TX:202701:regular"]
        == rows["TX:202611:regular"]
        == (
            "manual",
            "member_unavailable",
        )
    )
    assert rows["TX:202610:regular"] == rows["TX:202612:regular"] == ("complete", None)
    assert diagnostics == ["member:missing_contract", "member:no_observation"]
    assert not runtime.state.has_blocking_manual(plan["plan_id"])


def test_taifex_contract_missing_from_an_older_universe_retries_without_holding_the_round(
    tmp_path, monkeypatch
):
    # Plans frozen before #307 may bind an earlier universe whose contract has rolled.
    runtime, source, delivery, plan, release = _taifex_round(
        tmp_path, monkeypatch, effective=DAY - timedelta(days=1)
    )
    try:
        # The member the older universe still binds keeps its retries; it no
        # longer ends the round, so the later contracts are still delivered.
        assert runtime._deliver_plan(plan, release, _feed("taifex", "tw_futures_eod"))
        rows, diagnostics = _rows(runtime, plan["plan_id"])
    finally:
        runtime.close()
    assert len(delivery.prepared) == 2
    assert source.outcomes == [
        {
            "plan_id": plan["plan_id"],
            "work_item_id": plan["parts"][0]["work_item_id"],
            "member_key": key,
            "reason": "source_error",
        }
        for key in ("TX:202701:regular", "TX:202611:regular")
    ]
    assert rows["TX:202701:regular"] == ("pending", "source_error")
    assert rows["TX:202611:regular"] == ("manual", "member_unavailable")
    assert rows["TX:202610:regular"] == rows["TX:202612:regular"] == ("complete", None)
    assert diagnostics == ["exception:ReadinessBlockedError", "member:no_observation"]
