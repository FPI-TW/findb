"""Read-only query services for the public Serve API."""

from __future__ import annotations

import base64
import binascii
import calendar
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import String, and_, case, func, literal_column, or_, select, true, tuple_
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, aliased
from sqlalchemy.sql import ColumnElement

from app.models.canonical import (
    ExchangeDailyQuote,
    FuturesContract,
    FuturesContractEOD,
    Instrument,
    InstrumentStats,
    MarketDataEOD,
    MarketDataMinute,
)
from app.models.registry import DatasetRegistry, IngestionRun
from app.schemas.common import PaginationInfo
from app.schemas.serve import (
    CalendarListResponse,
    CalendarResponse,
    CursorPaginationInfo,
    DatasetCoverageResponse,
    DatasetListResponse,
    DatasetResponse,
    EODCoverageResponse,
    EODLatestMissing,
    EODLatestQuote,
    EODLatestResponse,
    EODListResponse,
    EODResponse,
    EODUnitsResponse,
    ExchangeQuoteListResponse,
    ExchangeQuoteResponse,
    FuturesContractListResponse,
    FuturesContractResponse,
    FuturesEODListResponse,
    FuturesEODResponse,
    FuturesUnitsResponse,
    InstrumentCoverageResponse,
    InstrumentDetailResponse,
    InstrumentFacetsResponse,
    InstrumentListResponse,
    InstrumentResolution,
    InstrumentResolveResponse,
    InstrumentResponse,
    InstrumentSort,
    InstrumentSummaryResponse,
    IntradayCoverageResponse,
    MinuteCoverageResponse,
    MinuteListResponse,
    MinuteResponse,
    ResolvedDateRangeResponse,
    SortDirection,
    VolumeUnit,
)
from app.services.ingress_contracts import (
    INTRADAY_DATASET_FREQUENCY,
    DatasetContractDeclaration,
)
from app.services.serve_errors import serve_error
from app.services.serve_selectors import InstrumentSelector, parse_selectors

LookupColumn = ColumnElement[Any] | InstrumentedAttribute[Any]
DataKind = Literal["eod", "minute", "futures_eod"]
MAX_CURSOR_LENGTH = 2048
MAX_CURSOR_BYTES = 1536
BASE64URL_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SUPPORTED_SERVE_VERSIONS = {
    "market_eod": frozenset({1, 2}),
    "market_minute": frozenset({1}),
    "futures_eod": frozenset({1}),
}
SUPPORTED_ACTIVE_SCOPES = frozenset(
    {
        ("market_eod", "US", "equity"),
        ("market_eod", "TW", "equity"),
        ("market_eod", "TW", "etf"),
        ("market_eod", "HK", "equity"),
        ("market_eod", "US", "index"),
        ("market_eod", "HK", "index"),
        ("market_eod", "TW", "index"),
        ("futures_eod", "TW", "future"),
        ("market_minute", "TW", "equity"),
        ("market_minute", "TW", "etf"),
    }
)


INTRADAY_ACTIVE_SCOPES = frozenset({("TW", "equity"), ("TW", "etf")})
# Exchange quotes reach Serve as field corrections of the provider's EOD rows.
EXCHANGE_QUOTE_ACTIVE_SCOPES = frozenset({("HK", "equity")})


@dataclass(frozen=True)
class ActiveDatasetScope:
    dataset_key: str
    market: str
    asset_class: str
    frequency: Literal["daily", "minute"]
    data_kind: DataKind
    interval: Literal["1d", "1m"]


def configuration_error() -> HTTPException:
    return serve_error(503, "SERVE_CONFIGURATION_INVALID", "Serve dataset configuration is invalid")


@dataclass(frozen=True)
class ExchangeQuoteScope:
    """An active dataset of an exchange's official daily quotation report."""

    dataset_key: str
    market: str
    asset_class: str


@dataclass(frozen=True)
class IntradayDatasetScope:
    """An active dataset whose rows are canonical intraday objects."""

    dataset_key: str
    market: str
    asset_class: str
    frequency: Literal["minute", "tick"]
    record_kind: Literal["bar_1m", "tick"]


async def active_dataset_scopes(db: AsyncSession) -> tuple[ActiveDatasetScope, ...]:
    return (await _active_registry_scopes(db))[0]


async def active_intraday_datasets(db: AsyncSession) -> tuple[IntradayDatasetScope, ...]:
    return (await _active_registry_scopes(db))[1]


async def active_exchange_quote_datasets(db: AsyncSession) -> tuple[ExchangeQuoteScope, ...]:
    return (await _active_registry_scopes(db))[2]


async def _active_registry_scopes(
    db: AsyncSession,
) -> tuple[
    tuple[ActiveDatasetScope, ...],
    tuple[IntradayDatasetScope, ...],
    tuple[ExchangeQuoteScope, ...],
]:
    """Every active dataset's read model; any inconsistent declaration fails closed."""
    rows = (
        await db.execute(
            select(DatasetRegistry)
            .where(DatasetRegistry.is_active.is_(True))
            .order_by(DatasetRegistry.dataset_key)
        )
    ).scalars()
    scopes: list[ActiveDatasetScope] = []
    intraday: list[IntradayDatasetScope] = []
    quotes: list[ExchangeQuoteScope] = []
    for row in rows:
        try:
            declaration = DatasetContractDeclaration.model_validate(row.config)
            schema_id = declaration.schema_id
            market = row.market.strip().upper()
            asset_class = row.asset_class.strip().lower()
            frequency = row.frequency.strip().lower()
            if declaration.schema_enforcement != "enforce":
                raise ValueError("schema enforcement must be enabled")
            if schema_id == "intraday_partition":
                # Intraday rows live in canonical objects, not in the tables these
                # scopes describe. A correctly declared dataset is simply not part
                # of them; a wrong one fails closed like any other active dataset.
                if (
                    set(declaration.accepted_schema_versions) != {1}
                    or declaration.defaults.market != market
                    or declaration.defaults.asset_class != asset_class
                    or (market, asset_class) not in INTRADAY_ACTIVE_SCOPES
                    or frequency
                    != INTRADAY_DATASET_FREQUENCY[cast(str, declaration.defaults.record_kind)]
                ):
                    raise ValueError("active intraday dataset declaration is invalid")
                if (market, asset_class, frequency) in {
                    (item.market, item.asset_class, item.frequency) for item in intraday
                }:
                    raise ValueError("duplicate active intraday scope")
                intraday.append(
                    IntradayDatasetScope(
                        dataset_key=row.dataset_key,
                        market=market,
                        asset_class=asset_class,
                        frequency=cast(Literal["minute", "tick"], frequency),
                        record_kind=cast(
                            Literal["bar_1m", "tick"], declaration.defaults.record_kind
                        ),
                    )
                )
                continue
            if schema_id == "exchange_daily_quote":
                # The official table is not a read model of its own; its values are
                # served through the market_data_eod rows they correct.
                if (
                    set(declaration.accepted_schema_versions) != {1}
                    or declaration.defaults.market != market
                    or declaration.defaults.asset_class != asset_class
                    or (market, asset_class) not in EXCHANGE_QUOTE_ACTIVE_SCOPES
                    or frequency != "daily"
                ):
                    raise ValueError("active exchange quote dataset declaration is invalid")
                if any((item.market, item.asset_class) == (market, asset_class) for item in quotes):
                    raise ValueError("duplicate active exchange quote scope")
                quotes.append(ExchangeQuoteScope(row.dataset_key, market, asset_class))
                continue
            if not set(declaration.accepted_schema_versions) <= SUPPORTED_SERVE_VERSIONS.get(
                schema_id, frozenset()
            ):
                raise ValueError("contract version does not have a supported Serve read model")
            if (
                declaration.defaults.market != market
                or declaration.defaults.asset_class != asset_class
            ):
                raise ValueError("registry and contract scopes differ")
            if schema_id == "market_eod":
                expected_frequency, data_kind, interval = "daily", "eod", "1d"
            elif schema_id == "market_minute":
                expected_frequency, data_kind, interval = "minute", "minute", "1m"
            elif schema_id == "futures_eod":
                expected_frequency, data_kind, interval = "daily", "futures_eod", "1d"
            else:
                raise ValueError("active dataset does not have a Serve read model")
            if frequency != expected_frequency:
                raise ValueError("registry frequency does not match schema")
            identity = (schema_id, market, asset_class)
            if identity not in SUPPORTED_ACTIVE_SCOPES:
                raise ValueError("active dataset is outside the supported Serve scope")
            # Several datasets may write one canonical scope (index groups fetched at
            # different times): reads go by market and asset class, never by dataset.
            scopes.append(
                ActiveDatasetScope(
                    dataset_key=row.dataset_key,
                    market=market,
                    asset_class=asset_class,
                    frequency=cast(Literal["daily", "minute"], expected_frequency),
                    data_kind=cast(DataKind, data_kind),
                    interval=cast(Literal["1d", "1m"], interval),
                )
            )
        except (AttributeError, TypeError, ValueError):
            raise configuration_error() from None
    return tuple(scopes), tuple(intraday), tuple(quotes)


