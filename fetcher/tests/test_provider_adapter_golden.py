"""Golden Full daily acquisition outputs; provider adapter moves must stay byte-identical.

Each scenario drives the runtime member loop (or the FactSet cohort path) with fixed
native inputs and a fixed clock, then records the exact prepared Source bodies,
idempotency keys, distinct R2 objects, Source outcome calls and member settlement.
Fakes sit at class or transport level so they survive module moves.

Regenerate only for an intended contract change: ``FINDB_UPDATE_GOLDEN=1 pytest``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig

GOLDEN = Path(__file__).parent / "fixtures/provider_adapter_golden.json"
FIXED = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)
DAY = date(2026, 10, 1)


class _S3:
    """Records exact conditional R2 uploads without network."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **arguments: Any) -> dict[str, Any]:
        self.puts.append(arguments)
        return {}


def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from findb_fetcher import factset_eod
    from findb_fetcher import full_market_runtime as runtime_module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return FIXED.astimezone(tz) if tz else FIXED.replace(tzinfo=None)

    monkeypatch.setattr(runtime_module, "datetime", Clock)
    monkeypatch.setattr(factset_eod, "datetime", Clock)


def _feed(provider: str, dataset: str) -> dict[str, Any]:
    from execution_config_support import publication

    (feed,) = [feed for feed in publication(provider)["feeds"] if feed["dataset_key"] == dataset]
    from findb_fetcher.execution_metadata import acquisition_feeds

    (installed,) = [f for f in acquisition_feeds(provider) if f["dataset_key"] == dataset]
    return {
        **feed,
        "completion_window_seconds": feed["publication_retry_window_seconds"],
        "slot_id": installed["delivery"]["slot_id"],
    }


def _runtime(tmp_path: Path, provider: str, http: httpx.Client | None = None):
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState

    class Source:
        def __init__(self) -> None:
            self.outcomes: list[dict[str, Any]] = []
            self.evaluated: list[str] = []

        def outcome(self, plan_id: str, **kwargs: Any) -> dict[str, Any]:
            self.outcomes.append({"plan_id": plan_id, **kwargs})
            return {}

        def authorization(self, *args: Any, **kwargs: Any):
            import time

            from findb_fetcher.acquisition import Authorization

            return Authorization(time.monotonic(), time.time(), time.time())

        def evaluate(self, plan_id: str) -> dict[str, Any]:
            self.evaluated.append(plan_id)
            return {
                "plan_id": plan_id,
                "status": "complete",
                "summary": {
                    "expected": 0,
                    "data": 0,
                    "no_data": 0,
                    "missing": 0,
                    "blocked": 0,
                    "is_late": False,
                },
            }

    class Delivery:
        def __init__(self) -> None:
            self.prepared: list[str] = []
            self.job_keys: list[str | None] = []

        def prepare(self, request: dict[str, Any], *, job_key: str | None = None) -> Any:
            self.prepared.append(canonical_bytes(request).decode())
            self.job_keys.append(job_key)
            return request

        def deliver(self, request: dict[str, Any]) -> Any:
            return SimpleNamespace(run_id=UUID(int=len(self.prepared)))

        def get_run_status(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                status="completed", failed_records=0, success_records=1, total_records=1
            )

    s3 = _S3()
    store = R2RawPayloadStore(
        RawStorageConfig(
            account_id="a" * 32,
            bucket="findb-raw-golden",
            access_key_id="golden-key",
            secret_access_key="golden-secret",
        ),
        client=s3,
    )
    source, delivery = Source(), Delivery()
    runtime = FullMarketRuntime(
        provider=provider,
        config={},
        state=FullMarketState(tmp_path / "state.sqlite3"),
        source=source,  # type: ignore[arg-type]
        delivery=delivery,  # type: ignore[arg-type]
        raw_store=store,
        client=http
        or httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    runtime.proof = {
        "requests_per_day": 10000,
        "requests_per_minute": 2000,
        "requests_per_second": 10,
        "bytes_per_day": 100000000,
        "max_response_bytes": 100000,
    }
    return runtime, source, delivery, s3


def _plan(dataset: str, members: list[str]) -> dict[str, Any]:
    return {
        "plan_id": f"plan:{dataset}",
        "dataset_key": dataset,
        "trade_date": DAY.isoformat(),
        "parts": [{"work_item_id": f"fp1:{dataset}", "member_keys": members}],
    }


def _capture(runtime: Any, source: Any, delivery: Any, s3: _S3, plan: dict) -> dict[str, Any]:
    with runtime.state.connection() as db:
        rows = db.execute(
            "SELECT key,member_key,status,reason FROM full_work WHERE plan_id=? ORDER BY key",
            (plan["plan_id"],),
        ).fetchall()
    prepared = {
        row["key"]: runtime.state.prepared(row["key"]).decode()
        for row in rows
        if runtime.state.prepared(row["key"]) is not None
    }
    objects: list[dict[str, Any]] = []
    for put in s3.puts:
        entry = {
            "key": put["Key"],
            "content_type": put["ContentType"],
            "metadata": put["Metadata"],
            "sha256": hashlib.sha256(put["Body"]).hexdigest(),
            "size": len(put["Body"]),
        }
        if entry not in objects:
            objects.append(entry)
    return {
        "prepared": prepared,
        "delivered": delivery.prepared,
        "job_keys": delivery.job_keys,
        "idempotency_keys": [json.loads(body)["idempotency_key"] for body in delivery.prepared],
        "raw_objects": objects,
        "outcomes": source.outcomes,
        "settlement": [
            {"member_key": row["member_key"], "status": row["status"], "reason": row["reason"]}
            for row in rows
        ],
    }


def _check(name: str, actual: dict[str, Any]) -> None:
    actual = json.loads(json.dumps(actual, sort_keys=True))
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.getenv("FINDB_UPDATE_GOLDEN") == "1":
        golden[name] = actual
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    assert actual == golden[name]


def _twelve_payload(symbol: str, close: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "meta": {
            "symbol": symbol,
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
                "open": "10",
                "high": "12.5",
                "low": "9.75",
                "close": close,
                "volume": "1234",
            }
        ],
    }


