from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest

import findb_fetcher.historical_runtime as runtime
from findb_fetcher import execution_metadata
from findb_fetcher.client import PreparedDelivery
from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.historical_backfill import HistoricalWorkItem
from findb_fetcher.providers.finlab import FinLabDatasetTable
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
)
from findb_fetcher.raw_storage import RawObject
from findb_fetcher.universe import load_symbol_universe

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
TWELVE_UNIVERSE_PATH = CONFIG_DIR / "twelve_data_us_common_stocks.v1.json"
FINLAB_UNIVERSE_PATH = CONFIG_DIR / "finlab_tw_review_required.v1.json"
TWELVE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "twelve_data" / "aapl_1day_2024-01-02_2024-01-05.json"
)
ATTEMPT_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a91")
RUN_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a92")


class _Context:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self) -> object:
        return self.value

    def __exit__(self, *_args: object) -> None:
        return None


class _RawStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def persist(self, raw_bytes: bytes, **_kwargs: object) -> RawObject:
        self.events.append("raw")
        return RawObject(
            f"r2://{'a' * 32}/findb-fetcher-raw/historical.json",
            hashlib.sha256(raw_bytes).hexdigest(),
            len(raw_bytes),
        )


class _Source:
    def __init__(self, events: list[str], *, record_count: int) -> None:
        self.events = events
        self.record_count = record_count
        self.requests: list[dict[str, object]] = []

    def prepare(self, request: dict[str, object]) -> PreparedDelivery:
        self.events.append("prepare")
        payload = cast(dict[str, Any], request["payload"])
        batch = cast(dict[str, Any], payload["batch"])
        assert str(batch["source_raw_ref"]).startswith("r2://")
        self.requests.append(request)
        return PreparedDelivery(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode(),
            str(request["idempotency_key"]),
            str(request["dataset_key"]),
            str(request["schema_id"]),
            cast(int, request["schema_version"]),
        )

    def deliver(self, _prepared: PreparedDelivery, **_kwargs: object) -> SimpleNamespace:
        self.events.append("deliver")
        return SimpleNamespace(attempt_id=ATTEMPT_ID, run_id=RUN_ID)

    def get_run_status(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        self.events.append("terminal")
        return SimpleNamespace(
            run_id=RUN_ID,
            status="completed",
            total_records=self.record_count,
            success_records=self.record_count,
            failed_records=0,
            failure_code=None,
        )


def _item(provider: str, dataset_key: str, market: str, trade_date: date) -> HistoricalWorkItem:
    return HistoricalWorkItem(
        item_id=UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a93"),
        request_id=UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a94"),
        request_key="hbf:runtime-test",
        provider=provider,
        dataset_key=dataset_key,
        market=market,
        trade_date=trade_date,
        lease_token=UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a95"),
    )


def _patch_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: _Source,
    raw_store: _RawStore,
) -> None:
    config = SimpleNamespace(contracts_dir=CONTRACTS_DIR)
    monkeypatch.setattr(runtime.FetcherConfig, "from_env", lambda: config)
    monkeypatch.setattr(runtime, "SourceAPIClient", lambda *_args: _Context(source))
    monkeypatch.setattr(runtime, "R2RawPayloadStore", lambda _config: raw_store)
    monkeypatch.setattr(runtime.RawStorageConfig, "from_env", lambda: object())


