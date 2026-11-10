"""Full release cohort historical dates: per-member isolation through the Full adapters."""

from __future__ import annotations

import importlib.metadata
import json
import math
import threading
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import pytest

from findb_fetcher import member_historical
from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.historical_backfill import (
    HistoricalRunResult,
    HistoricalWorkDeferredError,
    HistoricalWorkItem,
)
from findb_fetcher.member_historical import MemberHistoricalRunner
from findb_fetcher.provider_work import WorkJournal
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig

DAY = date(2026, 11, 4)
ITEM = UUID("01982886-b452-7c71-b166-cb74cfd1d002")
REQUEST = UUID("01982886-b452-7c71-b166-cb74cfd1d003")


class _S3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **arguments: Any) -> dict:
        self.puts.append(arguments)
        return {}


class _Delivery:
    def __init__(self, invalid: set[str] = frozenset()) -> None:
        self.prepared: list[dict] = []
        self.job_keys: list[str | None] = []
        self.invalid = invalid

    def prepare(self, request: dict, *, job_key: str | None = None) -> dict:
        self.prepared.append(json.loads(canonical_bytes(request)))
        self.job_keys.append(job_key)
        return request

    def deliver(self, request: dict) -> Any:
        return SimpleNamespace(run_id=UUID(int=len(self.prepared)))

    def get_run_status(self, run_id: UUID, *, expected: dict) -> Any:
        bad = expected["payload"]["data"][0]["symbol"] in self.invalid
        return SimpleNamespace(
            status="completed_with_errors" if bad else "completed",
            total_records=1,
            success_records=int(not bad),
            failed_records=int(bad),
        )


class _Owner:
    dispatch_observations = 0

    def __init__(self, tmp_path, item: HistoricalWorkItem) -> None:
        self.journal = WorkJournal(tmp_path / "work.sqlite3", "historical-work")
        self.work = {"stable_key": f"historical:{item.item_id}", "kind": "historical"}
        self.failed = threading.Event()
        self.frozen: list[dict] = []

    def freeze(self, manifest: dict) -> dict:
        self.frozen.append(manifest)
        self.work["manifest"] = self.journal.freeze(manifest)
        return self.work["manifest"]

    def restore(self, **kwargs: Any) -> list[str]:
        return []


def _member(symbol: str, currency: str, provider_symbol: str | None = None) -> dict:
    return {
        "member_key": symbol,
        "symbol": symbol,
        "provider_symbol": provider_symbol or symbol,
        "exchange": "XNAS",
        "currency": currency,
        "asset_class": "equity",
        "classification": "ordinary",
        "mapping_status": "mapped",
        "raw_evidence_ref": None,
    }


def _item(provider: str, dataset: str, members: list[dict]) -> HistoricalWorkItem:
    return HistoricalWorkItem(
        item_id=ITEM,
        request_id=REQUEST,
        request_key="cohort",
        provider=provider,
        dataset_key=dataset,
        market="US" if provider == "twelve_data" else "TW",
        trade_date=DAY,
        lease_token=UUID(int=7),
        universe_snapshot={
            "version": 2,
            "cohort": "full_release",
            "trade_date": DAY.isoformat(),
            "release_id": "01982886-b452-7c71-b166-cb74cfd1d004",
            "effective_date": DAY.isoformat(),
            "member_sha256": "a" * 64,
            "plan_id": None,
            "members": members,
        },
    )


