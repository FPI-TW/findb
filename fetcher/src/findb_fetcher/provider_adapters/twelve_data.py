"""Twelve Data: one native daily series per member."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from findb_fetcher.full_market_errors import (
    ReadinessBlockedError,
    member_identity_error,
    retryable_http_status,
)
from findb_fetcher.full_market_universe import (
    NASDAQ_URLS,
    TWELVE_DATA_EQUITY_TYPES,
    map_catalogue,
    nasdaq_effective_date,
    nasdaq_security_names,
    parse_nasdaq,
    scope_us_members,
)
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    CatalogueRequest,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
    UniverseContext,
    UniverseSnapshot,
)
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataNoNewDataError,
    TwelveDataPayloadError,
    TwelveDataResponseError,
)
from findb_fetcher.providers.twelve_data import (
    authorization_headers as td_authorization_headers,
)
from findb_fetcher.providers.twelve_data import (
    build_market_eod_request as td_request,
)

# HK EOD is owned by FactSet; Twelve Data never refreshes or acquires HK again.
CATALOGUE_COUNTRIES = {"us_equity_eod": "United States"}


class TwelveDataAdapter(ProviderAdapter):
    provider = "twelve_data"
    catalogue_datasets = tuple(CATALOGUE_COUNTRIES)
    # Existing journal identity of the country catalogue response; never rename.
    catalogue_journal = "twelve_catalogue"

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
            raise member_identity_error("provider identity differs from official universe")
        return AcquisitionResult.data(request, stored)

    def catalogue_request(self, dataset_key: str, max_response_bytes: int) -> CatalogueRequest:
        config = TwelveDataConfig.from_env()
        country = CATALOGUE_COUNTRIES[dataset_key]
        return CatalogueRequest(
            url=config.base_url + "/stocks",
            params={"country": country},
            headers=td_authorization_headers(config.api_key),
            evidence_url=config.base_url + "/stocks?" + urlencode({"country": country}),
            max_response_bytes=min(config.max_response_bytes, max_response_bytes),
        )

    def catalogue_rows(self, raw: bytes) -> list[dict[str, Any]]:
        value = json.loads(raw)
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise ReadinessBlockedError("provider catalogue shape invalid")
        return value["data"]

    def catalogue_evidence_dataset(self, url: str) -> str | None:
        country = dict(parse_qsl(urlsplit(url).query)).get("country")
        return next(
            (dataset for dataset, name in CATALOGUE_COUNTRIES.items() if name == country), None
        )

    def build_universe(
        self, feeds: Sequence[Mapping[str, Any]], context: UniverseContext
    ) -> dict[str, UniverseSnapshot]:
        groups = {}
        catalogue = context.catalogue(dataset_keys=[feed["dataset_key"] for feed in feeds])
        if "us_equity_eod" in context.dataset_keys:
            snapshots = {url: context.snapshot(url) for url in NASDAQ_URLS}
            us = parse_nasdaq(snapshots[NASDAQ_URLS[0]]) + parse_nasdaq(
                snapshots[NASDAQ_URLS[1]], other=True
            )
            us_dates = {
                nasdaq_effective_date(
                    snapshots[url],
                    observed_date=context.observed_at.astimezone(
                        context.zones["us_equity_eod"]
                    ).date(),
                )
                for url in NASDAQ_URLS
            }
            if len(us_dates) != 1:
                raise ReadinessBlockedError(
                    "Nasdaq official directories have different effective dates"
                )
            names = nasdaq_security_names(snapshots[NASDAQ_URLS[0]]) | nasdaq_security_names(
                snapshots[NASDAQ_URLS[1]], other=True
            )
            groups["us_equity_eod"] = UniverseSnapshot(
                next(iter(us_dates)),
                map_catalogue(
                    scope_us_members(us, catalogue, official_names=names),
                    catalogue,
                    us_official_names=names,
                ),
                {
                    **{url: snapshots[url] for url in NASDAQ_URLS},
                    **context.catalogue_evidence("us_equity_eod"),
                },
            )
        return groups

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

    def member_outcome(self, exc: BaseException) -> tuple[str, str | None] | None:
        # A symbol without a series for the date says nothing about the others.
        if isinstance(exc, TwelveDataNoNewDataError) or (
            isinstance(exc, TwelveDataResponseError)
            and not exc.is_rate_limited
            and exc.category in {"no_data", "not_found"}
        ):
            return ("missing", "no_source_row")
        if isinstance(exc, TwelveDataPayloadError):
            return ("invalid", "row_structure_invalid")
        return super().member_outcome(exc)

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        if not isinstance(exc, TwelveDataResponseError):
            return None
        return {
            "category": exc.category,
            "http_status": exc.status_code,
            "provider_code": exc.provider_code,
            "dispatched": exc.dispatched,
        }
