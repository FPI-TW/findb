"""Read-only query services for the public Serve API."""

from __future__ import annotations

import base64
import binascii
import calendar
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import String, and_, case, func, literal_column, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import ColumnElement

from app.models.canonical import (
    CalendarRevisionDay,
    CalendarYearRevision,
    FuturesContract,
    FuturesContractEOD,
    Instrument,
    InstrumentStats,
    MarketDataEOD,
    MarketDataMinute,
)
from app.models.registry import DatasetRegistry
from app.schemas.common import PaginationInfo
from app.schemas.serve import (
    CalendarListResponse,
    CalendarResponse,
    CursorPaginationInfo,
    DatasetCoverageResponse,
    DatasetListResponse,
    DatasetResponse,
    EODCoverageResponse,
    EODListResponse,
    EODResponse,
    FuturesEODListResponse,
    FuturesEODResponse,
    InstrumentCoverageResponse,
    InstrumentDetailResponse,
    InstrumentFacetsResponse,
    InstrumentListResponse,
    InstrumentResponse,
    InstrumentSort,
    MinuteCoverageResponse,
    MinuteListResponse,
    MinuteResponse,
    ResolvedDateRangeResponse,
    SortDirection,
)
from app.services.calendar_management import complete_published_revision_ids
from app.services.ingress_contracts import DatasetContractDeclaration

LookupColumn = ColumnElement[Any] | InstrumentedAttribute[Any]
DataKind = Literal["eod", "minute", "futures_eod"]
MAX_CURSOR_LENGTH = 2048
MAX_CURSOR_BYTES = 1536
BASE64URL_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
SUPPORTED_ACTIVE_SCOPES = frozenset(
    {
        ("market_eod", "US", "equity"),
        ("market_eod", "TW", "equity"),
        ("market_eod", "TW", "etf"),
        ("market_eod", "HK", "equity"),
        ("futures_eod", "TW", "future"),
        ("market_minute", "TW", "equity"),
        ("market_minute", "TW", "etf"),
    }
)


@dataclass(frozen=True)
class ActiveDatasetScope:
    dataset_key: str
    market: str
    asset_class: str
    frequency: Literal["daily", "minute"]
    data_kind: DataKind
    interval: Literal["1d", "1m"]


def serve_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def configuration_error() -> HTTPException:
    return serve_error(503, "SERVE_CONFIGURATION_INVALID", "Serve dataset configuration is invalid")


async def active_dataset_scopes(db: AsyncSession) -> tuple[ActiveDatasetScope, ...]:
    rows = (
        await db.execute(
            select(DatasetRegistry)
            .where(DatasetRegistry.is_active.is_(True))
            .order_by(DatasetRegistry.dataset_key)
        )
    ).scalars()
    scopes: list[ActiveDatasetScope] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        try:
            declaration = DatasetContractDeclaration.model_validate(row.config)
            schema_id = declaration.schema_id
            market = row.market.strip().upper()
            asset_class = row.asset_class.strip().lower()
            frequency = row.frequency.strip().lower()
            if declaration.schema_enforcement != "enforce":
                raise ValueError("schema enforcement must be enabled")
            if declaration.current_schema_version != 1 or declaration.accepted_schema_versions != [
                1
            ]:
                raise ValueError("Serve supports only the current v1 contract")
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
            if identity in seen:
                raise ValueError("duplicate active canonical scope")
            seen.add(identity)
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
    return tuple(scopes)


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


def _literal_like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _instrument_response(
    instrument: Instrument,
    stats: InstrumentStats | None,
    eod_pairs: set[tuple[str, str]],
    minute_pairs: set[tuple[str, str]],
    futures_pairs: set[tuple[str, str]],
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
        coverage=InstrumentCoverageResponse(eod=eod, minute=minute, futures=futures),
    )


