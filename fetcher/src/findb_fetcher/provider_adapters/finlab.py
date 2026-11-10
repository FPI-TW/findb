"""FinLab: five shared OHLCV field tables per date, split into one request per member."""

from __future__ import annotations

import importlib.metadata
import math
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from findb_fetcher.full_market_errors import MemberUnavailableError, ReadinessBlockedError
from findb_fetcher.full_market_universe import canonical_bytes, checksum
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
)
from findb_fetcher.providers.finlab import (
    FINLAB_FIELD_DATASETS,
    FinLabConfigError,
    FinLabDatasetConfig,
    FinLabDatasetTable,
    FinLabOperationalError,
    FinLabSdkGateway,
    FinLabSymbol,
    FinLabTargetNotReadyError,
    FinLabValueError,
    build_finlab_dataset_bundle,
)
from findb_fetcher.providers.finlab import (
    build_market_eod_request as finlab_request,
)

# Shared FinLab evidence URL; the bundle checksum identifies the exact observation.
FINLAB_PRICE_EVIDENCE_URL = "https://www.finlab.tw/data/price-close?dataset={dataset}"
FINLAB_PRICE_FIELDS = ("open", "high", "low", "close")


def _unobserved(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return value is None
    try:
        return not math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def finlab_no_trade(values: Mapping[str, object]) -> bool:
    """FinLab leaves every price empty, with no positive volume, on a day without trades."""
    volume = values["volume"]
    return all(_unobserved(values[field]) for field in FINLAB_PRICE_FIELDS) and (
        _unobserved(volume) or volume == 0
    )


def table_bytes(table: FinLabDatasetTable) -> bytes:
    """The exact detached bytes of one SDK table, as accounted and persisted."""
    return canonical_bytes(
        {
            "dates": table.dates,
            "symbols": table.symbols,
            "values": [[str(value) for value in row] for row in table.values],
        }
    )


class FinLabAdapter(ProviderAdapter):
    provider = "finlab"
    # The five field tables of a date are one shared observation of the cohort.
    shared_response_prefix = "finlab_dataset:"

    def __init__(self) -> None:
        self.bundles: dict[tuple[str, date], Any] = {}

    def reset(self) -> None:
        self.bundles.clear()

    def gateway(self, observe_response: Callable[[int], None]) -> FinLabSdkGateway:
        try:
            if importlib.metadata.version("finlab") != "1.5.7":
                raise ReadinessBlockedError("FinLab SDK version is unsupported")
        except importlib.metadata.PackageNotFoundError:
            raise ReadinessBlockedError("FinLab pinned SDK is unavailable") from None
        return FinLabSdkGateway.from_env(
            on_response_bytes=observe_response, allow_missing_symbols=True
        )

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        dataset, day = context.dataset_key, context.trade_date
        cache_key = (dataset, day)
        if cache_key not in self.bundles:
            if context.cohort is None:
                raise KeyError("members")
            config = FinLabDatasetConfig(
                dataset_key=dataset,
                market="TW",
                asset_class=context.feed["asset_class"],
                currency="TWD",
                field_datasets=dict(FINLAB_FIELD_DATASETS),
                # Gap members never reach a provider call, so they never
                # constrain the table FinLab serves for the mapped ones.
                symbols=tuple(
                    FinLabSymbol(item["provider_symbol"], item["symbol"])
                    for item in context.cohort
                    if item["mapping_status"] == "mapped"
                ),
                production_scope=True,
            )
            gateway = self.gateway(context.observe_response)
            tables = {}
            for field, name in config.field_datasets.items():
                tables[field] = gateway.fetch_dataset(
                    name,
                    target_date=day,
                    symbols=tuple(item.source_symbol for item in config.symbols),
                )
                context.observe_response(len(table_bytes(tables[field])))
            table_raw = canonical_bytes(
                {
                    field: {
                        "dates": table.dates,
                        "symbols": table.symbols,
                        "values": [[str(value) for value in row] for row in table.values],
                    }
                    for field, table in tables.items()
                }
            )
            # Five independent field responses share a bounded derived bundle.
            if len(table_raw) > 5 * context.max_response_bytes + 64:
                raise ReadinessBlockedError("SDK derived bundle exceeds aggregate bound")
            table_stored = context.persist(table_raw, "dataset_bundle")
            self.bundles[cache_key] = (config, tables, table_raw, table_stored)
        full_config, tables, raw, stored = self.bundles[cache_key]
        provider_symbol = member["provider_symbol"]
        if any(provider_symbol not in table.symbols for table in tables.values()):
            raise MemberUnavailableError("missing_symbol")
        values = {
            field: table.values[0][table.symbols.index(provider_symbol)]
            for field, table in tables.items()
        }
        if finlab_no_trade(values):
            return AcquisitionResult.no_trade_evidence(
                {
                    "url": FINLAB_PRICE_EVIDENCE_URL.format(dataset=dataset),
                    "sha256": checksum(raw),
                    "observed_at": context.fetched_at.isoformat(),
                    "source_symbol": provider_symbol,
                    "trade_date": day.isoformat(),
                    "session": member.get("session"),
                    "source_status": "no_trade",
                    "source_excerpt": canonical_bytes(
                        {field: str(value) for field, value in sorted(values.items())}
                    ).decode()[:500],
                }
            )
        single_config = FinLabDatasetConfig(
            dataset_key=dataset,
            market="TW",
            asset_class=context.feed["asset_class"],
            currency="TWD",
            field_datasets=full_config.field_datasets,
            symbols=(FinLabSymbol(member["provider_symbol"], member["symbol"]),),
            production_scope=True,
        )
        single_tables = {
            field: FinLabDatasetTable(
                dates=table.dates,
                symbols=(member["provider_symbol"],),
                values=tuple(
                    (values[table.symbols.index(member["provider_symbol"])],)
                    for values in table.values
                ),
            )
            for field, table in tables.items()
        }
        try:
            bundle = build_finlab_dataset_bundle(
                config=single_config, target_date=day, tables=single_tables
            )
        except FinLabValueError as exc:
            # The shared table passed validation; only this member's values are unusable.
            raise MemberUnavailableError(exc.code) from None
        request = finlab_request(bundle, fetched_at=context.fetched_at, delivery=context.delivery)
        request["payload"]["data"] = [
            row for row in request["payload"]["data"] if row["symbol"] == member["symbol"]
        ]
        request["payload"]["batch"]["declared_record_count"] = 1
        request["payload"]["batch"]["delivery_mode"] = "incremental"
        return AcquisitionResult.data(request, stored)

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        if phase == "member" and isinstance(exc, FinLabTargetNotReadyError):
            return ProviderFailure(
                FailureKind.ROUND_RETRY,
                wait="publication",
                code="finlab_target_not_ready",
            )
        if phase == "maintenance":
            if isinstance(exc, FinLabOperationalError):
                return ProviderFailure(
                    FailureKind.ROUND_RETRY, code="MAINTENANCE_FINLAB_OPERATIONAL"
                )
            if isinstance(exc, FinLabConfigError):
                return ProviderFailure(FailureKind.WORK_FAILED, code="MAINTENANCE_FINLAB_CONFIG")
        return PASSTHROUGH

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        if isinstance(exc, FinLabValueError):
            return {"category": f"finlab_value:{exc.code}"}
        return None