def _pairs(
    scopes: tuple[ActiveDatasetScope, ...], kind: DataKind | None = None
) -> set[tuple[str, str]]:
    return {
        (scope.market, scope.asset_class)
        for scope in scopes
        if kind is None or scope.data_kind == kind
    }


def _instrument_scope_filter(pairs: set[tuple[str, str]]) -> ColumnElement[bool]:
    if not pairs:
        return literal_column("false")
    return or_(
        *(
            and_(Instrument.market == market, Instrument.asset_class == asset_class)
            for market, asset_class in sorted(pairs)
        )
    )


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized or None


def _normalize_filter(value: str | None) -> str | None:
    normalized = _normalize(value)
    return normalized.casefold() if normalized else None


def _normalized_db_text(column: LookupColumn) -> ColumnElement[str]:
    return func.normalize(sql_cast(column, String), literal_column("NFKC"))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _literal_like_pattern(value: str) -> str:
    return f"%{_escape_like(value)}%"


def _instrument_response(
    instrument: Instrument,
    stats: InstrumentStats | None,
    eod_pairs: set[tuple[str, str]],
    minute_pairs: set[tuple[str, str]],
    futures_pairs: set[tuple[str, str]],
    intraday: dict[tuple[UUID, str], IntradayCoverageResponse] | None = None,
) -> InstrumentResponse:
    pair = (instrument.market, instrument.asset_class)
    eod = None
    minute = None
    futures = None
    if stats is not None and pair in eod_pairs and stats.eod_first_date and stats.eod_latest_date:
        eod = EODCoverageResponse(
            first_date=stats.eod_first_date,
            latest_date=stats.eod_latest_date,
            latest_close=stats.eod_latest_close,
        )
    if (
        stats is not None
        and pair in minute_pairs
        and stats.minute_first_bar_at
        and stats.minute_latest_bar_at
        and stats.minute_latest_close is not None
    ):
        minute = MinuteCoverageResponse(
            first_bar_at=stats.minute_first_bar_at,
            latest_bar_at=stats.minute_latest_bar_at,
            latest_close=stats.minute_latest_close,
        )
    if (
        stats is not None
        and pair in futures_pairs
        and stats.futures_first_date
        and stats.futures_latest_date
    ):
        futures = EODCoverageResponse(
            first_date=stats.futures_first_date,
            latest_date=stats.futures_latest_date,
            latest_close=None,
        )
    return InstrumentResponse(
        instrument_id=instrument.instrument_id,
        asset_class=instrument.asset_class,
        market=instrument.market,
        symbol=instrument.symbol,
        name=instrument.name,
        currency=instrument.currency,
        timezone=instrument.timezone,
        status=instrument.status,
        listed_date=instrument.listed_date,
        delisted_date=instrument.delisted_date,
        coverage=InstrumentCoverageResponse(
            eod=eod,
            minute=minute,
            futures=futures,
            intraday_bars=(intraday or {}).get((instrument.instrument_id, "bar_1m")),
            intraday_ticks=(intraday or {}).get((instrument.instrument_id, "tick")),
        ),
    )


async def list_datasets(db: AsyncSession) -> DatasetListResponse:
    from app.services.serve_intraday import intraday_dataset_coverage

    scopes, intraday_scopes, quote_scopes = await _active_registry_scopes(db)
    items: list[DatasetResponse] = []
    for intraday_scope in intraday_scopes:
        first, latest, symbols = await intraday_dataset_coverage(db, intraday_scope.dataset_key)
        bars = intraday_scope.record_kind == "bar_1m"
        items.append(
            DatasetResponse(
                dataset_key=intraday_scope.dataset_key,
                display_name=(
                    f"{intraday_scope.market} {intraday_scope.asset_class.upper()} "
                    + ("1m bars" if bars else "ticks")
                ),
                market=intraday_scope.market,
                asset_class=intraday_scope.asset_class,
                frequency=intraday_scope.frequency,
                data_kind="intraday_bars" if bars else "intraday_ticks",
                interval="1m" if bars else "tick",
                operation_id=DATASET_OPERATIONS["intraday_bars" if bars else "intraday_ticks"],
                volume_unit="lot",
                availability="available" if symbols else "configured_empty",
                coverage=DatasetCoverageResponse(
                    coverage_start_date=first,
                    coverage_end_date=latest,
                    instrument_count=symbols,
                ),
            )
        )
    shared = Counter((scope.data_kind, scope.market, scope.asset_class) for scope in scopes)
    for scope in scopes:
        table: Any
        if scope.data_kind == "eod":
            table = MarketDataEOD
            date_column = MarketDataEOD.trade_date
        elif scope.data_kind == "futures_eod":
            table = FuturesContractEOD
            date_column = FuturesContractEOD.trade_date
        else:
            table = MarketDataMinute
            date_column = MarketDataMinute.trade_date
        row = (
            await db.execute(
                select(
                    func.min(date_column),
                    func.max(date_column),
                    func.count(func.distinct(table.instrument_id)),
                )
                .select_from(table)
                .join(Instrument, table.instrument_id == Instrument.instrument_id)
                .where(
                    Instrument.market == scope.market,
                    Instrument.asset_class == scope.asset_class,
                    # A scope two datasets share counts each dataset's own writes.
                    *(
                        (
                            table.run_id.in_(
                                select(IngestionRun.run_id).where(
                                    IngestionRun.dataset_key == scope.dataset_key
                                )
                            ),
                        )
                        if shared[(scope.data_kind, scope.market, scope.asset_class)] > 1
                        else ()
                    ),
                )
            )
        ).one()
        first_date, latest_date, instrument_count = row
        items.append(
            DatasetResponse(
                dataset_key=scope.dataset_key,
                display_name=f"{scope.market} {scope.asset_class.upper()} {scope.interval}",
                market=scope.market,
                asset_class=scope.asset_class,
                frequency=scope.frequency,
                data_kind=scope.data_kind,
                interval=scope.interval,
                operation_id=DATASET_OPERATIONS[scope.data_kind],
                volume_unit=(
                    eod_volume_unit(scope.asset_class)
                    if scope.data_kind == "eod"
                    else "contract"
                    if scope.data_kind == "futures_eod"
                    else "share"
                ),
                availability="available" if instrument_count else "configured_empty",
                coverage=DatasetCoverageResponse(
                    coverage_start_date=first_date,
                    coverage_end_date=latest_date,
                    instrument_count=int(instrument_count or 0),
                ),
            )
        )
    for quote_scope in quote_scopes:
        first_date, latest_date, instrument_count = (
            await db.execute(
                select(
                    func.min(ExchangeDailyQuote.trade_date),
                    func.max(ExchangeDailyQuote.trade_date),
                    func.count(func.distinct(ExchangeDailyQuote.instrument_id)),
                )
                .join(Instrument, ExchangeDailyQuote.instrument_id == Instrument.instrument_id)
                .where(
                    Instrument.market == quote_scope.market,
                    Instrument.asset_class == quote_scope.asset_class,
                )
            )
        ).one()
        items.append(
            DatasetResponse(
                dataset_key=quote_scope.dataset_key,
                display_name=(
                    f"{quote_scope.market} {quote_scope.asset_class.upper()} exchange daily quotes"
                ),
                market=quote_scope.market,
                asset_class=quote_scope.asset_class,
                frequency="daily",
                data_kind="exchange_quotes",
                interval="1d",
                operation_id=DATASET_OPERATIONS["exchange_quotes"],
                volume_unit="share",
                availability="available" if instrument_count else "configured_empty",
                coverage=DatasetCoverageResponse(
                    coverage_start_date=first_date,
                    coverage_end_date=latest_date,
                    instrument_count=int(instrument_count or 0),
                ),
            )
        )
    return DatasetListResponse(data=sorted(items, key=lambda item: item.dataset_key))


