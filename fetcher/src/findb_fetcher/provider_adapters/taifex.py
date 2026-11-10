"""TAIFEX: one official daily report per date serves every contract/session member."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from functools import partial
from typing import Any

import httpx

from findb_fetcher.full_market_errors import (
    DatedUniversePendingError,
    ProviderHTTPError,
    ReadinessBlockedError,
    retryable_http_status,
)
from findb_fetcher.full_market_universe import (
    TAIFEX_HISTORY_URL,
    TAIFEX_URL,
    canonical_bytes,
    checksum,
)
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    DatedUniverse,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
)
from findb_fetcher.providers.taifex import (
    TaifexOperationalError,
    fetch_report,
    is_no_trade,
    parse_report,
    persist_dated_report,
    report_members,
)

# A TAIFEX report outage stops charging members, but only for this long per plan.
TAIFEX_OUTAGE_RETRY_SECONDS = 12 * 3600
TAIFEX_PRODUCTS = ("TX", "MTX", "TMF", "TE", "TF")
OBSERVATION_FIELDS = ("open", "high", "low", "close", "settlement_price", "open_interest")


def taifex_http_retryable(exc: TaifexOperationalError | ProviderHTTPError) -> bool:
    # A TAIFEX response-bound failure has no status and is a payload problem.
    if isinstance(exc, TaifexOperationalError) and exc.status_code is None:
        return False
    return retryable_http_status(exc.status_code)


def taifex_report_unavailable(exc: BaseException) -> bool:
    """Exchange-report outages that every member of a TAIFEX plan shares."""
    if isinstance(exc, (httpx.TransportError, DatedUniversePendingError)):
        return True
    return isinstance(exc, (TaifexOperationalError, ProviderHTTPError)) and taifex_http_retryable(
        exc
    )


class TaifexAdapter(ProviderAdapter):
    provider = "taifex"
    # Each date's universe is the report itself; there is no catalogue refresh.
    scheduled_maintenance = False
    dated_universe_code = "taifex_dated_universe_required"
    outage_retry_seconds = TAIFEX_OUTAGE_RETRY_SECONDS

    def __init__(self) -> None:
        self.reports: dict[str, tuple[bytes, list[dict[str, Any]], bool]] = {}

    def reset(self) -> None:
        self.reports.clear()

    def report(
        self, day: date, context: AcquisitionContext
    ) -> tuple[bytes, list[dict[str, Any]], bool]:
        """The date's report: the latest report when it is that date, else the dated one."""
        if day.isoformat() not in self.reports:
            latest_raw = context.snapshot(TAIFEX_URL, provider_response=True)
            if latest_raw:
                context.persist(latest_raw, "exchange_report")
            try:
                rows = parse_report(latest_raw, target_date=day)
                raw, historical = latest_raw, False
            except (ValueError, UnicodeError):
                reports = {}
                rows = []
                for product in TAIFEX_PRODUCTS:
                    product_raw = fetch_report(
                        context.http,
                        target_date=day,
                        latest=False,
                        product=product,
                        max_response_bytes=context.max_response_bytes,
                        on_observed_bytes=context.observe_response,
                        on_bounded_response=lambda size, declared: context.observe_response(
                            size, declared=declared
                        ),
                        on_http_rate_limited=context.rate_limited,
                    )
                    if not product_raw:
                        raise DatedUniversePendingError("dated_report_unavailable")
                    try:
                        _, parsed = persist_dated_report(
                            product_raw,
                            target_date=day,
                            product=product,
                            persist=partial(
                                context.persist, symbol=product, payload_format="csv_cp950"
                            ),
                        )
                    except (ValueError, UnicodeError):
                        raise DatedUniversePendingError("dated_report_unavailable") from None
                    if any(row["product_code"] != product for row in parsed):
                        raise DatedUniversePendingError("dated_report_unavailable")
                    reports[product] = product_raw.decode("cp950")
                    rows.extend(parsed)
                raw, historical = canonical_bytes(reports), True
            self.reports[day.isoformat()] = (raw, rows, historical)
        return self.reports[day.isoformat()]

    def dated_universe(self, day: date, context: AcquisitionContext) -> DatedUniverse:
        raw, rows, historical = self.report(day, context)
        url = (TAIFEX_HISTORY_URL if historical else TAIFEX_URL) + "?trade_date=" + day.isoformat()
        return DatedUniverse(raw=raw, members=report_members(rows), url=url)

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        day = context.trade_date
        raw, rows, historical = self.report(day, context)
        stored = context.persist(raw, "exchange_report")
        selected = [
            row
            for row in rows
            if row["contract_code"] == member["contract_code"]
            and row["session"] == member["session"]
        ]
        if len(selected) != 1:
            raise ReadinessBlockedError("actual TAIFEX contract/session is missing")
        if is_no_trade(selected[0]):
            evidence = {
                "url": TAIFEX_HISTORY_URL if historical else TAIFEX_URL,
                "sha256": checksum(raw),
                "observed_at": context.fetched_at.isoformat(),
                "source_symbol": member["provider_symbol"],
                "trade_date": day.isoformat(),
                "session": member["session"],
                "source_status": "no_trade",
                "source_excerpt": canonical_bytes(selected[0]).decode()[:500],
            }
            context.persist(raw, "exchange_report")
            return AcquisitionResult.no_trade_evidence(evidence)
        if (
            not any(selected[0][field] is not None for field in OBSERVATION_FIELDS)
            and selected[0]["volume"] is None
        ):
            raise ReadinessBlockedError("TAIFEX quote has no source-provided observation")
        if context.contract is None:
            raise ReadinessBlockedError("TAIFEX contract metadata is unavailable")
        request = {
            "dataset_key": context.dataset_key,
            "schema_id": context.contract["schema_id"],
            "schema_version": context.contract["current_version"],
            "source": "taifex",
            "fetched_at": context.fetched_at.isoformat(),
            "payload": {
                "batch": {
                    "data_date": day.isoformat(),
                    "delivery_mode": "incremental",
                    "declared_record_count": 1,
                },
                "data": selected,
            },
            "delivery": dict(context.delivery),
        }
        return AcquisitionResult.data(request, stored)

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        if phase == "member" and taifex_report_unavailable(exc):
            # One exchange report serves every member: an outage keeps the member
            # failure budget and ends the round, within a bounded window.
            return ProviderFailure(
                FailureKind.ROUND_RETRY,
                wait="outage",
                retry_after=getattr(exc, "retry_after", 0) or 0,
            )
        if phase == "round" and isinstance(exc, TaifexOperationalError):
            return ProviderFailure(
                FailureKind.ROUND_RETRY if taifex_http_retryable(exc) else FailureKind.WORK_FAILED,
                http=True,
                status_code=exc.status_code,
            )
        return PASSTHROUGH