def _runner(monkeypatch, tmp_path, provider: str, item, delivery=None):
    owner = _Owner(tmp_path, item)
    monkeypatch.setattr(member_historical, "active", lambda: owner)
    s3 = _S3()
    runner = MemberHistoricalRunner(
        provider,
        delivery=delivery or _Delivery(),
        raw_store=R2RawPayloadStore(
            RawStorageConfig(
                account_id="a" * 32,
                bucket="findb-raw-test",
                access_key_id="k",
                secret_access_key="s",
            ),
            client=s3,
        ),
        http=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    return runner, owner, s3


def _twelve(symbol: str, currency: str = "USD", kind: str = "REIT") -> dict:
    return {
        "status": "ok",
        "meta": {
            "symbol": symbol,
            "type": kind,
            "currency": currency,
            "exchange": "NYSE",
            "mic_code": "XNYS",
            "interval": "1day",
            "exchange_timezone": "America/New_York",
        },
        "values": [
            {
                "datetime": DAY.isoformat(),
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10.5",
                "volume": "100",
            }
        ],
    }


def test_twelve_data_member_failures_stay_member_scoped_and_replay(monkeypatch, tmp_path):
    from findb_fetcher.providers.twelve_data import (
        TwelveDataClient,
        TwelveDataConfig,
        TwelveDataResponse,
        TwelveDataResponseError,
    )

    monkeypatch.setattr(TwelveDataConfig, "from_env", classmethod(lambda cls: cls(api_key="k")))
    calls: list[str] = []
    limited = {"LIMIT": 1}

    def fetch_daily(self, symbol, *, start_date, end_date, **kwargs):
        assert start_date == end_date == DAY and "exchange" not in kwargs
        calls.append(symbol)
        if symbol == "NODATA":
            raise TwelveDataResponseError("absent", status_code=404, dispatched=True)
        if symbol == "LIMIT" and limited["LIMIT"]:
            limited["LIMIT"] -= 1
            raise TwelveDataResponseError("limited", status_code=429, dispatched=True)
        payload = _twelve(symbol, "HKD" if symbol == "BADCCY" else "USD")
        return TwelveDataResponse(payload, raw_bytes=canonical_bytes(payload))

    monkeypatch.setattr(TwelveDataClient, "fetch_daily", fetch_daily)
    members = [
        _member("O", "USD"),
        _member("NODATA", "USD"),
        _member("BADCCY", "USD"),
        _member("LIMIT", "USD"),
    ]
    item = _item("twelve_data", "us_equity_eod", members)
    delivery = _Delivery()
    runner, owner, _ = _runner(monkeypatch, tmp_path, "twelve_data", item, delivery)
    try:
        # A rate limit retries the date; the members already settled are not refetched.
        with pytest.raises(HistoricalWorkDeferredError):
            runner.run(item)
        assert calls == ["BADCCY", "LIMIT"]
        result = runner.run(item)
    finally:
        runner.close()
    assert calls == ["BADCCY", "LIMIT", "LIMIT", "NODATA", "O"]
    assert isinstance(result, HistoricalRunResult) and result.status == "partial"
    assert result.run_id is None
    assert {row["member_key"]: row["outcome"] for row in result.result_evidence["members"]} == {
        "BADCCY": "invalid",
        "LIMIT": "data",
        "NODATA": "missing",
        "O": "data",
    }
    evidence = {row["member_key"]: row for row in result.result_evidence["members"]}
    # An identity mismatch cites its own raw response; a provider 404 has none.
    assert "/twelve_data/us_equity_eod/" in evidence["BADCCY"]["raw"]["ref"]
    assert "raw" not in evidence["NODATA"]
    assert result.result_evidence["schema_version"] == 1
    assert owner.frozen[0]["contract"] == {"schema_id": "market_eod", "schema_version": 1}
    assert [m["key"] for m in owner.frozen[0]["members"]] == ["BADCCY", "LIMIT", "NODATA", "O"]
    # REIT members deliver as in Full daily, one backfill body per member.
    assert [body["payload"]["data"][0]["symbol"] for body in delivery.prepared] == ["LIMIT", "O"]
    for body in delivery.prepared:
        batch = body["payload"]["batch"]
        assert batch["delivery_mode"] == "backfill"
        assert batch["coverage_start_date"] == batch["coverage_end_date"] == DAY.isoformat()
        assert body["delivery"]["work_item_id"] == f"historical:{ITEM}"
        assert batch["source_raw_ref"].startswith(
            "r2://" + "a" * 32 + "/findb-raw-test/twelve_data/"
        )
        assert body["idempotency_key"].startswith("twelve_data:")
    assert delivery.job_keys == ["historical-member:LIMIT", "historical-member:O"]


def _finlab_table(symbols, values):
    from findb_fetcher.providers.finlab import FinLabDatasetTable

    return FinLabDatasetTable((DAY.isoformat(),), symbols, (values,))


@pytest.mark.parametrize(
    ("dataset", "asset_class"), [("tw_equity_eod", "equity"), ("tw_etf_eod", "etf")]
)
def test_finlab_downloads_five_tables_once_and_isolates_members(
    monkeypatch, tmp_path, dataset, asset_class
):
    from findb_fetcher.providers.finlab import FinLabSdkGateway

    real = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata, "version", lambda name: "1.5.7" if name == "finlab" else real(name)
    )
    served = ("2330", "1101", "2317")
    prices = {
        "open": ("580", math.nan, "100"),
        "high": ("590", math.nan, "90"),
        "low": ("575", math.nan, "95"),
        "close": ("588", math.nan, "99"),
        "volume": ("1000", 0, "10"),
    }
    names = dict(
        zip(
            ("price:開盤價", "price:最高價", "price:最低價", "price:收盤價", "price:成交股數"),
            prices,
            strict=True,
        )
    )
    fetched: list[str] = []

    class Gateway:
        def fetch_dataset(self, name, *, target_date, symbols):
            assert target_date == DAY and tuple(symbols) == ("1101", "2317", "2330", "9999")
            fetched.append(name)
            return _finlab_table(served, prices[names[name]])

    monkeypatch.setattr(FinLabSdkGateway, "from_env", classmethod(lambda cls, **kw: Gateway()))
    members = [
        {**_member(symbol, "TWD"), "exchange": "XTAI", "asset_class": asset_class}
        for symbol in ("1101", "2317", "2330", "9999")
    ]
    item = _item("finlab", dataset, members)
    runner, owner, s3 = _runner(monkeypatch, tmp_path, "finlab", item)
    try:
        result = runner.run(item)
    finally:
        runner.close()
    assert len(fetched) == 5
    outcomes = {row["member_key"]: row for row in result.result_evidence["members"]}
    assert {key: row["outcome"] for key, row in outcomes.items()} == {
        "1101": "no_data",
        "2317": "invalid",
        "2330": "data",
        "9999": "missing",
    }
    bundle = next(put for put in s3.puts if put["Metadata"]["source-symbol"] == "dataset_bundle")
    assert outcomes["1101"]["raw"]["ref"].endswith(bundle["Metadata"]["sha256"] + ".json")
    assert outcomes["9999"]["reason"] == "no_source_row"
    assert outcomes["9999"]["raw"] == outcomes["2317"]["raw"] == outcomes["1101"]["raw"]
    assert result.status == "partial"


