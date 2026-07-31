from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from findb_fetcher.client import SourceAPIResponseError
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.providers.twelve_data import TwelveDataResponse, TwelveDataResponseError
from findb_fetcher.raw_storage import RawObject, RawStorageUploadError
from findb_fetcher.twelve_data_universe import execute_twelve_data_universe
from findb_fetcher.universe import UniverseLimits, load_symbol_universe

CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "twelve_data_us_common_stocks.v1.json"
)
FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "twelve_data" / "aapl_1day_2024-01-02_2024-01-05.json"
)
ATTEMPT_IDS = [
    UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a91"),
    UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a93"),
    UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a95"),
]
RUN_IDS = [
    UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a92"),
    UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a94"),
    UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a96"),
]


class FakeProvider:
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[str] = []
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def fetch_daily(self, symbol: str, **_kwargs: object) -> TwelveDataResponse:
        self.calls.append(symbol)
        failure = self.failures.get(symbol)
        if failure is not None:
            raise failure
        response = deepcopy(self.fixture)
        response["meta"]["symbol"] = symbol
        return TwelveDataResponse(response, raw_bytes=json.dumps(response).encode())


class RawFakeProvider(FakeProvider):
    def fetch_daily(self, symbol: str, **kwargs: object) -> TwelveDataResponse:
        return super().fetch_daily(symbol, **kwargs)


class SuccessfulRawStore:
    def persist(
        self,
        raw_bytes: bytes,
        *,
        source_symbol: str,
        **_kwargs: object,
    ) -> RawObject:
        return RawObject(
            ref=f"r2://{'a' * 32}/findb-fetcher-raw/{source_symbol}.json",
            sha256="a" * 64,
            size_bytes=len(raw_bytes),
        )


class CapturingRegistry:
    def __init__(self, delegate: ContractRegistry) -> None:
        self.delegate = delegate
        self.requests: list[dict[str, Any]] = []

    def validate(
        self,
        schema_id: str,
        schema_version: int,
        request: dict[str, Any],
    ) -> None:
        self.delegate.validate(schema_id, schema_version, request)
        self.requests.append(deepcopy(request))


