"""Bounded per-symbol orchestration for a governed Twelve Data universe."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from findb_fetcher.client import (
    PreparedDelivery,
    SourceAPIClient,
    SourceAPIDeadlineExceeded,
    SourceAPIProtocolError,
    SourceAPIResponseError,
    SourceAPITransportError,
)
from findb_fetcher.contracts import ContractError, ContractRegistry
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataPayloadError,
    TwelveDataResponseError,
    build_market_eod_request,
)
from findb_fetcher.universe import SymbolUniverse, UniverseSymbol

_SUCCESS_OUTCOMES = frozenset({"validated", "accepted", "completed"})
_TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})


@dataclass(frozen=True, slots=True)
class SymbolExecution:
    symbol: str
    outcome: str
    record_count: int = 0
    source_status: str | None = None
    attempt_id: UUID | None = None
    run_id: UUID | None = None
    total_records: int | None = None
    success_records: int | None = None
    failed_records: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome in _SUCCESS_OUTCOMES


@dataclass(frozen=True, slots=True)
class UniverseExecution:
    universe_id: str
    universe_version: int
    dataset_key: str
    credits_budget: int
    credits_used: int
    records_fetched: int
    results: tuple[SymbolExecution, ...]

    @property
    def succeeded_symbols(self) -> int:
        return sum(result.succeeded for result in self.results)

    @property
    def failed_symbols(self) -> int:
        return sum(
            result.outcome not in _SUCCESS_OUTCOMES and result.outcome != "not_attempted"
            for result in self.results
        )

    @property
    def skipped_symbols(self) -> int:
        return sum(result.outcome == "not_attempted" for result in self.results)

    @property
    def status(self) -> str:
        if self.succeeded_symbols == len(self.results):
            return "completed"
        if self.succeeded_symbols:
            return "partial_failure"
        return "failed"


def execute_twelve_data_universe(
    universe: SymbolUniverse,
    *,
    provider: TwelveDataClient,
    registry: ContractRegistry,
    start_date: date | None,
    end_date: date | None,
    outputsize: int | None,
    source: SourceAPIClient | None = None,
    wait: bool = False,
    deadline: float | None = None,
    poll_interval_seconds: float = 2.0,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> UniverseExecution:
    """Execute a deterministic, credit-bounded run with one delivery per symbol."""
    universe.validate_query_bounds(
        start_date=start_date,
        end_date=end_date,
        outputsize=outputsize,
    )
    if wait and (source is None or deadline is None):
        raise ValueError("wait requires a Source client and deadline")

    results: list[SymbolExecution] = []
    credits_used = 0
    records_fetched = 0
    halted = False

    for member in universe.symbols:
        if halted:
            results.append(SymbolExecution(symbol=member.symbol, outcome="not_attempted"))
            continue
        if credits_used + universe.credit_cost_per_symbol > universe.limits.max_credits_per_run:
            results.append(SymbolExecution(symbol=member.symbol, outcome="credit_budget_exceeded"))
            halted = True
            continue
        if deadline is not None and deadline - monotonic() <= 0:
            results.append(SymbolExecution(symbol=member.symbol, outcome="wait_timeout"))
            halted = True
            continue

        credits_used += universe.credit_cost_per_symbol
        try:
            request = _fetch_request(
                member,
                universe=universe,
                provider=provider,
                registry=registry,
                start_date=start_date,
                end_date=end_date,
                outputsize=outputsize,
                fetched_at=now(),
            )
            record_count = len(request["payload"]["data"])
            if record_count > universe.limits.max_records_per_symbol:
                results.append(
                    SymbolExecution(
                        symbol=member.symbol,
                        outcome="record_limit_exceeded",
                        record_count=record_count,
                    )
                )
                halted = True
                continue
            if records_fetched + record_count > universe.limits.max_total_records_per_run:
                results.append(
                    SymbolExecution(
                        symbol=member.symbol,
                        outcome="total_record_limit_exceeded",
                        record_count=record_count,
                    )
                )
                halted = True
                continue
            records_fetched += record_count

            if source is None:
                results.append(
                    SymbolExecution(
                        symbol=member.symbol,
                        outcome="validated",
                        record_count=record_count,
                    )
                )
                continue

            prepared = source.prepare(request)
            receipt = source.deliver(
                prepared,
                deadline=deadline,
                monotonic=monotonic,
            )
            if not wait:
                results.append(
                    SymbolExecution(
                        symbol=member.symbol,
                        outcome="accepted",
                        record_count=record_count,
                        source_status=receipt.status,
                        attempt_id=receipt.attempt_id,
                        run_id=receipt.run_id,
                    )
                )
                continue

            run = _wait_for_terminal(
                source,
                receipt.run_id,
                prepared,
                deadline=deadline,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
            outcome = "completed" if run.status == "completed" else "terminal_failure"
            results.append(
                SymbolExecution(
                    symbol=member.symbol,
                    outcome=outcome,
                    record_count=record_count,
                    source_status=run.status,
                    attempt_id=receipt.attempt_id,
                    run_id=run.run_id,
                    total_records=run.total_records,
                    success_records=run.success_records,
                    failed_records=run.failed_records,
                )
            )
        except TwelveDataResponseError as exc:
            results.append(
                SymbolExecution(
                    symbol=member.symbol,
                    outcome="provider_rate_limited" if exc.is_rate_limited else "provider_failed",
                )
            )
            if exc.is_rate_limited:
                halted = True
        except (TwelveDataPayloadError, ContractError, ValueError):
            results.append(SymbolExecution(symbol=member.symbol, outcome="mapping_failed"))
        except SourceAPIDeadlineExceeded:
            results.append(SymbolExecution(symbol=member.symbol, outcome="wait_timeout"))
            halted = True
        except SourceAPIResponseError as exc:
            outcome = "source_rate_limited" if exc.status_code == 429 else "source_rejected"
            results.append(SymbolExecution(symbol=member.symbol, outcome=outcome))
            if exc.status_code in {401, 403, 429}:
                halted = True
        except (SourceAPIProtocolError, SourceAPITransportError):
            results.append(SymbolExecution(symbol=member.symbol, outcome="delivery_failed"))
            halted = True

    return UniverseExecution(
        universe_id=universe.universe_id,
        universe_version=universe.universe_version,
        dataset_key=universe.dataset_key,
        credits_budget=universe.limits.max_credits_per_run,
        credits_used=credits_used,
        records_fetched=records_fetched,
        results=tuple(results),
    )


def _fetch_request(
    member: UniverseSymbol,
    *,
    universe: SymbolUniverse,
    provider: TwelveDataClient,
    registry: ContractRegistry,
    start_date: date | None,
    end_date: date | None,
    outputsize: int | None,
    fetched_at: datetime,
) -> dict[str, Any]:
    response = provider.fetch_daily(
        member.symbol,
        start_date=start_date,
        end_date=end_date,
        outputsize=outputsize,
        exchange=member.exchange,
    )
    request = build_market_eod_request(
        response,
        dataset_key=universe.dataset_key,
        fetched_at=fetched_at,
        requested_symbol=member.symbol,
        requested_exchange=member.exchange,
        canonical_symbol=member.canonical_symbol,
        allowed_instrument_types=(universe.instrument_type,),
    )
    registry.validate("market_eod", 1, request)
    return request


def _wait_for_terminal(
    source: SourceAPIClient,
    run_id: UUID,
    expected: PreparedDelivery,
    *,
    deadline: float | None,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
):
    if deadline is None:
        raise ValueError("deadline is required")
    while True:
        run = source.get_run_status(
            run_id,
            expected=expected,
            deadline=deadline,
            monotonic=monotonic,
        )
        if run.status in _TERMINAL_STATUSES:
            return run
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise SourceAPIDeadlineExceeded()
        sleep(min(poll_interval_seconds, remaining))