def test_finlab_unpublished_date_retries_the_date_within_one_shared_budget(monkeypatch, tmp_path):
    from findb_fetcher.providers.finlab import FinLabSdkGateway, FinLabTargetNotReadyError

    real = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata, "version", lambda name: "1.5.7" if name == "finlab" else real(name)
    )
    attempts = []

    class Gateway:
        def fetch_dataset(self, name, *, target_date, symbols):
            attempts.append(name)
            raise FinLabTargetNotReadyError("not yet")

    monkeypatch.setattr(FinLabSdkGateway, "from_env", classmethod(lambda cls, **kw: Gateway()))
    members = [{**_member(s, "TWD"), "exchange": "XTAI"} for s in ("1101", "2330")]
    item = _item("finlab", "tw_equity_eod", members)
    runner, owner, _ = _runner(monkeypatch, tmp_path, "finlab", item)
    try:
        for _ in range(member_historical.MEMBER_ATTEMPTS):
            with pytest.raises(HistoricalWorkDeferredError):
                runner.run(item)
        # The date budget, not each member's, is spent; exhaustion fails the date.
        with pytest.raises(ValueError):
            runner.run(item)
    finally:
        runner.close()
    assert len(attempts) == member_historical.MEMBER_ATTEMPTS


@pytest.mark.parametrize("provider", ["twelve_data", "finlab"])
def test_requests_without_a_cohort_keep_the_legacy_executor(monkeypatch, provider):
    from findb_fetcher import historical_runtime

    class LegacyPathError(Exception):
        pass

    def legacy(*args, **kwargs):
        raise LegacyPathError

    monkeypatch.setattr(historical_runtime, "require_owner", lambda provider: None)
    monkeypatch.setattr(historical_runtime, "historical_execution", legacy)
    monkeypatch.setattr(
        historical_runtime.MemberHistoricalRunner,
        "from_env",
        classmethod(lambda cls, provider: pytest.fail("legacy request used the cohort runner")),
    )
    runner = (
        historical_runtime.TwelveDataHistoricalRunner
        if provider == "twelve_data"
        else historical_runtime.FinLabHistoricalRunner
    )()
    item = _item(provider, "us_equity_eod" if provider == "twelve_data" else "tw_equity_eod", [])
    with pytest.raises(LegacyPathError):
        runner.run(replace(item, universe_snapshot=None))
    runner.close()


def test_cohort_items_complete_with_member_evidence(monkeypatch):
    from findb_fetcher.historical_backfill import _member_evidence

    item = _item("twelve_data", "us_equity_eod", [_member("O", "USD")])
    assert _member_evidence(item)
    assert not _member_evidence(SimpleNamespace(provider="twelve_data", universe_snapshot=None))