def _us(symbol: str) -> dict[str, Any]:
    return {
        "member_key": symbol,
        "symbol": symbol,
        "provider_symbol": symbol,
        "exchange": "XNAS",
        "currency": "USD",
        "mapping_status": "mapped",
    }


def test_twelve_data_member_acquisition_is_byte_identical(tmp_path, monkeypatch):
    from findb_fetcher.providers import twelve_data
    from findb_fetcher.providers.twelve_data import (
        TwelveDataClient,
        TwelveDataConfig,
        TwelveDataResponse,
        TwelveDataResponseError,
    )

    _clock(monkeypatch)
    monkeypatch.setattr(
        TwelveDataConfig, "from_env", classmethod(lambda cls: cls(api_key="golden-key"))
    )
    calls: list[tuple[str, str, str, int]] = []

    def fetch_daily(self, symbol, *, start_date, end_date, max_response_bytes, **kwargs):
        calls.append((symbol, str(start_date), str(end_date), max_response_bytes))
        if symbol == "NODATA":
            raise TwelveDataResponseError("absent", status_code=404, dispatched=True)
        if symbol == "LIMIT":
            raise TwelveDataResponseError("limited", status_code=429, dispatched=True)
        payload = _twelve_payload(symbol, {"AAPL": "11.25", "IBM": "12"}[symbol])
        return TwelveDataResponse(payload, raw_bytes=canonical_bytes(payload))

    monkeypatch.setattr(TwelveDataClient, "fetch_daily", fetch_daily)
    assert twelve_data.TwelveDataClient is TwelveDataClient
    runtime, source, delivery, s3 = _runtime(tmp_path, "twelve_data")
    feed = _feed("twelve_data", "us_equity_eod")
    keys = ["AAPL", "NODATA", "IBM", "LIMIT", "GAP"]
    plan = _plan("us_equity_eod", keys)
    release = {
        "release_id": "r",
        "members": [
            *(_us(key) for key in keys[:-1]),
            {**_us("GAP"), "mapping_status": "gap"},
        ],
    }
    runtime.state.record_plan(plan, "twelve_data")
    try:
        assert runtime._deliver_plan(plan, release, feed) is False
        _check(
            "twelve_data",
            {"calls": calls, **_capture(runtime, source, delivery, s3, plan)},
        )
    finally:
        runtime.close()


def _finlab_table(symbols: tuple[str, ...], values: tuple[object, ...]) -> Any:
    from findb_fetcher.providers.finlab import FinLabDatasetTable

    return FinLabDatasetTable(dates=(DAY.isoformat(),), symbols=symbols, values=(values,))


