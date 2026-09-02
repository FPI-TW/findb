from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest

import findb_fetcher.historical_runtime as runtime
import findb_fetcher.shioaji_historical as shioaji_historical
from findb_fetcher.client import PreparedDelivery
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.historical_backfill import HistoricalWorkItem
from findb_fetcher.providers.finlab import FinLabDatasetTable
from findb_fetcher.providers.twelve_data import TwelveDataResponse
from findb_fetcher.raw_storage import RawObject
from findb_fetcher.schedule import load_schedule_manifest
from findb_fetcher.shioaji_scheduler import EXPECTED as SHIOAJI_EXPECTED
from findb_fetcher.shioaji_scheduler import GOVERNANCE, PILOT_UNIVERSE_ID, ProductionCoordinator
from findb_fetcher.shioaji_staging_state import ShioajiStagingState
from findb_fetcher.universe import load_symbol_universe

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
SCHEDULE_PATH = CONFIG_DIR / "daily_scheduler.v2.json"
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
    schedule = next(
        feed
        for feed in load_schedule_manifest(SCHEDULE_PATH).feeds
        if feed.provider == "twelve_data"
    )
    universe = load_symbol_universe(TWELVE_UNIVERSE_PATH)
    universe = replace(universe, symbols=universe.symbols[:1])
    monkeypatch.setattr(runtime, "_schedule", lambda *_args: schedule)
    monkeypatch.setattr(runtime, "load_symbol_universe", lambda _path: universe)

    class Provider:
        def fetch_daily(self, symbol: str, **_kwargs: object) -> TwelveDataResponse:
            events.append("fetch")
            payload = json.loads(TWELVE_FIXTURE_PATH.read_text())
            payload["meta"]["symbol"] = symbol
            return TwelveDataResponse(payload, raw_bytes=json.dumps(payload).encode())

    provider = Provider()
    monkeypatch.setattr(runtime, "TwelveDataClient", lambda *_args: _Context(provider))
    monkeypatch.setattr(runtime.TwelveDataConfig, "from_env", lambda: object())

    run_id = runtime.TwelveDataHistoricalRunner().run(
        _item("twelve_data", "us_equity_eod", "US", date(2024, 1, 5))
    )

    assert run_id == RUN_ID
    assert events == ["fetch", "raw", "prepare", "deliver", "terminal"]
    assert str(source.requests[0]["idempotency_key"]).startswith("twelve_data:")


def test_finlab_historical_runtime_uses_raw_first_prepared_terminal_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source = _Source(events, record_count=2)
    raw_store = _RawStore(events)
    _patch_runtime_config(monkeypatch, source=source, raw_store=raw_store)
    schedule = next(
        feed for feed in load_schedule_manifest(SCHEDULE_PATH).feeds if feed.provider == "finlab"
    )
    universe = load_finlab_universe(FINLAB_UNIVERSE_PATH)
    monkeypatch.setattr(runtime, "_schedule", lambda *_args: schedule)
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


class _ShioajiGateway:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def fetch_kbars(self, _symbol: str, _target: date) -> SimpleNamespace:
        self.events.append("fetch")
        return SimpleNamespace(
            kbars={
                "ts": (1785286860000000000,),
                "Open": (1,),
                "High": (1,),
                "Low": (1,),
                "Close": (1,),
                "Volume": (1,),
                "Amount": (1,),
            }
        )


def _historical_manifest() -> dict[str, object]:
    return {
        "version": 1,
        "universe_id": PILOT_UNIVERSE_ID,
        "governance": GOVERNANCE,
        "sequences": [dict(row) for row in SHIOAJI_EXPECTED],
    }


def _configure_shioaji_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: _Source,
    events: list[str],
) -> None:
    monkeypatch.setenv("FETCHER_HISTORICAL_STATE_DIR", str(tmp_path / "historical"))
    monkeypatch.setattr(shioaji_historical, "SourceAPIClient", lambda *_args: _Context(source))
    monkeypatch.setattr(shioaji_historical, "R2RawPayloadStore", lambda _config: _RawStore(events))
    monkeypatch.setattr(shioaji_historical.RawStorageConfig, "from_env", lambda: object())
    monkeypatch.setattr(
        shioaji_historical,
        "IsolatedShioajiGateway",
        SimpleNamespace(from_env=lambda: _ShioajiGateway(events)),
    )
    monkeypatch.setattr(shioaji_historical, "load_manifest", lambda _path: _historical_manifest())
    monkeypatch.setattr(shioaji_historical, "default_manifest_path", lambda: Path("pilot.json"))

    class AfterCutoffClock:
        @staticmethod
        def now(tz: ZoneInfo) -> datetime:
            return datetime(2026, 7, 29, 17, 1, tzinfo=tz)

    monkeypatch.setattr(shioaji_historical, "datetime", AfterCutoffClock)


def test_shioaji_historical_runtime_bypasses_cutoff_isolates_state_and_requires_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    source = _Source(events, record_count=1)
    _configure_shioaji_runner(monkeypatch, tmp_path, source, events)
    item = _item("shioaji", "tw_etf_minute", "TW", date(2026, 7, 29))

    run_id = shioaji_historical.ExecutableShioajiHistoricalRunner(
        fetcher=cast(FetcherConfig, SimpleNamespace()), contracts=ContractRegistry(CONTRACTS_DIR)
    ).run(item)

    assert run_id == RUN_ID
    assert events == ["fetch", "raw", "prepare", "deliver", "terminal"] * 3
    state_path = tmp_path / "historical" / f"{item.request_id}.sqlite3"
    assert state_path.is_file()

    # The normal scheduler remains cut off and never receives the request-scoped rows.
    recurring_state = ShioajiStagingState(tmp_path / "recurring.sqlite")
    recurring = ProductionCoordinator(
        recurring_state,
        _historical_manifest(),
        _ShioajiGateway([]),
        now=lambda: datetime(2026, 7, 29, 17, 1, tzinfo=ZoneInfo("Asia/Taipei")),
    ).run(item.trade_date, deliver=True)
    assert [result.code for result in recurring] == ["CUTOFF_REACHED"]
    assert recurring_state.db.execute("SELECT count(*) FROM daily_updates").fetchone() == (0,)
    recurring_state.close()


def test_shioaji_historical_runtime_rejects_incomplete_terminal_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    # A completed Source status with the wrong record count is not a completed
    # sequence, so the adapter must fail the control item rather than complete it.
    source = _Source(events, record_count=0)
    _configure_shioaji_runner(monkeypatch, tmp_path, source, events)
    item = _item("shioaji", "tw_etf_minute", "TW", date(2026, 7, 29))

    with pytest.raises(
        shioaji_historical.ShioajiHistoricalBridgeUnavailableError,
        match="sequenced snapshot did not complete",
    ):
        shioaji_historical.ExecutableShioajiHistoricalRunner(
            fetcher=cast(FetcherConfig, SimpleNamespace()),
            contracts=ContractRegistry(CONTRACTS_DIR),
        ).run(item)

    assert events == ["fetch", "raw", "prepare", "deliver", "terminal"] * 3