def test_twelve_historical_runtime_uses_raw_first_prepared_terminal_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The historical adapter drives the real executor, not a runner-result mock."""

    events: list[str] = []
    source = _Source(events, record_count=1)
    raw_store = _RawStore(events)
    _patch_runtime_config(monkeypatch, source=source, raw_store=raw_store)
    monkeypatch.setattr(execution_metadata, "CONFIGS_DIR", CONFIG_DIR)
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_HISTORICAL_SCHEDULE_FILE", "/absent/daily.json")
    monkeypatch.setenv("FETCHER_FULL_MARKET_CONFIG", "/absent/full-market.json")
    monkeypatch.setenv("FULL_MARKET_ENABLED", "false")
    universe = load_symbol_universe(TWELVE_UNIVERSE_PATH)
    universe = replace(universe, symbols=universe.symbols[:1])
    monkeypatch.setattr(runtime, "load_symbol_universe", lambda _path: universe)

    def handler(request: httpx.Request) -> httpx.Response:
        events.append("fetch")
        assert request.url.params["start_date"] == "2024-01-05"
        assert request.url.params["end_date"] == "2024-01-06"
        payload = json.loads(TWELVE_FIXTURE_PATH.read_text())
        payload["meta"]["symbol"] = request.url.params["symbol"]
        return httpx.Response(200, json=payload)

    provider = TwelveDataClient(
        TwelveDataConfig(api_key="test-key"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(runtime, "TwelveDataClient", lambda *_args: _Context(provider))
    monkeypatch.setattr(runtime.TwelveDataConfig, "from_env", lambda: object())

    run_id = runtime.TwelveDataHistoricalRunner().run(
        _item("twelve_data", "us_equity_eod", "US", date(2024, 1, 5))
    )

    assert run_id == RUN_ID
    assert events == ["fetch", "raw", "prepare", "deliver", "terminal"]
    assert str(source.requests[0]["idempotency_key"]).startswith("twelve_data:")
    assert source.requests[0]["delivery"]["scheduled_for"] == "2024-01-05T00:00:00Z"
    frozen = {
        "universe_id": universe.universe_id,
        "universe_version": universe.universe_version,
        "members": [
            {"key": m.canonical_symbol, "symbol": m.symbol, "exchange": m.exchange}
            for m in universe.symbols
        ],
    }
    monkeypatch.setattr(runtime, "load_symbol_universe", lambda path: replace(universe, symbols=()))
    _assert_frozen_replay(
        monkeypatch,
        runtime.TwelveDataHistoricalRunner(),
        runtime.TwelveDataScheduledExecutor,
        _item("twelve_data", "us_equity_eod", "US", date(2024, 1, 5)),
        source,
        events,
        frozen,
    )


def test_finlab_historical_runtime_uses_raw_first_prepared_terminal_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source = _Source(events, record_count=2)
    raw_store = _RawStore(events)
    _patch_runtime_config(monkeypatch, source=source, raw_store=raw_store)
    monkeypatch.setattr(execution_metadata, "CONFIGS_DIR", CONFIG_DIR)
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_HISTORICAL_SCHEDULE_FILE", "/absent/daily.json")
    monkeypatch.setenv("FETCHER_FULL_MARKET_CONFIG", "/absent/full-market.json")
    monkeypatch.setenv("FULL_MARKET_ENABLED", "false")
    universe = load_finlab_universe(FINLAB_UNIVERSE_PATH)
    monkeypatch.setattr(runtime, "load_finlab_universe", lambda _path: universe)

    target = date(2026, 7, 29)
    tables = {
        "price:開盤價": FinLabDatasetTable((target.isoformat(),), ("2330", "2317"), ((1000, 200),)),
        "price:最高價": FinLabDatasetTable((target.isoformat(),), ("2330", "2317"), ((1010, 205),)),
        "price:最低價": FinLabDatasetTable((target.isoformat(),), ("2330", "2317"), ((995, 198),)),
        "price:收盤價": FinLabDatasetTable((target.isoformat(),), ("2330", "2317"), ((1005, 202),)),
        "price:成交股數": FinLabDatasetTable(
            (target.isoformat(),), ("2330", "2317"), ((123456, 654321),)
        ),
    }

    class Gateway:
        def fetch_dataset(
            self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
        ) -> FinLabDatasetTable:
            events.append("fetch")
            assert target_date == target and symbols == ("2330", "2317")
            return tables[dataset]

    monkeypatch.setattr(runtime.FinLabSdkGateway, "from_env", lambda: Gateway())

    run_id = runtime.FinLabHistoricalRunner().run(_item("finlab", "tw_equity_eod", "TW", target))

    assert run_id == RUN_ID
    assert events == ["fetch"] * 5 + ["raw", "prepare", "deliver", "terminal"]
    assert str(source.requests[0]["idempotency_key"]).startswith("finlab:")
    assert source.requests[0]["delivery"]["scheduled_for"] == target.isoformat() + "T00:00:00Z"
    frozen = {
        "manifest_id": universe.manifest_id,
        "manifest_version": universe.manifest_version,
        "field_datasets": universe.field_datasets,
        "members": [
            {"key": m.canonical_symbol, "symbol": m.source_symbol} for m in universe.symbols
        ],
    }
    monkeypatch.setattr(runtime, "load_finlab_universe", lambda path: replace(universe, symbols=()))
    _assert_frozen_replay(
        monkeypatch,
        runtime.FinLabHistoricalRunner(),
        runtime.FinLabScheduledExecutor,
        _item("finlab", "tw_equity_eod", "TW", target),
        source,
        events,
        frozen,
    )


def _assert_frozen_replay(monkeypatch, runner, executor_type, item, source, events, frozen):
    """A restarted leased job retains legacy bytes, membership, identity and attempt."""
    request = json.loads(json.dumps(source.requests[0]))
    request["delivery"]["scheduled_for"] = "2026-01-01T22:30:00Z"
    original_bytes = json.dumps(request, sort_keys=True)
    reserved, released, jobs = [], [], []
    owner = SimpleNamespace(
        dispatch_observations=0,
        freeze=lambda proposed: frozen,
        journal=SimpleNamespace(
            prepared=lambda key: json.loads(original_bytes),
            reserve_attempt=lambda key, maximum: reserved.append((key, maximum)) or 4,
            release_wait_attempt=lambda key: released.append(key),
            save_prepared_request=lambda *a, **kw: pytest.fail("prepared bytes rewritten"),
        ),
    )
    monkeypatch.setattr(runtime, "active", lambda: owner)
    execute = executor_type.execute

    def tracked(executor, job, **kwargs):
        jobs.append(job)
        return execute(executor, job, **kwargs)

    monkeypatch.setattr(executor_type, "execute", tracked)
    events.clear()
    assert runner.run(replace(item, lease_token=UUID(int=1234))) == RUN_ID
    assert events == ["prepare", "deliver", "terminal"]
    assert json.dumps(source.requests[-1], sort_keys=True) == original_bytes
    assert jobs[0].attempt_count == 4
    assert jobs[0].job_key == f"historical:{item.request_id}:{item.trade_date}:{jobs[0].symbol}"
    assert jobs[0].schedule_id == f"{item.provider}_{jobs[0].slot_id}_{item.dataset_key}"
    assert reserved == [(jobs[0].job_key, 5)]
    assert released == [jobs[0].job_key]