@pytest.mark.parametrize(
    ("dataset", "asset_class"), [("tw_equity_eod", "equity"), ("tw_etf_eod", "etf")]
)
def test_finlab_member_acquisition_is_byte_identical(tmp_path, monkeypatch, dataset, asset_class):
    import importlib.metadata

    from findb_fetcher.providers.finlab import FinLabSdkGateway

    _clock(monkeypatch)
    real_version = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: "1.5.7" if name == "finlab" else real_version(name),
    )
    # The table serves the mapped members except "9999"; "1101" did not trade.
    served = ("2330", "1101", "0050")
    prices = {
        "open": ("580", math.nan, "130.5"),
        "high": ("590", math.nan, "131"),
        "low": ("575.5", math.nan, "129.95"),
        "close": ("588", math.nan, "130.8"),
        "volume": ("25000000", 0, "8000000"),
    }
    names = {
        "price:開盤價": "open",
        "price:最高價": "high",
        "price:最低價": "low",
        "price:收盤價": "close",
        "price:成交股數": "volume",
    }
    requested: list[tuple[str, str, tuple[str, ...]]] = []

    class Gateway:
        def fetch_dataset(self, name, *, target_date, symbols):
            requested.append((name, str(target_date), tuple(symbols)))
            return _finlab_table(served, prices[names[name]])

    monkeypatch.setattr(FinLabSdkGateway, "from_env", classmethod(lambda cls, **kwargs: Gateway()))
    runtime, source, delivery, s3 = _runtime(tmp_path, "finlab")
    feed = _feed("finlab", dataset)
    keys = ["2330", "1101", "9999", "0050", "GAP1"]
    plan = _plan(dataset, keys)

    def member(symbol: str, status: str = "mapped") -> dict[str, Any]:
        return {
            "member_key": symbol,
            "symbol": symbol,
            "provider_symbol": symbol,
            "exchange": "XTAI",
            "currency": "TWD",
            "mapping_status": status,
        }

    release = {
        "release_id": "r",
        "members": [*(member(key) for key in keys[:-1]), member("GAP1", "gap")],
    }
    runtime.state.record_plan(plan, "finlab")
    try:
        runtime._deliver_plan(plan, release, feed)
        _check(
            f"finlab:{dataset}",
            {"requested": requested, **_capture(runtime, source, delivery, s3, plan)},
        )
    finally:
        runtime.close()


def _taifex_row(month: str, session: str, **values: str) -> dict[str, str]:
    return {
        "Date": DAY.strftime("%Y%m%d"),
        "Contract": "TX",
        "ContractMonth(Week)": month,
        "Open": "22000",
        "High": "22100",
        "Low": "21900",
        "Last": "22050",
        "Volume": "37",
        "SettlementPrice": "22040",
        "OpenInterest": "1200",
        "TradingSession": session,
        **values,
    }


