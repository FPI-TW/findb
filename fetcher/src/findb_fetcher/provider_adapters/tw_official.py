"""The TWSE and TPEx adapters: each exchange publishes its own index.

Each provider has one index dataset with one fixed member, its exchange's index.
A month response serves every day of the month, so it is kept for the round and
read again for the next date; only a response that already holds the target day
is reused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, ClassVar

import httpx

from findb_fetcher.full_market_errors import ReadinessBlockedError, retryable_http_status
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
    UniverseContext,
    UniverseSnapshot,
)
from findb_fetcher.providers.tw_official import (
    MAX_RESPONSE_BYTES,
    TwOfficialFormatError,
    TwOfficialNotPublishedError,
    TwOfficialPayloadError,
    TwOfficialResponseError,
)


class TwOfficialAdapter(ProviderAdapter):
    """One exchange's index; subclasses name the exchange and its endpoint."""

    # The index dataset and its one member, as FinDB and Yahoo spell it.
    index_dataset: ClassVar[str] = ""
    index_symbol: ClassVar[str] = ""
    exchange: ClassVar[str] = ""
    # The only member is read from one response: an outage holds the plan.
    outage_holds_plan = True

    def __init__(self) -> None:
        # (dataset, YYYY-MM) -> the latest month response of this round.
        self._months: dict[tuple[str, str], bytes] = {}

    def reset(self) -> None:
        self._months.clear()

    # --- exchange specifics -------------------------------------------------

    def fetch_index_month(
        self,
        http: httpx.Client,
        *,
        day: date,
        max_response_bytes: int,
        on_http_rate_limited: Callable[[], None] | None,
        on_observed_bytes: Callable[..., None] | None,
    ) -> bytes:
        raise NotImplementedError

    def index_row(self, raw: bytes, day: date) -> dict[str, Any]:
        raise NotImplementedError

    def index_month_url(self, month: date) -> str:
        raise NotImplementedError

    def validate_month(self, raw: bytes, month: date) -> None:
        """Raise unless ``raw`` is a well-formed month response."""
        raise NotImplementedError

    # --- adapter ------------------------------------------------------------

    def _member(self) -> dict[str, Any]:
        return {
            "symbol": self.index_symbol,
            "provider_symbol": self.index_symbol,
            "exchange": self.exchange,
            "currency": "TWD",
            "asset_class": "index",
            "mapping_status": "mapped",
        }

    def build_universe(
        self, feeds: Sequence[Mapping[str, Any]], context: UniverseContext
    ) -> dict[str, UniverseSnapshot]:
        """The exchange's index, evidenced by its current month as the exchange serves it."""
        datasets = [feed["dataset_key"] for feed in feeds]
        if unknown := sorted(set(datasets) - {self.index_dataset}):
            raise ReadinessBlockedError(f"{self.provider} has no universe for {unknown}")
        if context.http is None:
            raise ReadinessBlockedError(f"{self.provider} universe refresh needs the HTTP client")
        groups: dict[str, UniverseSnapshot] = {}
        for dataset in datasets:
            lag = (context.target_date_lags or {}).get(dataset)
            if type(lag) is not int or lag < 0:
                raise ReadinessBlockedError(
                    f"{self.provider} universe needs the published target date lag"
                )
            local = context.observed_at.astimezone(context.zones[dataset]).date()
            effective = local - timedelta(days=lag)
            raw = self.fetch_index_month(
                context.http,
                day=effective,
                max_response_bytes=MAX_RESPONSE_BYTES,
                on_http_rate_limited=None,
                on_observed_bytes=context.observe_response,
            )
            self.validate_month(raw, effective)
            groups[dataset] = UniverseSnapshot(
                effective, [self._member()], {self.index_month_url(effective): raw}
            )
        return groups

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        if context.dataset_key != self.index_dataset:
            raise ReadinessBlockedError(f"{self.provider} does not serve {context.dataset_key}")
        if member["provider_symbol"] != self.index_symbol:
            raise ReadinessBlockedError(f"{self.provider} serves only {self.index_symbol}")
        if context.contract is None:
            raise ReadinessBlockedError(f"{self.provider} contract metadata is unavailable")
        day = context.trade_date
        key = (context.dataset_key, f"{day:%Y-%m}")
        raw = self._months.get(key)
        if raw is None or not self._holds(raw, day):
            raw = self.fetch_index_month(
                context.http,
                day=day,
                max_response_bytes=context.max_response_bytes,
                on_http_rate_limited=context.rate_limited,
                on_observed_bytes=context.observe_response,
            )
            self._months[key] = raw
        # Kept before reading, so an unpublished or malformed day still cites the response.
        stored = context.persist(raw, self.index_symbol)
        levels = self.index_row(raw, day)
        row = {
            "symbol": member["symbol"],
            "source_symbol": self.index_symbol,
            "trade_date": day.isoformat(),
            "currency": "TWD",
            # Order and range checks are Backend DQ's; indices have no volume.
            **{name: str(levels[name]) for name in ("open", "high", "low", "close")},
        }
        request = {
            "dataset_key": context.dataset_key,
            "schema_id": context.contract["schema_id"],
            "schema_version": context.contract["current_version"],
            "source": self.provider,
            "fetched_at": context.fetched_at.isoformat(),
            "payload": {
                "batch": {
                    "data_date": day.isoformat(),
                    "delivery_mode": "incremental",
                    "declared_record_count": 1,
                },
                "data": [row],
            },
            "delivery": dict(context.delivery),
        }
        return AcquisitionResult.data(request, stored)

    def _holds(self, raw: bytes, day: date) -> bool:
        try:
            self.index_row(raw, day)
        except TwOfficialNotPublishedError:
            return False
        return True

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        code = self.provider
        if isinstance(exc, TwOfficialNotPublishedError) and phase == "member":
            # The exchange adds the day's row after the close; nothing else marks it final.
            return ProviderFailure(
                FailureKind.ROUND_RETRY, wait="publication", code=f"{code}_index_not_published"
            )
        if isinstance(exc, TwOfficialFormatError):
            # The exchange changed its format: retrying cannot help, a code change can.
            return ProviderFailure(FailureKind.WORK_FAILED, code=f"{code}_format_changed")
        retryable = isinstance(exc, (TwOfficialPayloadError, httpx.TransportError)) or (
            isinstance(exc, TwOfficialResponseError) and exc.retryable
        )
        if phase == "member":
            if retryable:
                # A block page, a 403/429 burst limit or a server error: wait, then retry.
                return ProviderFailure(
                    FailureKind.ROUND_RETRY,
                    wait="outage",
                    code=f"{code}_unavailable",
                    retry_after=getattr(exc, "retry_after", 0) or 0,
                )
            if isinstance(exc, TwOfficialResponseError):
                return ProviderFailure(
                    FailureKind.WORK_FAILED,
                    http=exc.status_code is not None,
                    status_code=exc.status_code,
                    code=exc.category,
                )
            return PASSTHROUGH
        # Maintenance and daily planning settle only a retry or a failure.
        if isinstance(exc, TwOfficialResponseError) and exc.status_code is not None:
            return ProviderFailure(
                FailureKind.ROUND_RETRY
                if retryable or retryable_http_status(exc.status_code)
                else FailureKind.WORK_FAILED,
                http=True,
                status_code=exc.status_code,
            )
        if retryable or isinstance(exc, TwOfficialNotPublishedError):
            return ProviderFailure(FailureKind.ROUND_RETRY, code=f"{code}_unavailable")
        if isinstance(exc, TwOfficialResponseError):
            return ProviderFailure(FailureKind.WORK_FAILED, code=exc.category)
        return PASSTHROUGH

    def member_outcome(self, exc: BaseException) -> tuple[str, str | None] | None:
        # A past date the exchange has no row for will not gain one by retrying.
        if isinstance(exc, TwOfficialNotPublishedError):
            return ("missing", "no_source_row")
        return super().member_outcome(exc)

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        if isinstance(exc, TwOfficialResponseError):
            return {"category": exc.category, "http_status": exc.status_code}
        if isinstance(exc, TwOfficialFormatError):
            return {"category": "format_changed"}
        if isinstance(exc, TwOfficialPayloadError):
            return {"category": "malformed_response"}
        return None
