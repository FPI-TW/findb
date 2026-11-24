"""FMP: one daily request per market index, grouped into datasets by publication time.

The index lists are fixed here: each dataset holds the indices that settle at
about the same time (``docs/dev/fmp-provider.md``). A universe refresh checks
them against FMP's own index list and publishes that list as the evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from findb_fetcher.full_market_errors import (
    MemberUnavailableError,
    ReadinessBlockedError,
    retryable_http_status,
)
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
from findb_fetcher.providers.fmp import (
    FMP_INDEX_LIST_PATH,
    RETRYABLE_CATEGORIES,
    FMPClient,
    FMPConfig,
    FMPPayloadError,
    FMPResponseError,
    FMPRowError,
    FMPRowNotPublishedError,
    contract_row,
    index_list,
    target_row,
)

# Dataset -> FMP symbol -> currency. Each group is one schedule; see the catalog.
INDEX_UNIVERSES: dict[str, dict[str, str]] = {
    "hk_index_eod": {"^HSI": "HKD", "^HSCE": "HKD", "512.HK": "HKD"},
    "us_index_eod": {
        # First batch
        "^GSPC": "USD",
        "^NDX": "USD",
        "^DJI": "USD",
        "^IXIC": "USD",
        "^RUT": "USD",
        "^VIX": "USD",
        "^SOX": "USD",
        "^NYA": "USD",
        # Broad market and Russell
        "^DJT": "USD",
        "^DJU": "USD",
        "^XAX": "USD",
        "^OEX": "USD",
        "^MID": "USD",
        "^SPXEW": "USD",
        "^W5000": "USD",
        "^DWCF": "USD",
        "^RUI": "USD",
        "^RUA": "USD",
        "^RLG": "USD",
        "^RLV": "USD",
        # Sectors and industries
        "^GSPE": "USD",
        "^SP500-15": "USD",
        "^SP500-20": "USD",
        "^SP500-25": "USD",
        "^SP500-30": "USD",
        "^SP500-35": "USD",
        "^SP500-40": "USD",
        "^SP500-45": "USD",
        "^SP500-50": "USD",
        "^SP500-55": "USD",
        "^SP500-60": "USD",
        "^SIXB": "USD",
        "^SIXC": "USD",
        "^SIXE": "USD",
        "^SIXI": "USD",
        "^SIXM": "USD",
        "^SIXR": "USD",
        "^SIXRE": "USD",
        "^SIXT": "USD",
        "^SIXU": "USD",
        "^SIXV": "USD",
        "^SIXY": "USD",
        "^DJUSSC": "USD",
        "^BKX": "USD",
        "^NBI": "USD",
        "^SPSIBI": "USD",
        "^ICEBIO": "USD",
        "^DJUSPM": "USD",
        "^PSE": "USD",
        "^EMCLOUD": "USD",
        "^TRAN": "USD",
        "^AMZ": "USD",
        # Volatility, rates, the dollar and commodities
        "^VIX1D": "USD",
        "^VIX9D": "USD",
        "^VIX3M": "USD",
        "^VIX6M": "USD",
        "^VIX1Y": "USD",
        "^VVIX": "USD",
        "^VXN": "USD",
        "^RVX": "USD",
        "^VXD": "USD",
        "^GVZ": "USD",
        "^OVX": "USD",
        "^VXSLV": "USD",
        "^VXTLT": "USD",
        "^VIXEQ": "USD",
        "^IRX": "USD",
        "^FVX": "USD",
        "^TNX": "USD",
        "^TYX": "USD",
        "DX-Y.NYB": "USD",
        "^IDCTCRT": "USD",
        "^SPGSCI": "USD",
    },
}


class FMPAdapter(ProviderAdapter):
    provider = "fmp"
    # The index list is read inside ``build_universe`` with the account key.
    catalogue_datasets = None

    def __init__(self) -> None:
        self._client: FMPClient | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def client(self, http) -> FMPClient:
        if self._client is None:
            self._client = FMPClient(FMPConfig.from_env(), client=http)
        return self._client

    def _native(self, observe_response, call):
        try:
            return call()
        except FMPResponseError as exc:
            if exc.observed_bytes or exc.declared_bytes:
                try:
                    observe_response(exc.observed_bytes, declared=exc.declared_bytes)
                except ReadinessBlockedError:
                    # Overage debt is retained without erasing the known HTTP status.
                    raise exc from None
            raise

    def build_universe(
        self, feeds: Sequence[Mapping[str, Any]], context: UniverseContext
    ) -> dict[str, UniverseSnapshot]:
        """Each configured index, as FMP lists it; a missing or re-denominated one stops."""
        datasets = [feed["dataset_key"] for feed in feeds]
        if unknown := sorted(set(datasets) - set(INDEX_UNIVERSES)):
            raise ReadinessBlockedError(f"FMP has no index list for {unknown}")
        if context.http is None:
            raise ReadinessBlockedError("FMP universe refresh needs the governed HTTP client")
        client = self.client(context.http)
        raw = self._native(
            context.observe_response,
            lambda: client.index_list_raw(on_observed_bytes=context.observe_response),
        )
        listed = index_list(raw)
        evidence_url = client.base_url + FMP_INDEX_LIST_PATH
        groups = {}
        for feed in feeds:
            dataset = feed["dataset_key"]
            members = []
            for symbol, currency in INDEX_UNIVERSES[dataset].items():
                row = listed.get(symbol)
                if row is None or row.get("currency") != currency:
                    raise ReadinessBlockedError(f"FMP index list does not carry {symbol}")
                members.append(
                    {
                        "symbol": symbol,
                        "provider_symbol": symbol,
                        "exchange": str(row.get("exchange") or "FMP"),
                        "currency": currency,
                        "asset_class": "index",
                        "mapping_status": "mapped",
                    }
                )
            # The list is effective for the market date the feed's next run fetches;
            # only Source publishes the lag (packaged descriptors carry no schedule).
            lag = (context.target_date_lags or {}).get(dataset)
            if type(lag) is not int or lag < 0:
                raise ReadinessBlockedError("FMP universe needs the published target date lag")
            local = context.observed_at.astimezone(context.zones[dataset]).date()
            effective = local - timedelta(days=lag)
            groups[dataset] = UniverseSnapshot(effective, members, {evidence_url: raw})
        return groups

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        day, symbol = context.trade_date, member["provider_symbol"]
        client = self.client(context.http)
        raw = self._native(
            context.observe_response,
            lambda: client.index_daily(
                symbol,
                day,
                max_response_bytes=context.max_response_bytes,
                on_http_rate_limited=context.rate_limited,
                on_observed_bytes=context.observe_response,
            ),
        )
        # Kept before reading, so a missing or invalid row still cites what FMP served.
        stored = context.persist(raw, symbol)
        native = target_row(raw, symbol=symbol, day=day)
        try:
            row = contract_row(native, symbol=member["symbol"], currency=member["currency"])
        except FMPRowError as exc:
            raise MemberUnavailableError(exc.code.lower()) from None
        if context.contract is None:
            raise ReadinessBlockedError("FMP contract metadata is unavailable")
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

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        if isinstance(exc, FMPRowNotPublishedError) and phase == "member":
            # FMP serves a date only once it has a row for it; nothing marks it final.
            return ProviderFailure(
                FailureKind.ROUND_RETRY, wait="publication", code="fmp_row_not_published"
            )
        if isinstance(exc, FMPPayloadError):
            # A malformed index list is retried like a malformed daily row; the
            # work's attempt budget bounds how often.
            return ProviderFailure(FailureKind.ROUND_RETRY, code="fmp_payload_invalid")
        if not isinstance(exc, FMPResponseError):
            return PASSTHROUGH
        if phase == "member":
            if exc.category == "rate_limited":
                return ProviderFailure(
                    FailureKind.QUOTA_WAIT, reason="rate_limited", retry_after=exc.retry_after
                )
            if exc.category == "plan_restricted":
                # The plan does not cover this index; the others are unaffected.
                return ProviderFailure(FailureKind.MEMBER, code=exc.category)
            return ProviderFailure(FailureKind.ROUND_RETRY, code=exc.category)
        # Maintenance and daily planning settle only a retry or a failure, so a 429
        # retries the round; the governor has already recorded its cooldown.
        if exc.status_code is None:
            retryable = exc.category in RETRYABLE_CATEGORIES
            return ProviderFailure(
                FailureKind.ROUND_RETRY if retryable else FailureKind.WORK_FAILED,
                code=exc.category,
            )
        return ProviderFailure(
            FailureKind.ROUND_RETRY
            if retryable_http_status(exc.status_code)
            else FailureKind.WORK_FAILED,
            http=True,
            status_code=exc.status_code,
        )

    def member_outcome(self, exc: BaseException) -> tuple[str, str | None] | None:
        # A past date FMP has no row for will not gain one by retrying.
        if isinstance(exc, FMPRowNotPublishedError):
            return ("missing", "no_source_row")
        return super().member_outcome(exc)

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        if isinstance(exc, FMPResponseError):
            return {"category": exc.category, "http_status": exc.status_code}
        return None