class FakeSource:
    def __init__(self, statuses: list[str] | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses = statuses or ["queued", "queued", "queued"]
        self.run_by_id: dict[UUID, int] = {}

    def prepare(self, request: dict[str, Any]) -> SimpleNamespace:
        self.requests.append(deepcopy(request))
        return SimpleNamespace(
            dataset_key=request["dataset_key"],
            schema_id=request["schema_id"],
            schema_version=request["schema_version"],
            request=request,
        )

    def deliver(
        self,
        _prepared: object,
        *,
        deadline: float | None,
        monotonic: Any,
    ) -> SimpleNamespace:
        del deadline, monotonic
        index = len(self.run_by_id)
        self.run_by_id[RUN_IDS[index]] = index
        return SimpleNamespace(
            attempt_id=ATTEMPT_IDS[index],
            run_id=RUN_IDS[index],
            status=self.statuses[index],
        )

    def get_run_status(
        self,
        run_id: UUID,
        *,
        expected: object,
        deadline: float,
        monotonic: Any,
    ) -> SimpleNamespace:
        del expected, deadline, monotonic
        index = self.run_by_id[run_id]
        status = self.statuses[index]
        failed = 1 if status in {"failed", "completed_with_errors"} else 0
        return SimpleNamespace(
            run_id=run_id,
            status=status,
            total_records=4,
            success_records=4 - failed,
            failed_records=failed,
        )


def test_raw_upload_failure_halts_universe_without_delivery(
    contracts_dir: Path,
) -> None:
    class FailedRawStore:
        def persist(self, *_args: object, **_kwargs: object) -> object:
            raise RawStorageUploadError("safe upload failure", retryable=False)

    provider = RawFakeProvider()
    source = FakeSource()
    execution = execute_twelve_data_universe(
        load_symbol_universe(CONFIG_PATH),
        provider=provider,  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        start_date=None,
        end_date=None,
        outputsize=4,
        source=source,  # type: ignore[arg-type]
        raw_store=FailedRawStore(),  # type: ignore[arg-type]
    )

    assert provider.calls == ["AAPL"]
    assert source.requests == []
    assert [result.outcome for result in execution.results] == [
        "raw_upload_failed",
        "not_attempted",
        "not_attempted",
    ]


def test_universe_delivery_rejects_missing_raw_store_before_any_calls(
    contracts_dir: Path,
) -> None:
    provider = FakeProvider()
    source = FakeSource()

    with pytest.raises(ValueError, match="raw object storage"):
        execute_twelve_data_universe(
            load_symbol_universe(CONFIG_PATH),
            provider=provider,  # type: ignore[arg-type]
            registry=ContractRegistry(contracts_dir),
            start_date=None,
            end_date=None,
            outputsize=4,
            source=source,  # type: ignore[arg-type]
        )

    assert provider.calls == []
    assert source.requests == []


def test_universe_delivery_attaches_complete_raw_pair(
    contracts_dir: Path,
) -> None:
    class RawStore:
        def persist(
            self,
            raw_bytes: bytes,
            *,
            source_symbol: str,
            **_kwargs: object,
        ) -> RawObject:
            assert raw_bytes
            return RawObject(
                ref=f"r2://{'a' * 32}/findb-fetcher-raw/{source_symbol}.json",
                sha256="a" * 64,
                size_bytes=len(raw_bytes),
            )

    provider = RawFakeProvider()
    source = FakeSource()
    registry = CapturingRegistry(ContractRegistry(contracts_dir))

    execution = execute_twelve_data_universe(
        load_symbol_universe(CONFIG_PATH),
        provider=provider,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        start_date=None,
        end_date=None,
        outputsize=4,
        source=source,  # type: ignore[arg-type]
        raw_store=RawStore(),
    )

    assert execution.status == "completed"
    assert len(source.requests) == 3
    for request in registry.requests:
        batch = request["payload"]["batch"]
        assert batch["source_raw_ref"].startswith(f"r2://{'a' * 32}/findb-fetcher-raw/")
        assert batch["source_raw_sha256"] == "a" * 64


@pytest.fixture
def universe():
    return load_symbol_universe(CONFIG_PATH)


def test_dry_run_keeps_each_symbol_identity_independent(
    universe,
    contracts_dir: Path,
) -> None:
    provider = FakeProvider()
    registry = CapturingRegistry(ContractRegistry(contracts_dir))

    execution = execute_twelve_data_universe(
        universe,
        provider=provider,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        outputsize=None,
        now=lambda: datetime(2024, 1, 6, 12, tzinfo=timezone.utc),
    )

    assert execution.status == "completed"
    assert execution.credits_used == 3
    assert execution.records_fetched == 12
    assert [result.outcome for result in execution.results] == ["validated"] * 3
    assert provider.calls == ["AAPL", "MSFT", "NVDA"]
    assert len({request["idempotency_key"] for request in registry.requests}) == 3
    assert [request["payload"]["data"][0]["symbol"] for request in registry.requests] == [
        "AAPL",
        "MSFT",
        "NVDA",
    ]


def test_provider_failure_isolated_and_later_symbols_continue(
    universe,
    contracts_dir: Path,
) -> None:
    provider = FakeProvider({"MSFT": TwelveDataResponseError("provider failed safely")})

    execution = execute_twelve_data_universe(
        universe,
        provider=provider,  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        outputsize=None,
    )

    assert execution.status == "partial_failure"
    assert [result.outcome for result in execution.results] == [
        "validated",
        "provider_failed",
        "validated",
    ]
    assert provider.calls == ["AAPL", "MSFT", "NVDA"]
    assert execution.credits_used == 3


@pytest.mark.parametrize(
    "failure",
    [
        TwelveDataResponseError("limited", status_code=429, provider_code=429),
        TwelveDataResponseError("limited", status_code=200, provider_code=429),
    ],
)
def test_provider_rate_limit_stops_remaining_symbols(
    universe,
    contracts_dir: Path,
    failure: TwelveDataResponseError,
) -> None:
    provider = FakeProvider({"MSFT": failure})

    execution = execute_twelve_data_universe(
        universe,
        provider=provider,  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        outputsize=None,
    )

    assert [result.outcome for result in execution.results] == [
        "validated",
        "provider_rate_limited",
        "not_attempted",
    ]
    assert provider.calls == ["AAPL", "MSFT"]
    assert execution.credits_used == 2
    assert execution.skipped_symbols == 1


def test_source_delivery_remains_one_request_per_symbol(
    universe,
    contracts_dir: Path,
) -> None:
    source = FakeSource()

    execution = execute_twelve_data_universe(
        universe,
        provider=FakeProvider(),  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        outputsize=None,
        source=source,  # type: ignore[arg-type]
        raw_store=SuccessfulRawStore(),
    )

    assert execution.status == "completed"
    assert [result.outcome for result in execution.results] == ["accepted"] * 3
    assert [result.run_id for result in execution.results] == RUN_IDS
    assert len({request["idempotency_key"] for request in source.requests}) == 3


def test_terminal_failure_is_partial_and_does_not_pollute_other_symbols(
    universe,
    contracts_dir: Path,
) -> None:
    source = FakeSource(["completed", "failed", "completed"])

    execution = execute_twelve_data_universe(
        universe,
        provider=FakeProvider(),  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        outputsize=None,
        source=source,  # type: ignore[arg-type]
        raw_store=SuccessfulRawStore(),
        wait=True,
        deadline=100.0,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )

    assert execution.status == "partial_failure"
    assert [result.outcome for result in execution.results] == [
        "completed",
        "terminal_failure",
        "completed",
    ]
    assert execution.results[1].source_status == "failed"
    assert execution.results[1].failed_records == 1


@pytest.mark.parametrize("status_code", [401, 403, 429])
def test_systemic_source_rejection_stops_remaining_symbols(
    universe,
    contracts_dir: Path,
    status_code: int,
) -> None:
    class RejectingSource(FakeSource):
        def deliver(
            self,
            _prepared: object,
            *,
            deadline: float | None,
            monotonic: Any,
        ) -> SimpleNamespace:
            del deadline, monotonic
            raise SourceAPIResponseError(status_code)

    execution = execute_twelve_data_universe(
        universe,
        provider=FakeProvider(),  # type: ignore[arg-type]
        registry=ContractRegistry(contracts_dir),
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        outputsize=None,
        source=RejectingSource(),  # type: ignore[arg-type]
        raw_store=SuccessfulRawStore(),
    )

    expected = "source_rate_limited" if status_code == 429 else "source_rejected"
    assert [result.outcome for result in execution.results] == [
        expected,
        "not_attempted",
        "not_attempted",
    ]
    assert execution.credits_used == 1


def test_total_record_limit_halts_before_another_delivery(
    universe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bounded_universe = replace(
        universe,
        limits=UniverseLimits(
            max_symbols_per_run=3,
            max_records_per_symbol=500,
            max_total_records_per_run=780,
            max_date_span_days=366,
            max_credits_per_run=3,
        ),
    )

    def oversized_request(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {
            "dataset_key": "us_equity_eod",
            "schema_id": "market_eod",
            "schema_version": 1,
            "idempotency_key": "stable",
            "payload": {"data": [{}] * 400},
        }

    class NoopRegistry:
        def validate(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(
        "findb_fetcher.twelve_data_universe.build_market_eod_request",
        oversized_request,
    )

    execution = execute_twelve_data_universe(
        bounded_universe,
        provider=FakeProvider(),  # type: ignore[arg-type]
        registry=NoopRegistry(),  # type: ignore[arg-type]
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        outputsize=None,
    )

    assert [result.outcome for result in execution.results] == [
        "validated",
        "total_record_limit_exceeded",
        "not_attempted",
    ]
    assert execution.records_fetched == 400
    assert execution.credits_used == 2
