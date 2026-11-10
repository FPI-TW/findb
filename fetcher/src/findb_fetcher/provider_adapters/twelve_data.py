"""Twelve Data: one native daily series per member."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

from findb_fetcher.full_market_errors import ReadinessBlockedError, retryable_http_status
from findb_fetcher.full_market_universe import TWELVE_DATA_EQUITY_TYPES
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
)
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataResponseError,
)
from findb_fetcher.providers.twelve_data import (
    build_market_eod_request as td_request,
)


class TwelveDataAdapter(ProviderAdapter):
    provider = "twelve_data"

    def __init__(self) -> None:
        self.client: TwelveDataClient | None = None

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        day = context.trade_date
        if self.client is None:
            config = TwelveDataConfig.from_env()
            self.client = TwelveDataClient(
                replace(
                    config,
                    max_response_bytes=min(config.max_response_bytes, context.max_response_bytes),
                )
            )
        try:
            response = self.client.fetch_daily(
                member["provider_symbol"],
                start_date=day,
                end_date=day,
                max_response_bytes=context.max_response_bytes,
                on_http_rate_limited=context.rate_limited,
            )
        except TwelveDataResponseError as exc:
            if exc.is_rate_limited:
                context.rate_limited()
            if exc.observed_bytes or exc.declared_bytes:
                try:
                    context.observe_response(exc.observed_bytes, declared=exc.declared_bytes)
                except ReadinessBlockedError:
                    # Overage debt is retained without erasing known HTTP/API status.
                    raise exc
            raise
        raw = response.raw_bytes
        context.observe_response(len(raw))
        stored = context.persist(raw, member["provider_symbol"])
        request = td_request(
            response,
            dataset_key=context.dataset_key,
            fetched_at=context.fetched_at,
            requested_symbol=member["provider_symbol"],
            canonical_symbol=member["symbol"],
            allowed_instrument_types=tuple(TWELVE_DATA_EQUITY_TYPES),
            after_trade_date=day - timedelta(days=1),
            through_trade_date=day,
            delivery=dict(context.delivery),
        )
        if request["payload"]["data"][0]["currency"] != member["currency"]:
            raise ReadinessBlockedError("provider identity differs from official universe")
        return AcquisitionResult.data(request, stored)

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        if not isinstance(exc, TwelveDataResponseError):
            return PASSTHROUGH
        if phase == "member":
            if exc.is_rate_limited:
                return ProviderFailure(FailureKind.QUOTA_WAIT, reason="rate_limited")
            # A symbol the catalogue lists before its series is served answers 404,
            # so it shares the member-scoped no-data budget; diagnostics keep the class.
            if exc.category in {"no_data", "not_found"}:
                return ProviderFailure(FailureKind.MEMBER, no_data=True)
            return ProviderFailure(FailureKind.ROUND_RETRY)
        retryable = retryable_http_status(exc.status_code)
        return ProviderFailure(
            FailureKind.ROUND_RETRY if retryable else FailureKind.WORK_FAILED,
            http=True,
            status_code=exc.status_code,
        )

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        if not isinstance(exc, TwelveDataResponseError):
            return None
        return {
            "category": exc.category,
            "http_status": exc.status_code,
            "provider_code": exc.provider_code,
            "dispatched": exc.dispatched,
        }