def test_taifex_member_acquisition_is_byte_identical(tmp_path, monkeypatch):
    from findb_fetcher.providers.taifex import parse_report, report_members

    _clock(monkeypatch)
    no_trade = {
        field: "NULL"
        for field in ("Open", "High", "Low", "Last", "SettlementPrice", "OpenInterest")
    }
    report = canonical_bytes(
        [
            _taifex_row("202610", "一般"),
            _taifex_row("202610", "盤後", Volume="0", **no_trade),
            _taifex_row("202611", "一般", Volume="5"),
        ]
    )
    calls: list[str] = []

    def native(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        return httpx.Response(200, content=report)

    runtime, source, delivery, s3 = _runtime(
        tmp_path, "taifex", httpx.Client(transport=httpx.MockTransport(native))
    )
    feed = _feed("taifex", "tw_futures_eod")
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
    # A frozen member the report no longer lists ends the round as a source error.
    members.append(
        {
            **members[0],
            "contract_code": "TX:202612",
            "provider_symbol": "TX:202612",
            "member_key": "TX:202612:regular",
        }
    )
    keys = [member["member_key"] for member in members]
    plan = _plan("tw_futures_eod", keys)
    runtime.state.record_plan(plan, "taifex")
    try:
        assert runtime._deliver_plan(plan, {"release_id": "r", "members": members}, feed) is False
        _check(
            "taifex",
            {"calls": calls, **_capture(runtime, source, delivery, s3, plan)},
        )
    finally:
        runtime.close()


def test_factset_cohort_acquisition_is_byte_identical(tmp_path, monkeypatch):
    from findb_fetcher import factset_eod, provider_work
    from findb_fetcher.provider_work import WorkJournal
    from findb_fetcher.providers.factset import FactSetConfig, FactSetGateway, encode_native

    _clock(monkeypatch)
    monkeypatch.setattr(FactSetConfig, "from_env", classmethod(lambda cls: "golden-config"))
    monkeypatch.setattr(FactSetGateway, "__init__", lambda self, config: None)
    queried: list[tuple[str, tuple[str, ...]]] = []

    def prices(self, trade_date, listing_ids):
        queried.append((str(trade_date), tuple(listing_ids)))
        rows = [
            {
                "fsym_id": "AAAAA1-L",
                "price_date": trade_date.isoformat(),
                "currency": "HKD",
                "price": 10.5,
                "price_open": 10,
                "price_high": 11,
                "price_low": 9.5,
                "volume": 1000,
            },
            {
                "fsym_id": "AAAAA2-L",
                "price_date": trade_date.isoformat(),
                "currency": "USD",
                "price": 20,
                "price_open": 20,
                "price_high": 21,
                "price_low": 19,
                "volume": 10,
            },
        ]
        return encode_native(rows, scope={"trade_date": trade_date, "listing_ids": listing_ids})

    monkeypatch.setattr(FactSetGateway, "prices", prices)
    runtime, source, delivery, s3 = _runtime(tmp_path, "factset")
    feed = _feed("factset", "hk_equity_eod")
    settled: list[tuple[str, dict]] = []

    class Owner:
        provider = "factset"
        dispatch_observations = 0
        failed = threading.Event()

        def __init__(self) -> None:
            self.journal = WorkJournal(tmp_path / "work.sqlite3", "golden-work")
            self.work = {
                "work_id": "golden-work",
                "kind": "daily",
                "state": "open",
                "trade_date": DAY.isoformat(),
                "created_at": (FIXED - timedelta(days=1)).isoformat(),
            }

        def freeze(self, manifest):
            self.work["manifest"] = self.journal.freeze(manifest)
            return self.work["manifest"]

        def request(self, action, **body):
            assert action == "calendar-status"
            return {
                "work_id": "golden-work",
                "trade_date": DAY.isoformat(),
                "repair_required": False,
            }

        def restore(self, **kwargs):
            return []

        def reserve(self, request, **kwargs):
            return None

        def settle(self, outcome, **kwargs):
            settled.append((outcome, kwargs))

    owner = Owner()
    monkeypatch.setattr(provider_work, "active", lambda: owner)
    monkeypatch.setattr(factset_eod, "active", lambda: owner)
    members = [
        {
            "member_key": key,
            "symbol": key,
            "provider_symbol": listing,
            "currency": "HKD",
            "mapping_status": status,
        }
        for key, listing, status in (
            ("00001", "AAAAA1-L", "mapped"),
            ("00002", "AAAAA2-L", "mapped"),
            ("00003", "AAAAA3-L", "mapped"),
            ("00004", "AAAAA4-L", "gap"),
        )
    ]
    plan = _plan("hk_equity_eod", [member["member_key"] for member in members])
    runtime.state.record_plan(plan, "factset")
    try:
        runtime._deliver_batch_plan(owner, plan, {"members": members}, feed)
        _check(
            "factset",
            {
                "queried": queried,
                "manifest": owner.work["manifest"],
                "settled": settled,
                "evaluated": source.evaluated,
                **_capture(runtime, source, delivery, s3, plan),
            },
        )
    finally:
        runtime.close()


def test_taifex_dated_report_acquisition_is_byte_identical(tmp_path, monkeypatch):
    from urllib.parse import parse_qs

    _clock(monkeypatch)
    stale = canonical_bytes([{**_taifex_row("202610", "一般"), "Date": "20260930"}])
    calls: list[str] = []
    header = (
        "Date,Contract,ContractMonth(Week),Open,High,Low,Last,Volume,"
        "SettlementPrice,OpenInterest,TradingSession\n"
    )

    def native(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            calls.append("GET latest")
            return httpx.Response(200, content=stale)
        product = parse_qs(request.content.decode())["commodity_id"][0]
        calls.append("POST " + product)
        rows = f"{DAY:%Y/%m/%d},{product},202610,100,110,90,105,5,105,10,一般\n" + (
            f"{DAY:%Y/%m/%d},{product},202610,-,-,-,-,0,-,-,盤後\n"
        )
        return httpx.Response(200, content=(header + rows).encode("cp950"))

    runtime, source, delivery, s3 = _runtime(
        tmp_path, "taifex", httpx.Client(transport=httpx.MockTransport(native))
    )
    feed = _feed("taifex", "tw_futures_eod")
    members = [
        {
            "member_key": f"{product}:202610:{session}",
            "symbol": f"{product}:202610",
            "provider_symbol": f"{product}:202610",
            "contract_code": f"{product}:202610",
            "session": session,
            "mapping_status": "mapped",
        }
        for product in ("TX", "MTX")
        for session in ("regular", "after_hours")
    ]
    plan = _plan("tw_futures_eod", [member["member_key"] for member in members])
    runtime.state.record_plan(plan, "taifex")
    try:
        assert runtime._deliver_plan(plan, {"release_id": "r", "members": members}, feed)
        _check(
            "taifex:dated",
            {"calls": calls, **_capture(runtime, source, delivery, s3, plan)},
        )
    finally:
        runtime.close()