async def list_datasets(db: AsyncSession) -> DatasetListResponse:
    scopes = await active_dataset_scopes(db)
    items: list[DatasetResponse] = []
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
                availability="available" if instrument_count else "configured_empty",
                coverage=DatasetCoverageResponse(
                    coverage_start_date=first_date,
                    coverage_end_date=latest_date,
                    instrument_count=int(instrument_count or 0),
                ),
            )
        )
    return DatasetListResponse(data=items)


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
    market = market.upper()
    base = (
        select(CalendarRevisionDay, CalendarYearRevision.market)
        .join(
            CalendarYearRevision,
            CalendarRevisionDay.calendar_revision_id == CalendarYearRevision.id,
        )
        .where(
            CalendarYearRevision.market == market,
            CalendarYearRevision.id.in_(complete_published_revision_ids()),
        )
    )
    count_query = (
        select(func.count(CalendarRevisionDay.id))
        .join(
            CalendarYearRevision,
            CalendarRevisionDay.calendar_revision_id == CalendarYearRevision.id,
        )
        .where(
            CalendarYearRevision.market == market,
            CalendarYearRevision.id.in_(complete_published_revision_ids()),
        )
    )
    if start_date:
        base = base.where(CalendarRevisionDay.trade_date >= start_date)
        count_query = count_query.where(CalendarRevisionDay.trade_date >= start_date)
    if end_date:
        base = base.where(CalendarRevisionDay.trade_date <= end_date)
        count_query = count_query.where(CalendarRevisionDay.trade_date <= end_date)
    if is_open is not None:
        base = base.where(CalendarRevisionDay.is_open == is_open)
        count_query = count_query.where(CalendarRevisionDay.is_open == is_open)
    total_records = int((await db.execute(count_query)).scalar() or 0)
    rows = (
        await db.execute(
            base.order_by(CalendarRevisionDay.trade_date)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return CalendarListResponse(
        data=[
            CalendarResponse(
                market=market_code,
                trade_date=day.trade_date,
                is_open=day.is_open,
                session_open=str(day.session_open) if day.session_open else None,
                session_close=str(day.session_close) if day.session_close else None,
                holiday_name=day.holiday_name,
                day_status=day.day_status,
                description=day.description,
            )
            for day, market_code in rows
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
    sort_by: InstrumentSort,
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
        filters.append(
            or_(
                _normalized_db_text(Instrument.instrument_id).ilike(pattern, escape="\\"),
                _normalized_db_text(Instrument.symbol).ilike(pattern, escape="\\"),
                _normalized_db_text(Instrument.name).ilike(pattern, escape="\\"),
            )
        )
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
    primary = sort_columns[sort_by].desc() if sort_dir == "desc" else sort_columns[sort_by].asc()
    rows = (
        await db.execute(
            query.order_by(primary.nulls_last(), Instrument.instrument_id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    facets = await _instrument_facets(db, all_pairs)
    return InstrumentListResponse(
        data=[
            _instrument_response(instrument, stats, eod_pairs, minute_pairs, futures_pairs)
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
        )
    )


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


def _eod_scope_payload(
    *,
    instrument_id: UUID | None,
    market: str | None,
    symbols: tuple[str, ...],
    start_date: date | None,
    end_date: date | None,
) -> dict[str, Any]:
    return {
        "instrument_id": str(instrument_id) if instrument_id else None,
        "market": market,
        "symbols": list(symbols),
        "start_date": _date_text(start_date),
        "end_date": _date_text(end_date),
    }


async def list_eod(
    db: AsyncSession,
    *,
    instrument_id: UUID | None,
    market: str | None,
    symbols_raw: str | None,
    start_date: date | None,
    end_date: date | None,
    cursor: str | None,
    page_size: int,
) -> EODListResponse:
    _validate_dates(start_date, end_date)
    normalized_market_value = _normalize(market)
    normalized_market = normalized_market_value.upper() if normalized_market_value else None
    if (instrument_id is None) == (normalized_market is None):
        raise serve_error(
            422, "QUERY_SCOPE_REQUIRED", "Provide instrument_id or market, but not both"
        )
    if instrument_id is not None and symbols_raw is not None:
        raise serve_error(422, "INVALID_SYMBOLS", "symbols may only be used with market scope")
    scopes = await active_dataset_scopes(db)
    eod_pairs = _pairs(scopes, "eod")
    symbols: tuple[str, ...] = ()
    instrument_ids: list[UUID] | None = None
    if instrument_id is not None:
        instrument = await db.get(Instrument, instrument_id)
        if instrument is None:
            raise serve_error(404, "INSTRUMENT_NOT_FOUND", "Instrument not found")
        if (instrument.market, instrument.asset_class) not in eod_pairs:
            raise serve_error(
                422, "DATASET_NOT_AVAILABLE", "EOD data is not available for this instrument"
            )
        instrument_ids = [instrument_id]
    else:
        if normalized_market is None:
            raise serve_error(422, "QUERY_SCOPE_REQUIRED", "A non-empty market is required")
        market_pairs = {pair for pair in eod_pairs if pair[0] == normalized_market}
        if not market_pairs:
            raise serve_error(
                422, "DATASET_NOT_AVAILABLE", "EOD data is not available for this market"
            )
        if symbols_raw is not None:
            raw_tokens = symbols_raw.split(",")
            if len(raw_tokens) > 50 or any(not token.strip() for token in raw_tokens):
                raise serve_error(
                    422, "INVALID_SYMBOLS", "symbols must contain 1 to 50 non-empty values"
                )
            normalized_tokens = [
                unicodedata.normalize("NFKC", token).strip().upper() for token in raw_tokens
            ]
            if any(not token for token in normalized_tokens):
                raise serve_error(422, "INVALID_SYMBOLS", "symbols contain an invalid value")
            symbols = tuple(sorted(set(normalized_tokens)))
            matches = (
                (
                    await db.execute(
                        select(Instrument).where(
                            Instrument.market == normalized_market,
                            Instrument.symbol.in_(symbols),
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_symbol: dict[str, list[Instrument]] = {symbol: [] for symbol in symbols}
            for match in matches:
                by_symbol[match.symbol].append(match)
            if any(not by_symbol[symbol] for symbol in symbols):
                raise serve_error(
                    404, "INSTRUMENT_NOT_FOUND", "One or more instruments were not found"
                )
            if any(
                not any((item.market, item.asset_class) in eod_pairs for item in by_symbol[symbol])
                for symbol in symbols
            ):
                raise serve_error(
                    422,
                    "DATASET_NOT_AVAILABLE",
                    "EOD data is not available for one or more instruments",
                )
            instrument_ids = [
                item.instrument_id
                for symbol in symbols
                for item in by_symbol[symbol]
                if (item.market, item.asset_class) in eod_pairs
            ]
    scope = _eod_scope_payload(
        instrument_id=instrument_id,
        market=normalized_market,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )
    last_date: date | None = None
    last_id: UUID | None = None
    if cursor:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "scope", "last"}
            or payload.get("v") != 1
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
        assert normalized_market is not None
        query = query.where(
            Instrument.market == normalized_market,
            _instrument_scope_filter({pair for pair in eod_pairs if pair[0] == normalized_market}),
        )
    if start_date:
        query = query.where(MarketDataEOD.trade_date >= start_date)
    if end_date:
        query = query.where(MarketDataEOD.trade_date <= end_date)
    if last_date and last_id:
        query = query.where(
            or_(
                MarketDataEOD.trade_date < last_date,
                and_(MarketDataEOD.trade_date == last_date, MarketDataEOD.instrument_id > last_id),
            )
        )
    rows = (
        await db.execute(
            query.order_by(
                MarketDataEOD.trade_date.desc(), MarketDataEOD.instrument_id.asc()
            ).limit(page_size + 1)
        )
    ).all()
    next_cursor = None
    if len(rows) > page_size:
        last_eod = rows[page_size - 1][0]
        next_cursor = _encode_cursor(
            {
                "v": 1,
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
        data=[
            EODResponse(
                instrument_id=eod.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                market=instrument.market,
                trade_date=eod.trade_date,
                open=eod.open,
                high=eod.high,
                low=eod.low,
                close=eod.close,
                volume=eod.volume,
                total_ticks=eod.total_ticks,
                turnover=eod.turnover,
                source=eod.source,
                source_fetched_at=eod.source_fetched_at,
                asof_ts=eod.asof_ts,
                created_at=eod.created_at,
                updated_at=eod.updated_at,
            )
            for eod, instrument in rows
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
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
    cursor: str | None,
    page_size: int,
) -> FuturesEODListResponse:
    """Read actual expiry/session quotes with a query-bound stable cursor."""
    scopes = await active_dataset_scopes(db)
    if ("TW", "future") not in _pairs(scopes, "futures_eod"):
        raise serve_error(422, "DATASET_NOT_AVAILABLE", "Futures EOD data is not active")
    if product_code is not None and product_code not in {"TX", "MTX", "TMF", "TE", "TF"}:
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
    if last_date is not None and last_id is not None:
        stmt = stmt.where(
            or_(
                FuturesContractEOD.trade_date < last_date,
                and_(FuturesContractEOD.trade_date == last_date, FuturesContractEOD.id < last_id),
            )
        )
    rows = (
        await db.execute(
            stmt.order_by(FuturesContractEOD.trade_date.desc(), FuturesContractEOD.id.desc()).limit(
                page_size + 1
            )
        )
    ).all()
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