async def list_calendar(
    db: AsyncSession,
    *,
    market: str,
    start_date: date | None,
    end_date: date | None,
    is_open: bool | None,
    page: int,
    page_size: int,
) -> CalendarListResponse:
    from app.services.effective_calendar import effective_days
    from app.utils import utc_now

    year = (start_date or end_date or utc_now().date()).year
    start_date = start_date or date(year, 1, 1)
    end_date = end_date or date(year, 12, 31)
    if (end_date - start_date).days > 3660:
        raise serve_error(422, "INVALID_DATE_RANGE", "Calendar range may cover at most 3661 dates")
    try:
        days = await effective_days(db, market, start_date, end_date)
    except ValueError as exc:
        raise serve_error(422, "INVALID_PARAMETER", str(exc)) from exc
    selected = [day for day in days if is_open is None or day["is_open"] == is_open]
    total_records = len(selected)
    return CalendarListResponse(
        data=[
            CalendarResponse(**day) for day in selected[(page - 1) * page_size : page * page_size]
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=(total_records + page_size - 1) // page_size,
        ),
    )


async def list_instruments(
    db: AsyncSession,
    *,
    q: str | None,
    market: str | None,
    asset_class: str | None,
    status: str | None,
    has_eod: bool | None,
    has_minute: bool | None,
    sort_by: InstrumentSort | None,
    sort_dir: SortDirection,
    page: int,
    page_size: int,
) -> InstrumentListResponse:
    scopes = await active_dataset_scopes(db)
    all_pairs = _pairs(scopes)
    eod_pairs = _pairs(scopes, "eod")
    minute_pairs = _pairs(scopes, "minute")
    futures_pairs = _pairs(scopes, "futures_eod")
    scope_filter = _instrument_scope_filter(all_pairs)
    eod_coverage_available = and_(
        _instrument_scope_filter(eod_pairs),
        InstrumentStats.eod_first_date.is_not(None),
        InstrumentStats.eod_latest_date.is_not(None),
    )
    minute_coverage_available = and_(
        _instrument_scope_filter(minute_pairs),
        InstrumentStats.minute_first_bar_at.is_not(None),
        InstrumentStats.minute_latest_bar_at.is_not(None),
        InstrumentStats.minute_latest_close.is_not(None),
    )
    query = (
        select(Instrument, InstrumentStats)
        .outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
        .where(scope_filter)
    )
    count_query = (
        select(func.count())
        .select_from(Instrument)
        .outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
        .where(scope_filter)
    )
    filters: list[ColumnElement[bool]] = []
    normalized_q = _normalize(q)
    if normalized_q:
        pattern = _literal_like_pattern(normalized_q)
        text_matches: list[ColumnElement[bool]] = [
            _normalized_db_text(Instrument.symbol).ilike(pattern, escape="\\"),
            _normalized_db_text(Instrument.name).ilike(pattern, escape="\\"),
        ]
        # Only a whole id matches: digits inside random UUIDs would flood code searches.
        if (wanted_id := _parse_uuid(normalized_q)) is not None:
            text_matches.append(Instrument.instrument_id == wanted_id)
        filters.append(or_(*text_matches))
    normalized_market = _normalize_filter(market)
    normalized_asset = _normalize_filter(asset_class)
    normalized_status = _normalize_filter(status)
    if normalized_market:
        filters.append(func.lower(Instrument.market) == normalized_market)
    if normalized_asset:
        filters.append(func.lower(Instrument.asset_class) == normalized_asset)
    if normalized_status:
        filters.append(func.lower(Instrument.status) == normalized_status)
    if has_eod is not None:
        filters.append(eod_coverage_available if has_eod else ~eod_coverage_available)
    if has_minute is not None:
        filters.append(minute_coverage_available if has_minute else ~minute_coverage_available)
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)
    total_records = int((await db.execute(count_query)).scalar_one())
    total_pages = (total_records + page_size - 1) // page_size
    sort_columns: dict[InstrumentSort, LookupColumn] = {
        "market": Instrument.market,
        "symbol": Instrument.symbol,
        "name": Instrument.name,
        "asset_class": Instrument.asset_class,
        "currency": Instrument.currency,
        "status": Instrument.status,
        "eod_first_date": case(
            (eod_coverage_available, InstrumentStats.eod_first_date), else_=None
        ),
        "eod_latest_date": case(
            (eod_coverage_available, InstrumentStats.eod_latest_date), else_=None
        ),
        "eod_latest_close": case(
            (eod_coverage_available, InstrumentStats.eod_latest_close), else_=None
        ),
        "minute_first_bar_at": case(
            (minute_coverage_available, InstrumentStats.minute_first_bar_at), else_=None
        ),
        "minute_latest_bar_at": case(
            (minute_coverage_available, InstrumentStats.minute_latest_bar_at), else_=None
        ),
        "minute_latest_close": case(
            (minute_coverage_available, InstrumentStats.minute_latest_close), else_=None
        ),
        "futures_first_date": case(
            (_instrument_scope_filter(futures_pairs), InstrumentStats.futures_first_date),
            else_=None,
        ),
        "futures_latest_date": case(
            (_instrument_scope_filter(futures_pairs), InstrumentStats.futures_latest_date),
            else_=None,
        ),
    }
    if sort_by is None:
        sort_by = "relevance" if normalized_q else "market"
    if sort_by == "relevance" and normalized_q:
        order = [*_relevance_order(normalized_q), Instrument.market.asc(), Instrument.symbol.asc()]
    else:
        column = sort_columns["market" if sort_by == "relevance" else sort_by]
        order = [(column.desc() if sort_dir == "desc" else column.asc()).nulls_last()]
    rows = (
        await db.execute(
            query.order_by(*order, Instrument.instrument_id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    facets = await _instrument_facets(db, all_pairs)
    intraday = await _intraday_coverage(db, [instrument for instrument, _ in rows])
    return InstrumentListResponse(
        data=[
            _instrument_response(
                instrument, stats, eod_pairs, minute_pairs, futures_pairs, intraday
            )
            for instrument, stats in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
        facets=facets,
    )


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _relevance_order(normalized_q: str) -> list[ColumnElement[Any]]:
    """Exact symbol, symbol prefix, name prefix, symbol substring, then any other match.

    Shorter symbols come first within a rank, so ``2330`` precedes ``23301``.
    """
    needle = normalized_q.upper()
    symbol = func.upper(_normalized_db_text(Instrument.symbol))
    name = func.upper(_normalized_db_text(Instrument.name))
    prefix = f"{_escape_like(needle)}%"
    rank = case(
        (symbol == needle, 0),
        (symbol.like(prefix, escape="\\"), 1),
        (name.like(prefix, escape="\\"), 2),
        (symbol.like(f"%{prefix}", escape="\\"), 3),
        else_=4,
    )
    return [rank.asc(), func.length(Instrument.symbol).asc()]


async def resolve_instruments(
    db: AsyncSession, *, symbols_raw: str, market: str | None
) -> InstrumentResolveResponse:
    """Look up each ``MARKET:SYMBOL`` exactly; a miss never fails the whole request."""
    selectors = parse_selectors(symbols_raw, param="symbols", default_market=market)
    scopes = await active_dataset_scopes(db)
    all_pairs = _pairs(scopes)
    wanted = {(selector.market, selector.symbol) for selector in selectors}
    rows = (
        await db.execute(
            select(Instrument, InstrumentStats)
            .outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
            .where(
                tuple_(Instrument.market, Instrument.symbol).in_(sorted(wanted)),
            )
            .order_by(Instrument.asset_class.asc(), Instrument.instrument_id.asc())
        )
    ).all()
    served = [
        (instrument, stats)
        for instrument, stats in rows
        if (instrument.market, instrument.asset_class) in all_pairs
    ]
    intraday = await _intraday_coverage(db, [instrument for instrument, _ in served])
    eod_pairs = _pairs(scopes, "eod")
    minute_pairs = _pairs(scopes, "minute")
    futures_pairs = _pairs(scopes, "futures_eod")
    known = {(instrument.market, instrument.symbol) for instrument, _ in rows}
    results: list[InstrumentResolution] = []
    for selector in selectors:
        key = (selector.market, selector.symbol)
        matches = [
            _instrument_response(
                instrument, stats, eod_pairs, minute_pairs, futures_pairs, intraday
            )
            for instrument, stats in served
            if (instrument.market, instrument.symbol) == key
        ]
        status: Literal["resolved", "ambiguous", "not_found", "not_available"]
        if len(matches) == 1:
            status = "resolved"
        elif matches:
            status = "ambiguous"
        elif key in known:
            status = "not_available"
        else:
            status = "not_found"
        results.append(
            InstrumentResolution(
                input=selector.input,
                market=cast(str, selector.market),
                symbol=cast(str, selector.symbol),
                status=status,
                matches=matches,
            )
        )
    return InstrumentResolveResponse(data=results)


async def _instrument_facets(
    db: AsyncSession, pairs: set[tuple[str, str]]
) -> InstrumentFacetsResponse:
    scope_filter = _instrument_scope_filter(pairs)

    async def values(column: LookupColumn) -> list[str]:
        result = await db.execute(
            select(column)
            .where(scope_filter, column.is_not(None))
            .distinct()
            .order_by(column.asc())
        )
        return [str(value) for value in result.scalars().all()]

    return InstrumentFacetsResponse(
        markets=await values(Instrument.market),
        asset_classes=await values(Instrument.asset_class),
        statuses=await values(Instrument.status),
    )


async def get_instrument(db: AsyncSession, instrument_id: UUID) -> InstrumentDetailResponse:
    scopes = await active_dataset_scopes(db)
    all_pairs = _pairs(scopes)
    row = (
        await db.execute(
            select(Instrument, InstrumentStats)
            .outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
            .where(
                Instrument.instrument_id == instrument_id,
                _instrument_scope_filter(all_pairs),
            )
        )
    ).one_or_none()
    if row is None:
        raise serve_error(404, "INSTRUMENT_NOT_FOUND", "Instrument not found")
    instrument, stats = row
    return InstrumentDetailResponse(
        data=_instrument_response(
            instrument,
            stats,
            _pairs(scopes, "eod"),
            _pairs(scopes, "minute"),
            _pairs(scopes, "futures_eod"),
            await _intraday_coverage(db, [instrument]),
        )
    )


async def _intraday_coverage(
    db: AsyncSession, instruments: list[Instrument]
) -> dict[tuple[UUID, str], IntradayCoverageResponse]:
    from app.services.serve_intraday import intraday_instrument_coverage

    scopes = await active_intraday_datasets(db)
    coverage = await intraday_instrument_coverage(db, scopes, instruments)
    return {(instrument_id, kind): value for (instrument_id, kind), value in coverage.items()}


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_CURSOR_BYTES:
        raise serve_error(422, "INVALID_CURSOR", "Cursor payload is too large")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    if (
        not cursor
        or len(cursor) > MAX_CURSOR_LENGTH
        or BASE64URL_CURSOR_PATTERN.fullmatch(cursor) is None
    ):
        raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        if len(raw) > MAX_CURSOR_BYTES:
            raise ValueError
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
    if not isinstance(payload, dict):
        raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid")
    return payload


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _validate_dates(start_date: date | None, end_date: date | None) -> None:
    if start_date and end_date and start_date > end_date:
        raise serve_error(422, "INVALID_DATE_RANGE", "start_date must not be after end_date")


# Volume unit of each asset class in canonical daily bars.
EOD_VOLUME_UNITS: dict[str, VolumeUnit] = {"equity": "share", "etf": "share"}
DATASET_OPERATIONS = {
    "eod": "list_eod",
    "minute": "list_minute_bars",
    "futures_eod": "list_futures_eod",
    "intraday_bars": "list_intraday_bars",
    "intraday_ticks": "list_intraday_ticks",
    "exchange_quotes": "list_exchange_quotes",
}


def eod_volume_unit(asset_class: str) -> VolumeUnit:
    return EOD_VOLUME_UNITS.get(asset_class, "provider_defined")


def _eod_units(eod_pairs: set[tuple[str, str]]) -> EODUnitsResponse:
    return EODUnitsResponse(
        volume_by_asset_class={
            asset_class: eod_volume_unit(asset_class)
            for asset_class in sorted({asset_class for _, asset_class in eod_pairs})
        }
    )


@dataclass(frozen=True)
class _SelectorMatch:
    selector: InstrumentSelector
    instrument: Instrument | None
    reason: Literal["not_found", "ambiguous", "not_available"] | None
    candidates: tuple[Instrument, ...] = ()


async def _match_selectors(
    db: AsyncSession, selectors: list[InstrumentSelector], pairs: set[tuple[str, str]]
) -> list[_SelectorMatch]:
    """Match each selector to the one served instrument it names, or say why not."""
    ids = [selector.instrument_id for selector in selectors if selector.instrument_id]
    keys = sorted({(selector.market, selector.symbol) for selector in selectors if selector.market})
    conditions: list[ColumnElement[bool]] = []
    if ids:
        conditions.append(Instrument.instrument_id.in_(ids))
    if keys:
        conditions.append(tuple_(Instrument.market, Instrument.symbol).in_(keys))
    rows = (
        (
            await db.execute(
                select(Instrument)
                .where(or_(*conditions))
                .order_by(Instrument.asset_class.asc(), Instrument.instrument_id.asc())
            )
        )
        .scalars()
        .all()
    )
    matches: list[_SelectorMatch] = []
    for selector in selectors:
        candidates = [
            row
            for row in rows
            if (
                row.instrument_id == selector.instrument_id
                if selector.instrument_id
                else (row.market, row.symbol) == (selector.market, selector.symbol)
            )
        ]
        served = tuple(row for row in candidates if (row.market, row.asset_class) in pairs)
        if len(served) == 1:
            matches.append(_SelectorMatch(selector, served[0], None))
        elif served:
            matches.append(_SelectorMatch(selector, None, "ambiguous", served))
        else:
            reason: Literal["not_found", "not_available"] = (
                "not_available" if candidates else "not_found"
            )
            matches.append(_SelectorMatch(selector, None, reason))
    return matches


def _require_matched(
    matches: list[_SelectorMatch], data_label: str = "EOD data"
) -> list[Instrument]:
    """Fail the request on the first kind of unmatched selector, naming every such input."""
    for reason, status, code, message in (
        ("not_found", 404, "INSTRUMENT_NOT_FOUND", "No instrument matches"),
        ("ambiguous", 422, "AMBIGUOUS_INSTRUMENT", "Several instruments match"),
        ("not_available", 422, "DATASET_NOT_AVAILABLE", f"{data_label} is not available for"),
    ):
        failed = [match for match in matches if match.reason == reason]
        if failed:
            details: dict[str, Any] = {"inputs": [match.selector.input for match in failed]}
            if reason == "ambiguous":
                details["matches"] = {
                    match.selector.input: [
                        {"instrument_id": str(row.instrument_id), "asset_class": row.asset_class}
                        for row in match.candidates
                    ]
                    for match in failed
                }
            raise serve_error(
                status, code, f"{message}: {', '.join(details['inputs'])}", details=details
            )
    return [match.instrument for match in matches if match.instrument is not None]


def _instrument_summary(instrument: Instrument) -> InstrumentSummaryResponse:
    return InstrumentSummaryResponse(
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        name=instrument.name,
        market=instrument.market,
        asset_class=instrument.asset_class,
        currency=instrument.currency,
    )


async def list_eod(
    db: AsyncSession,
    *,
    instruments_raw: str | None,
    market: str | None,
    start_date: date | None,
    end_date: date | None,
    order: SortDirection,
    cursor: str | None,
    page_size: int,
) -> EODListResponse:
    _validate_dates(start_date, end_date)
    normalized_market_value = _normalize(market)
    normalized_market = normalized_market_value.upper() if normalized_market_value else None
    if (instruments_raw is None) == (normalized_market is None):
        raise serve_error(
            422, "QUERY_SCOPE_REQUIRED", "Provide instruments or market, but not both"
        )
    scopes = await active_dataset_scopes(db)
    eod_pairs = _pairs(scopes, "eod")
    instrument_ids: list[UUID] | None = None
    if instruments_raw is not None:
        selectors = parse_selectors(instruments_raw, param="instruments", allow_uuid=True)
        matched = _require_matched(await _match_selectors(db, selectors, eod_pairs))
        instrument_ids = sorted({instrument.instrument_id for instrument in matched})
    else:
        if not any(pair[0] == normalized_market for pair in eod_pairs):
            raise serve_error(
                422, "DATASET_NOT_AVAILABLE", "EOD data is not available for this market"
            )
    scope = {
        "instrument_ids": [str(value) for value in instrument_ids] if instrument_ids else None,
        "market": normalized_market,
        "start_date": _date_text(start_date),
        "end_date": _date_text(end_date),
        "order": order,
    }
    last_date: date | None = None
    last_id: UUID | None = None
    if cursor:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "scope", "last"}
            or payload.get("v") != 2
            or payload.get("kind") != "eod"
            or payload.get("scope") != scope
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        last = payload.get("last")
        if not isinstance(last, dict) or set(last) != {"trade_date", "instrument_id"}:
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid")
        try:
            last_date = date.fromisoformat(last["trade_date"])
            last_id = UUID(last["instrument_id"])
        except (KeyError, TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
    query = select(MarketDataEOD, Instrument).join(
        Instrument, MarketDataEOD.instrument_id == Instrument.instrument_id
    )
    if instrument_ids is not None:
        query = query.where(MarketDataEOD.instrument_id.in_(instrument_ids))
    else:
        query = query.where(
            Instrument.market == normalized_market,
            _instrument_scope_filter({pair for pair in eod_pairs if pair[0] == normalized_market}),
        )
    if start_date:
        query = query.where(MarketDataEOD.trade_date >= start_date)
    if end_date:
        query = query.where(MarketDataEOD.trade_date <= end_date)
    ascending = order == "asc"
    if last_date and last_id:
        past_date = (
            MarketDataEOD.trade_date > last_date
            if ascending
            else MarketDataEOD.trade_date < last_date
        )
        query = query.where(
            or_(
                past_date,
                and_(MarketDataEOD.trade_date == last_date, MarketDataEOD.instrument_id > last_id),
            )
        )
    date_order = MarketDataEOD.trade_date.asc() if ascending else MarketDataEOD.trade_date.desc()
    rows = (
        await db.execute(
            query.order_by(date_order, MarketDataEOD.instrument_id.asc()).limit(page_size + 1)
        )
    ).all()
    next_cursor = None
    if len(rows) > page_size:
        last_eod = rows[page_size - 1][0]
        next_cursor = _encode_cursor(
            {
                "v": 2,
                "kind": "eod",
                "scope": scope,
                "last": {
                    "trade_date": last_eod.trade_date.isoformat(),
                    "instrument_id": str(last_eod.instrument_id),
                },
            }
        )
        rows = rows[:page_size]
    return EODListResponse(
        units=_eod_units(eod_pairs),
        data=[
            EODResponse(
                instrument_id=eod.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                market=instrument.market,
                currency=instrument.currency,
                trade_date=eod.trade_date,
                open=eod.open,
                high=eod.high,
                low=eod.low,
                close=eod.close,
                volume=eod.volume,
                total_ticks=eod.total_ticks,
                turnover=eod.turnover,
                source=eod.source,
                field_sources=eod.field_sources or None,
                source_fetched_at=eod.source_fetched_at,
                asof_ts=eod.asof_ts,
                created_at=eod.created_at,
                updated_at=eod.updated_at,
            )
            for eod, instrument in rows
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
    )


async def _open_days_between_bars(
    db: AsyncSession,
    matches: list[_SelectorMatch],
    recent_rows: dict[UUID, list[MarketDataEOD]],
) -> dict[str, set[date] | None]:
    """Open dates of each market spanning its instruments' last two bars; None without a calendar."""
    from app.services.effective_calendar import effective_days

    spans: dict[str, tuple[date, date]] = {}
    for match in matches:
        if match.instrument is None:
            continue
        dates = [row.trade_date for row in recent_rows.get(match.instrument.instrument_id, [])]
        if len(dates) < 2:
            continue
        low, high = spans.get(match.instrument.market, (min(dates), max(dates)))
        spans[match.instrument.market] = (min(low, *dates), max(high, *dates))
    open_days: dict[str, set[date] | None] = {}
    for market, (start, end) in spans.items():
        try:
            days = await effective_days(db, market, start, end)
        except ValueError:
            open_days[market] = None
            continue
        open_days[market] = {
            date.fromisoformat(day["trade_date"]) for day in days if day["is_open"]
        }
    return open_days


def _sessions_between(open_days: set[date] | None, previous: date, latest: date) -> int | None:
    if open_days is None:
        return None
    return sum(1 for day in open_days if previous < day < latest)


def _change_percent(change: Decimal, previous: Decimal) -> Decimal | None:
    if previous == 0:
        return None
    return (change / previous * 100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


async def latest_eod(
    db: AsyncSession, *, instruments_raw: str, as_of_date: date | None
) -> EODLatestResponse:
    """Latest daily bar of each instrument with the change from the bar before it."""
    scopes = await active_dataset_scopes(db)
    eod_pairs = _pairs(scopes, "eod")
    selectors = parse_selectors(instruments_raw, param="instruments", allow_uuid=True)
    matches = await _match_selectors(db, selectors, eod_pairs)
    ids = sorted(
        {match.instrument.instrument_id for match in matches if match.instrument is not None}
    )
    recent_rows: dict[UUID, list[MarketDataEOD]] = {}
    if ids:
        wanted = (
            select(Instrument.instrument_id.label("wanted_id"))
            .where(Instrument.instrument_id.in_(ids))
            .subquery()
        )
        recent_query = select(MarketDataEOD).where(
            MarketDataEOD.instrument_id == wanted.c.wanted_id
        )
        if as_of_date is not None:
            recent_query = recent_query.where(MarketDataEOD.trade_date <= as_of_date)
        recent = recent_query.order_by(MarketDataEOD.trade_date.desc()).limit(2).lateral("recent")
        recent_eod = aliased(MarketDataEOD, recent)
        for row in (
            (await db.execute(select(recent_eod).select_from(wanted).join(recent, true())))
            .scalars()
            .all()
        ):
            recent_rows.setdefault(row.instrument_id, []).append(row)
    open_days = await _open_days_between_bars(db, matches, recent_rows)
    data: list[EODLatestQuote] = []
    missing: list[EODLatestMissing] = []
    for match in matches:
        if match.instrument is None:
            missing.append(
                EODLatestMissing(
                    input=match.selector.input,
                    reason=cast(Literal["not_found", "ambiguous", "not_available"], match.reason),
                    matches=[_instrument_summary(row) for row in match.candidates],
                )
            )
            continue
        bars = sorted(
            recent_rows.get(match.instrument.instrument_id, []),
            key=lambda row: row.trade_date,
            reverse=True,
        )
        if not bars:
            missing.append(
                EODLatestMissing(input=match.selector.input, reason="no_data", matches=[])
            )
            continue
        latest = bars[0]
        previous = bars[1] if len(bars) > 1 else None
        missing_sessions = (
            _sessions_between(
                open_days.get(match.instrument.market), previous.trade_date, latest.trade_date
            )
            if previous is not None
            else None
        )
        # A change across skipped sessions is not the day's move, so it is not reported.
        change = (
            latest.close - previous.close
            if previous is not None
            and missing_sessions == 0
            and latest.close is not None
            and previous.close is not None
            else None
        )
        data.append(
            EODLatestQuote(
                input=match.selector.input,
                instrument=_instrument_summary(match.instrument),
                trade_date=latest.trade_date,
                open=latest.open,
                high=latest.high,
                low=latest.low,
                close=latest.close,
                volume=latest.volume,
                turnover=latest.turnover,
                previous_trade_date=previous.trade_date if previous else None,
                previous_close=previous.close if previous else None,
                missing_sessions=missing_sessions,
                change=change,
                change_percent=(
                    _change_percent(change, cast(Decimal, previous.close))
                    if change is not None and previous is not None
                    else None
                ),
                source=latest.source,
                field_sources=latest.field_sources or None,
            )
        )
    return EODLatestResponse(
        as_of_date=as_of_date, units=_eod_units(eod_pairs), data=data, missing=missing
    )


def _shift_month_back(value: date) -> date:
    year = value.year - 1 if value.month == 1 else value.year
    month = 12 if value.month == 1 else value.month - 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _shift_years(value: date, years: int) -> date:
    target_year = value.year + years
    return date(
        target_year, value.month, min(value.day, calendar.monthrange(target_year, value.month)[1])
    )


def _valid_minute_range(start_date: date, end_date: date) -> bool:
    if start_date > end_date:
        return False
    year_delta = end_date.year - start_date.year
    if year_delta < 5:
        return True
    if year_delta > 5:
        return False
    return end_date <= _shift_years(start_date, 5)


async def list_minute(
    db: AsyncSession,
    *,
    instrument_id: UUID,
    start_date: date | None,
    end_date: date | None,
    cursor: str | None,
    page_size: int,
) -> MinuteListResponse:
    if (start_date is None) != (end_date is None):
        raise serve_error(
            422, "INVALID_DATE_RANGE", "start_date and end_date must be provided together"
        )
    if start_date and end_date:
        if not _valid_minute_range(start_date, end_date):
            raise serve_error(
                422,
                "INVALID_DATE_RANGE",
                "Minute range must be ordered and no longer than five calendar years",
            )
    scopes = await active_dataset_scopes(db)
    instrument = await db.get(Instrument, instrument_id)
    if instrument is None:
        raise serve_error(404, "INSTRUMENT_NOT_FOUND", "Instrument not found")
    if (instrument.market, instrument.asset_class) not in _pairs(scopes, "minute"):
        raise serve_error(
            422, "DATASET_NOT_AVAILABLE", "Minute data is not available for this instrument"
        )
    anchor: Literal["explicit", "latest_available"] = (
        "explicit" if start_date else "latest_available"
    )
    last_at: datetime | None = None
    if cursor:
        payload = _decode_cursor(cursor)
        if (
            set(payload)
            != {
                "v",
                "kind",
                "instrument_id",
                "start_date",
                "end_date",
                "anchor",
                "last_bar_start_time",
            }
            or payload.get("v") != 1
            or payload.get("kind") != "minute"
            or payload.get("instrument_id") != str(instrument_id)
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        try:
            cursor_start = date.fromisoformat(payload["start_date"])
            cursor_end = date.fromisoformat(payload["end_date"])
            cursor_anchor = payload["anchor"]
            if cursor_anchor not in ("explicit", "latest_available"):
                raise ValueError
            last_at = datetime.fromisoformat(payload["last_bar_start_time"])
            if last_at.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
        if not _valid_minute_range(cursor_start, cursor_end) or (
            cursor_anchor == "latest_available" and cursor_start != _shift_month_back(cursor_end)
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor contains an invalid date range")
        if start_date is not None and (start_date != cursor_start or end_date != cursor_end):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        start_date, end_date = cursor_start, cursor_end
        anchor = cast(Literal["explicit", "latest_available"], cursor_anchor)
    elif start_date is None:
        end_date = await db.scalar(
            select(func.max(MarketDataMinute.trade_date)).where(
                MarketDataMinute.instrument_id == instrument_id
            )
        )
        if end_date is None:
            return MinuteListResponse(
                data=[],
                pagination=CursorPaginationInfo(page_size=page_size),
                range=ResolvedDateRangeResponse(anchor="latest_available"),
            )
        start_date = _shift_month_back(end_date)
    assert start_date is not None and end_date is not None
    query = select(MarketDataMinute).where(
        MarketDataMinute.instrument_id == instrument_id,
        MarketDataMinute.trade_date >= start_date,
        MarketDataMinute.trade_date <= end_date,
    )
    if last_at:
        query = query.where(MarketDataMinute.bar_start_time < last_at)
    records = (
        (
            await db.execute(
                query.order_by(MarketDataMinute.bar_start_time.desc()).limit(page_size + 1)
            )
        )
        .scalars()
        .all()
    )
    next_cursor = None
    if len(records) > page_size:
        last_record = records[page_size - 1]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "minute",
                "instrument_id": str(instrument_id),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "anchor": anchor,
                "last_bar_start_time": last_record.bar_start_time.isoformat(),
            }
        )
        records = records[:page_size]
    return MinuteListResponse(
        data=[
            MinuteResponse(
                instrument_id=record.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                market=instrument.market,
                trade_date=record.trade_date,
                bar_start_time=record.bar_start_time,
                bar_end_time=record.bar_end_time,
                signal_time=record.signal_time,
                market_timezone=record.market_timezone,
                open=record.open,
                high=record.high,
                low=record.low,
                close=record.close,
                volume=record.volume,
                turnover=record.turnover,
                trade_count=record.trade_count,
                price_adjustment=record.price_adjustment,
                source=record.source,
                source_fetched_at=record.source_fetched_at,
                asof_ts=record.asof_ts,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            for record in records
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
        range=ResolvedDateRangeResponse(
            start_date=start_date if records else None,
            end_date=end_date if records else None,
            anchor=anchor,
        ),
    )


async def list_futures_eod(
    db: AsyncSession,
    *,
    product_code: str | None,
    contract_code: str | None,
    start_date: date | None,
    end_date: date | None,
    session: str | None,
    order: SortDirection,
    cursor: str | None,
    page_size: int,
) -> FuturesEODListResponse:
    """Read actual expiry/session quotes with a query-bound stable cursor."""
    scopes = await active_dataset_scopes(db)
    if ("TW", "future") not in _pairs(scopes, "futures_eod"):
        raise serve_error(422, "DATASET_NOT_AVAILABLE", "Futures EOD data is not active")
    if product_code is not None:
        product_code = product_code.strip().upper()
    if product_code is not None and product_code not in FUTURES_PRODUCTS:
        raise serve_error(422, "INVALID_PRODUCT", "Unsupported futures product")
    if session is not None and session not in {"regular", "after_hours"}:
        raise serve_error(422, "INVALID_SESSION", "Unsupported futures session")
    if contract_code is not None and (len(contract_code) > 50 or not contract_code):
        raise serve_error(422, "INVALID_CONTRACT", "Invalid contract code")
    _validate_dates(start_date, end_date)
    filters = {
        "product_code": product_code,
        "contract_code": contract_code,
        "start_date": _date_text(start_date),
        "end_date": _date_text(end_date),
        "session": session,
        "order": order,
    }
    last_date: date | None = None
    last_id: UUID | None = None
    if cursor is not None:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "filters", "last_date", "last_id"}
            or payload.get("v") != 1
            or payload.get("kind") != "futures_eod"
            or payload.get("filters") != filters
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        try:
            last_date = date.fromisoformat(payload["last_date"])
            last_id = UUID(payload["last_id"])
        except (TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
    stmt = (
        select(FuturesContractEOD, FuturesContract, Instrument)
        .join(FuturesContract, FuturesContract.contract_id == FuturesContractEOD.contract_id)
        .join(Instrument, Instrument.instrument_id == FuturesContract.instrument_id)
        .where(Instrument.market == "TW", Instrument.asset_class == "future")
    )
    if product_code is not None:
        stmt = stmt.where(Instrument.symbol == product_code)
    if contract_code is not None:
        stmt = stmt.where(FuturesContract.contract_code == contract_code)
    if start_date is not None:
        stmt = stmt.where(FuturesContractEOD.trade_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(FuturesContractEOD.trade_date <= end_date)
    if session is not None:
        stmt = stmt.where(FuturesContractEOD.session == session)
    ascending = order == "asc"
    if last_date is not None and last_id is not None:
        stmt = stmt.where(
            or_(
                FuturesContractEOD.trade_date > last_date
                if ascending
                else FuturesContractEOD.trade_date < last_date,
                and_(
                    FuturesContractEOD.trade_date == last_date,
                    FuturesContractEOD.id > last_id
                    if ascending
                    else FuturesContractEOD.id < last_id,
                ),
            )
        )
    ordering = (
        (FuturesContractEOD.trade_date.asc(), FuturesContractEOD.id.asc())
        if ascending
        else (FuturesContractEOD.trade_date.desc(), FuturesContractEOD.id.desc())
    )
    rows = (await db.execute(stmt.order_by(*ordering).limit(page_size + 1))).all()
    next_cursor = None
    if len(rows) > page_size:
        last = rows[page_size - 1][0]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "futures_eod",
                "filters": filters,
                "last_date": last.trade_date.isoformat(),
                "last_id": str(last.id),
            }
        )
        rows = rows[:page_size]
    return FuturesEODListResponse(
        units=FuturesUnitsResponse(),
        data=[
            FuturesEODResponse(
                contract_id=contract.contract_id,
                instrument_id=instrument.instrument_id,
                product_code=instrument.symbol,
                contract_code=contract.contract_code,
                contract_month=contract.contract_month,
                trade_date=quote.trade_date,
                session=cast(Literal["regular", "after_hours"], quote.session),
                open=quote.open,
                high=quote.high,
                low=quote.low,
                close=quote.close,
                volume=quote.volume,
                settlement_price=quote.settlement_price,
                open_interest=quote.open_interest,
                source=quote.source,
                source_fetched_at=quote.source_fetched_at,
                asof_ts=quote.asof_ts,
                created_at=quote.created_at,
                updated_at=quote.updated_at,
            )
            for quote, contract, instrument in rows
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
    )


FUTURES_PRODUCTS = frozenset({"TX", "MTX", "TMF", "TE", "TF"})


def _contract_type(
    contract_month: str | None,
) -> Literal["monthly", "weekly", "friday_weekly"] | None:
    if contract_month is None:
        return None
    if re.fullmatch(r"\d{6}", contract_month):
        return "monthly"
    if re.fullmatch(r"\d{6}W[1-5]", contract_month):
        return "weekly"
    if re.fullmatch(r"\d{6}F[1-5]", contract_month):
        return "friday_weekly"
    return None


async def list_futures_contracts(
    db: AsyncSession,
    *,
    product_code: str | None,
    active_on: date | None,
    cursor: str | None,
    page_size: int,
) -> FuturesContractListResponse:
    """Expiry contracts with the trade dates FinDB holds quotes for."""
    scopes = await active_dataset_scopes(db)
    if ("TW", "future") not in _pairs(scopes, "futures_eod"):
        raise serve_error(422, "DATASET_NOT_AVAILABLE", "Futures EOD data is not active")
    if product_code is not None:
        product_code = product_code.strip().upper()
        if product_code not in FUTURES_PRODUCTS:
            raise serve_error(422, "INVALID_PRODUCT", "Unsupported futures product")
    filters = {"product_code": product_code, "active_on": _date_text(active_on)}
    last_date: date | None = None
    last_code: str | None = None
    if cursor is not None:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "filters", "last_date", "last_code"}
            or payload.get("v") != 1
            or payload.get("kind") != "futures_contracts"
            or payload.get("filters") != filters
            or not isinstance(payload.get("last_code"), str)
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        try:
            last_date = date.fromisoformat(payload["last_date"])
        except (TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
        last_code = payload["last_code"]
    quoted = (
        select(
            FuturesContractEOD.contract_id.label("contract_id"),
            func.min(FuturesContractEOD.trade_date).label("first_trade_date"),
            func.max(FuturesContractEOD.trade_date).label("last_trade_date"),
        )
        .group_by(FuturesContractEOD.contract_id)
        .subquery()
    )
    stmt = (
        select(FuturesContract, Instrument, quoted.c.first_trade_date, quoted.c.last_trade_date)
        .join(quoted, quoted.c.contract_id == FuturesContract.contract_id)
        .join(Instrument, Instrument.instrument_id == FuturesContract.instrument_id)
        .where(Instrument.market == "TW", Instrument.asset_class == "future")
    )
    if product_code is not None:
        stmt = stmt.where(Instrument.symbol == product_code)
    if active_on is not None:
        stmt = stmt.where(
            quoted.c.first_trade_date <= active_on, quoted.c.last_trade_date >= active_on
        )
    if last_date is not None and last_code is not None:
        stmt = stmt.where(
            or_(
                quoted.c.last_trade_date < last_date,
                and_(
                    quoted.c.last_trade_date == last_date,
                    FuturesContract.contract_code > last_code,
                ),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(
                quoted.c.last_trade_date.desc(), FuturesContract.contract_code.asc()
            ).limit(page_size + 1)
        )
    ).all()
    next_cursor = None
    if len(rows) > page_size:
        last_contract, _, _, last_trade = rows[page_size - 1]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "futures_contracts",
                "filters": filters,
                "last_date": last_trade.isoformat(),
                "last_code": last_contract.contract_code,
            }
        )
        rows = rows[:page_size]
    return FuturesContractListResponse(
        data=[
            FuturesContractResponse(
                contract_id=contract.contract_id,
                instrument_id=instrument.instrument_id,
                product_code=instrument.symbol,
                contract_code=contract.contract_code,
                contract_month=contract.contract_month,
                contract_type=_contract_type(contract.contract_month),
                expiry_date=contract.expiry_date,
                first_trade_date=first_trade,
                last_trade_date=last_trade,
            )
            for contract, instrument, first_trade, last_trade in rows
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
    )


async def list_exchange_quotes(
    db: AsyncSession,
    *,
    instruments_raw: str,
    start_date: date | None,
    end_date: date | None,
    order: SortDirection,
    cursor: str | None,
    page_size: int,
) -> ExchangeQuoteListResponse:
    """Rows of the exchange's official daily report for the requested instruments."""
    _validate_dates(start_date, end_date)
    pairs = {
        (scope.market, scope.asset_class) for scope in await active_exchange_quote_datasets(db)
    }
    if not pairs:
        raise serve_error(422, "DATASET_NOT_AVAILABLE", "Exchange quotes are not active")
    selectors = parse_selectors(instruments_raw, param="instruments", allow_uuid=True)
    matched = _require_matched(await _match_selectors(db, selectors, pairs), "Exchange quotes")
    instrument_ids = sorted({instrument.instrument_id for instrument in matched})
    scope = {
        "instrument_ids": [str(value) for value in instrument_ids],
        "start_date": _date_text(start_date),
        "end_date": _date_text(end_date),
        "order": order,
    }
    last_date: date | None = None
    last_id: UUID | None = None
    if cursor:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "scope", "last_date", "last_id"}
            or payload.get("v") != 1
            or payload.get("kind") != "exchange_quotes"
            or payload.get("scope") != scope
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        try:
            last_date = date.fromisoformat(payload["last_date"])
            last_id = UUID(payload["last_id"])
        except (TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
    query = (
        select(ExchangeDailyQuote, Instrument)
        .join(Instrument, ExchangeDailyQuote.instrument_id == Instrument.instrument_id)
        .where(ExchangeDailyQuote.instrument_id.in_(instrument_ids))
    )
    if start_date:
        query = query.where(ExchangeDailyQuote.trade_date >= start_date)
    if end_date:
        query = query.where(ExchangeDailyQuote.trade_date <= end_date)
    ascending = order == "asc"
    if last_date and last_id:
        query = query.where(
            or_(
                ExchangeDailyQuote.trade_date > last_date
                if ascending
                else ExchangeDailyQuote.trade_date < last_date,
                and_(
                    ExchangeDailyQuote.trade_date == last_date,
                    ExchangeDailyQuote.instrument_id > last_id,
                ),
            )
        )
    date_order = (
        ExchangeDailyQuote.trade_date.asc() if ascending else ExchangeDailyQuote.trade_date.desc()
    )
    rows = (
        await db.execute(
            query.order_by(date_order, ExchangeDailyQuote.instrument_id.asc()).limit(page_size + 1)
        )
    ).all()
    next_cursor = None
    if len(rows) > page_size:
        last_quote = rows[page_size - 1][0]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "exchange_quotes",
                "scope": scope,
                "last_date": last_quote.trade_date.isoformat(),
                "last_id": str(last_quote.instrument_id),
            }
        )
        rows = rows[:page_size]
    return ExchangeQuoteListResponse(
        data=[
            ExchangeQuoteResponse(
                instrument_id=quote.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                market=instrument.market,
                trade_date=quote.trade_date,
                board=quote.board,
                currency=quote.currency,
                trading_status=cast(Literal["traded", "no_trade"], quote.trading_status),
                previous_close=quote.previous_close,
                close=quote.close,
                high=quote.high,
                low=quote.low,
                bid=quote.bid,
                ask=quote.ask,
                volume=quote.volume,
                turnover=quote.turnover,
                source=quote.source,
                source_fetched_at=quote.source_fetched_at,
            )
            for quote, instrument in rows
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
    )
