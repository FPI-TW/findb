"""FinLab: five shared OHLCV field tables per date, split into one request per member.

Intraday datasets differ: FinLab serves one partition per symbol and trade date,
so each member is its own provider request and nothing is shared across members.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, timezone
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_errors import MemberUnavailableError, ReadinessBlockedError
from findb_fetcher.full_market_universe import (
    TW_COMPANY_URLS,
    TW_ISIN_ETF_URLS,
    TW_ISIN_STOCK_URLS,
    canonical_bytes,
    checksum,
    parse_tw_companies,
    parse_tw_etfs,
    parse_tw_ordinary,
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
from findb_fetcher.providers.finlab import (
    FINLAB_FIELD_DATASETS,
    FINLAB_SDK_VERSION,
    FinLabConfigError,
    FinLabDatasetConfig,
    FinLabDatasetTable,
    FinLabOperationalError,
    FinLabPayloadError,
    FinLabSdkError,
    FinLabSdkGateway,
    FinLabSymbol,
    FinLabTargetNotReadyError,
    FinLabValueError,
    build_finlab_dataset_bundle,
    unobserved,
)
from findb_fetcher.providers.finlab import (
    build_market_eod_request as finlab_request,
)
from findb_fetcher.providers.finlab_intraday import (
    FinLabIntradayNotReadyError,
    fetch_partition,
    manifest_bytes,
)

# Shared FinLab evidence URL; the bundle checksum identifies the exact observation.
FINLAB_PRICE_EVIDENCE_URL = "https://www.finlab.tw/data/price-close?dataset={dataset}"
# The manifest entry FinLab answered for one symbol's trading day.
FINLAB_INTRADAY_EVIDENCE_URL = "https://www.finlab.tw/data/intraday?dataset={dataset}"
# Intraday datasets: the kind of record each stores and the daily dataset whose
# official member list it shares.
INTRADAY_DATASETS: Mapping[str, tuple[str, str]] = {
    "tw_equity_bar_1m": ("bar_1m", "tw_equity_eod"),
    "tw_equity_tick": ("tick", "tw_equity_eod"),
    "tw_etf_bar_1m": ("bar_1m", "tw_etf_eod"),
    "tw_etf_tick": ("tick", "tw_etf_eod"),
}
FINLAB_PRICE_FIELDS = ("open", "high", "low", "close")
# A table that cannot be fetched fails for every member alike. The plan retries it
# as a whole for this long, then the work ends; no member is charged for it.
FINLAB_OUTAGE_RETRY_SECONDS = 4 * 3600
FINLAB_OUTAGE_DELAY_SECONDS = 5 * 60
# The daily quota does not come back within minutes.
FINLAB_QUOTA_DELAY_SECONDS = 30 * 60
# Failure classes no retry can clear: the work ends at once. The value is the
# maintenance work's failure code.
_FINLAB_PERMANENT = {
    "auth": "MAINTENANCE_FINLAB_AUTH",
    # The installed SDK is not the reviewed release; only a new image clears it.
    "sdk_contract": "MAINTENANCE_FINLAB_CONFIG",
}


def finlab_no_trade(values: Mapping[str, object]) -> bool:
    """FinLab leaves every price empty, with no positive volume, on a day without trades."""
    volume = values["volume"]
    return all(unobserved(values[field]) for field in FINLAB_PRICE_FIELDS) and (
        unobserved(volume) or volume == 0
    )


def _table_payload(table: FinLabDatasetTable) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "dates": table.dates,
        "symbols": table.symbols,
        "values": [[str(value) for value in row] for row in table.values],
    }
    if table.previous:
        # Provenance of every carried price: the earlier close and its date.
        payload["previous"] = [[symbol, day, str(value)] for symbol, day, value in table.previous]
    return payload


def table_bytes(table: FinLabDatasetTable) -> bytes:
    """The exact detached bytes of one SDK table, as accounted and persisted."""
    return canonical_bytes(_table_payload(table))


class FinLabAdapter(ProviderAdapter):
    provider = "finlab"
    # The five field tables of a date are one shared observation of the cohort.
    shared_response_prefix = "finlab_dataset:"
    # Catalogue presence is an SDK observation inside ``build_universe``.
    catalogue_datasets = None
    outage_retry_seconds = FINLAB_OUTAGE_RETRY_SECONDS
    outage_holds_plan = True

    def __init__(self) -> None:
        self.bundles: dict[tuple[str, date], Any] = {}
        self.layouts: dict[tuple[Any, Any, str], list[dict[str, Any]]] = {}

    def reset(self) -> None:
        self.bundles.clear()

    def gateway(self, observe_response: Callable[[int], None]) -> FinLabSdkGateway:
        try:
            if importlib.metadata.version("finlab") != FINLAB_SDK_VERSION:
                raise ReadinessBlockedError("FinLab SDK version is unsupported")
        except importlib.metadata.PackageNotFoundError:
            raise ReadinessBlockedError("FinLab pinned SDK is unavailable") from None
        return FinLabSdkGateway.from_env(
            on_response_bytes=observe_response, allow_missing_symbols=True
        )

    def build_universe(
        self, feeds: Sequence[Mapping[str, Any]], context: UniverseContext
    ) -> dict[str, UniverseSnapshot]:
        """TWSE/TPEx official lists, mapped by FinLab SDK column presence."""
        # Each dataset downloads only its own lists; a refresh covers one dataset.
        cohorts: dict[str, list[dict[str, Any]]] = {}
        official: dict[str, dict[str, bytes]] = {}
        intraday = [dataset for dataset in INTRADAY_DATASETS if dataset in context.zones]
        if intraday:
            groups = self._intraday_universe(intraday, context)
            if len(intraday) == len(context.zones):
                return groups
            # A refresh of FinLab's whole scope carries daily datasets too.
            daily = {key: zone for key, zone in context.zones.items() if key not in groups}
            return groups | self.build_universe(feeds, replace(context, zones=daily))
        if "tw_equity_eod" in context.zones:
            snapshots = {
                url: context.snapshot(url) for url in (*TW_COMPANY_URLS, *TW_ISIN_STOCK_URLS)
            }
            companies = {
                (member["symbol"], member["exchange"])
                for index, url in enumerate(TW_COMPANY_URLS)
                for member in parse_tw_companies(snapshots[url], otc=bool(index))
            }
            equity = [
                member
                for index, url in enumerate(TW_ISIN_STOCK_URLS)
                for member in parse_tw_ordinary(snapshots[url], otc=bool(index))
            ]
            for member in equity:
                if (member["symbol"], member["exchange"]) not in companies:
                    member["classification"] = "classification_gap"
            cohorts["tw_equity_eod"], official["tw_equity_eod"] = equity, snapshots
        if "tw_etf_eod" in context.zones:
            snapshots = {url: context.snapshot(url) for url in TW_ISIN_ETF_URLS}
            cohorts["tw_etf_eod"] = [
                member
                for index, url in enumerate(TW_ISIN_ETF_URLS)
                for member in parse_tw_etfs(snapshots[url], otc=bool(index))
            ]
            official["tw_etf_eod"] = snapshots
        # Catalogue presence is independent of daily OHLCV completeness.
        catalogue_snapshots: dict[str, dict[str, bytes]] = {}
        context.refresh_limits()
        gateway = self.gateway(context.observe_response)
        for dataset, members in cohorts.items():
            if dataset not in context.dataset_keys:
                continue
            try:
                try:
                    table = gateway.fetch_catalogue(
                        symbols=tuple(member["provider_symbol"] for member in members),
                    )
                finally:
                    context.pace()
            except FinLabOperationalError:
                raise
            except FinLabSdkError:
                for member in members:
                    member["mapping_status"] = "gap"
                continue
            detached = table_bytes(table)
            context.observe_response(len(detached))
            url = FINLAB_PRICE_EVIDENCE_URL.format(dataset=dataset)
            context.snapshot_objects[url] = context.persist(detached, dataset, "provider_catalogue")
            catalogue_snapshots[dataset] = {url: detached}
            for member in members:
                member["mapping_status"] = (
                    "mapped"
                    if member["classification"] != "classification_gap"
                    and member["provider_symbol"] in table.symbols
                    else "gap"
                )
        groups = {
            dataset: UniverseSnapshot(
                context.observed_at.astimezone(context.zones[dataset]).date(),
                members,
                official[dataset],
            )
            for dataset, members in cohorts.items()
        }
        for dataset, evidence in catalogue_snapshots.items():
            groups[dataset].evidence.update(evidence)
        return groups

    def _intraday_universe(
        self, datasets: Sequence[str], context: UniverseContext
    ) -> dict[str, UniverseSnapshot]:
        """The same official lists as the daily datasets, without a catalogue check.

        FinLab has no listing of the symbols it serves intraday, and it serves some
        the daily price table does not carry yet (a new listing, for one). Every
        classified member is therefore attempted; a symbol FinLab does not have is
        found out by its own partition request.
        """
        groups: dict[str, UniverseSnapshot] = {}
        for dataset in datasets:
            if INTRADAY_DATASETS[dataset][1] == "tw_equity_eod":
                snapshots = {
                    url: context.snapshot(url) for url in (*TW_COMPANY_URLS, *TW_ISIN_STOCK_URLS)
                }
                companies = {
                    (member["symbol"], member["exchange"])
                    for index, url in enumerate(TW_COMPANY_URLS)
                    for member in parse_tw_companies(snapshots[url], otc=bool(index))
                }
                members = [
                    member
                    for index, url in enumerate(TW_ISIN_STOCK_URLS)
                    for member in parse_tw_ordinary(snapshots[url], otc=bool(index))
                ]
                for member in members:
                    if (member["symbol"], member["exchange"]) not in companies:
                        member["classification"] = "classification_gap"
            else:
                snapshots = {url: context.snapshot(url) for url in TW_ISIN_ETF_URLS}
                members = [
                    member
                    for index, url in enumerate(TW_ISIN_ETF_URLS)
                    for member in parse_tw_etfs(snapshots[url], otc=bool(index))
                ]
            for member in members:
                member["mapping_status"] = (
                    "mapped" if member["classification"] != "classification_gap" else "gap"
                )
            groups[dataset] = UniverseSnapshot(
                context.observed_at.astimezone(context.zones[dataset]).date(), members, snapshots
            )
        return groups

    def _acquire_intraday(
        self, member: Mapping[str, Any], context: AcquisitionContext
    ) -> AcquisitionResult:
        dataset, day = context.dataset_key, context.trade_date
        record_kind = INTRADAY_DATASETS[dataset][0]
        provider_symbol = member["provider_symbol"]
        partition = fetch_partition(
            self.gateway(context.observe_response),
            record_kind=record_kind,
            provider_symbol=provider_symbol,
            symbol=member["symbol"],
            trade_date=day,
            layout=self._row_layout(context, record_kind),
        )
        if partition.status in {"no_trades", "absent"}:
            # FinLab states no trades itself; an absent partition is only accepted as
            # no trade once Source finds the same day's daily bar without a board lot.
            absent = partition.status == "absent"
            evidence = manifest_bytes(partition, dataset=dataset)
            return AcquisitionResult.no_trade_evidence(
                {
                    "url": FINLAB_INTRADAY_EVIDENCE_URL.format(dataset=dataset),
                    "sha256": checksum(evidence),
                    "observed_at": context.fetched_at.isoformat(),
                    "source_symbol": provider_symbol,
                    "trade_date": day.isoformat(),
                    "session": member.get("session"),
                    "source_status": "no_record" if absent else "no_trade",
                    "source_excerpt": evidence.decode()[:500],
                },
                context.persist(evidence, provider_symbol),
                unconfirmed=FinLabIntradayNotReadyError("partition") if absent else None,
            )
        # The provider's file is the evidence; the mapped rows are what is delivered.
        original = context.persist(partition.original, provider_symbol, payload_format="parquet")
        rows = context.persist(partition.rows, provider_symbol, payload_format="parquet")
        contract = context.contract or {}
        request = {
            "dataset_key": dataset,
            "schema_id": contract["schema_id"],
            "schema_version": contract["current_version"],
            "source": "finlab",
            "request_key": f"finlab:{dataset}:{provider_symbol}:{day.isoformat()}",
            "idempotency_key": f"finlab:{dataset}:{provider_symbol}:{day.isoformat()}",
            "fetched_at": context.fetched_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "payload": {
                "batch": {
                    "data_date": day.isoformat(),
                    "delivery_mode": "incremental",
                    "declared_record_count": 1,
                },
                "data": [
                    {
                        "symbol": member["symbol"],
                        "source_symbol": provider_symbol,
                        "trade_date": day.isoformat(),
                        "record_kind": record_kind,
                        "row_count": partition.row_count,
                        "rows_ref": rows.ref,
                        "rows_sha256": rows.sha256,
                        "rows_bytes": rows.size_bytes,
                        "rows_format": "parquet",
                        "price_adjustment": "none",
                        "volume_unit": "lot",
                    }
                ],
            },
        }
        if context.delivery is not None:
            request["delivery"] = dict(context.delivery)
        return AcquisitionResult.data(request, original)

    def _row_layout(self, context: AcquisitionContext, record_kind: str) -> list[dict[str, Any]]:
        """The column layout the installed contract publishes for this record kind."""
        contract = context.contract or {}
        key = (contract.get("schema_id"), contract.get("current_version"), record_kind)
        if key not in self.layouts:
            if contract.get("schema_id") != "intraday_partition":
                raise FinLabConfigError("FinLab intraday dataset has no partition contract")
            path = (
                Path(os.getenv("FETCHER_CONTRACTS_DIR", "/app/contracts"))
                / "intraday_partition"
                / f"v{int(contract['current_version'])}.schema.json"
            )
            try:
                columns = json.loads(path.read_text(encoding="utf-8"))["x-findb-row-layouts"][
                    record_kind
                ]["columns"]
            except (OSError, ValueError, KeyError, TypeError):
                raise FinLabConfigError("FinLab intraday row layout is unavailable") from None
            self.layouts[key] = [dict(column) for column in columns]
        return self.layouts[key]

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        dataset, day = context.dataset_key, context.trade_date
        if dataset in INTRADAY_DATASETS:
            return self._acquire_intraday(member, context)
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
                {field: _table_payload(table) for field, table in tables.items()}
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
                },
                stored,
            )
        if all(unobserved(values[field]) for field in FINLAB_PRICE_FIELDS):
            # Positive volume with every price empty: open, high, low and close are
            # the latest earlier close; volume stays as served.
            earlier = {symbol: close for symbol, _, close in tables["close"].previous}
            carried = earlier.get(provider_symbol)
            if carried is not None:
                values.update(dict.fromkeys(FINLAB_PRICE_FIELDS, carried))
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
                dates=table.dates, symbols=(provider_symbol,), values=((values[field],),)
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
        if phase == "member" and isinstance(exc, FinLabIntradayNotReadyError):
            if exc.scope == "calendar":
                # FinLab has not verified the trade date: no symbol can be served yet.
                return ProviderFailure(
                    FailureKind.ROUND_RETRY,
                    wait="publication",
                    code="finlab_target_not_ready",
                )
            # Only this symbol has no partition: not built yet, or not carried at
            # all. It keeps its bounded retries and the other members go on; a long
            # unbroken run of them holds the plan, which covers a day whose
            # partitions are still being built.
            return ProviderFailure(
                FailureKind.MEMBER,
                reason="empty_response",
                no_data=True,
                code="finlab_partition_not_ready",
            )
        if phase == "member" and isinstance(exc, FinLabTargetNotReadyError):
            return ProviderFailure(
                FailureKind.ROUND_RETRY,
                wait="publication",
                code="finlab_target_not_ready",
            )
        if phase == "member" and isinstance(exc, (FinLabSdkError, FinLabConfigError)):
            # Only the shared table fetch raises these in the member loop, so the
            # failure belongs to the provider or the account, never to this member.
            code = (
                exc.code
                if isinstance(exc, FinLabOperationalError)
                else "config"
                if isinstance(exc, FinLabConfigError)
                else "table_invalid"
            )
            if isinstance(exc, FinLabConfigError) or code in _FINLAB_PERMANENT:
                return ProviderFailure(FailureKind.WORK_FAILED, code="finlab_" + code)
            return ProviderFailure(
                FailureKind.ROUND_RETRY,
                wait="outage",
                code="finlab_" + code,
                retry_after=(
                    FINLAB_QUOTA_DELAY_SECONDS if code == "quota" else FINLAB_OUTAGE_DELAY_SECONDS
                ),
            )
        if phase == "maintenance":
            if isinstance(exc, FinLabOperationalError):
                if exc.code in _FINLAB_PERMANENT:
                    return ProviderFailure(
                        FailureKind.WORK_FAILED, code=_FINLAB_PERMANENT[exc.code]
                    )
                return ProviderFailure(
                    FailureKind.ROUND_RETRY, code="MAINTENANCE_FINLAB_OPERATIONAL"
                )
            if isinstance(exc, FinLabConfigError):
                return ProviderFailure(FailureKind.WORK_FAILED, code="MAINTENANCE_FINLAB_CONFIG")
        return PASSTHROUGH

    def pending_shared_fetch(self, dataset_key: str, trade_date: date) -> bool:
        if dataset_key in INTRADAY_DATASETS:
            return False
        return (dataset_key, trade_date) not in self.bundles

    def member_outcome(self, exc: BaseException) -> tuple[str, str | None] | None:
        # One symbol's values failing the mapper never reject the shared tables.
        if isinstance(exc, FinLabPayloadError) and not isinstance(exc, FinLabTargetNotReadyError):
            return ("invalid", "row_structure_invalid")
        return super().member_outcome(exc)

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        if isinstance(exc, FinLabValueError):
            return {"category": f"finlab_value:{exc.code}"}
        if isinstance(exc, FinLabOperationalError):
            return {"category": f"finlab_sdk:{exc.code}"}
        return None
