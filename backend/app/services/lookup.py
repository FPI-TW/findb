"""Read-only database queries for the public lookup UI."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import Any

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql import ColumnElement

from app.models.canonical import Instrument, InstrumentStats, MacroSeries
from app.schemas.common import PaginationInfo
from app.schemas.lookup import (
    InstrumentLookupFacets,
    InstrumentLookupItem,
    InstrumentLookupResponse,
    InstrumentLookupSort,
    MacroSeriesLookupFacets,
    MacroSeriesLookupItem,
    MacroSeriesLookupResponse,
    MacroSeriesLookupSort,
    SortDirection,
)

LookupColumn = ColumnElement[Any] | InstrumentedAttribute[Any]


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized or None


def _normalize_filter(value: str | None) -> str | None:
    normalized = _normalize(value)
    return normalized.casefold() if normalized else None


def _literal_like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _text_search(
    query: str | None,
    columns: Sequence[LookupColumn],
) -> ColumnElement[bool] | None:
    normalized = _normalize(query)
    if not normalized:
        return None
    pattern = _literal_like_pattern(normalized)
    return or_(*(column.ilike(pattern, escape="\\") for column in columns))


def _case_insensitive_filter(
    column: LookupColumn,
    value: str | None,
) -> ColumnElement[bool] | None:
    normalized = _normalize_filter(value)
    if not normalized:
        return None
    return func.lower(column) == normalized


def _order_by(
    column: LookupColumn,
    direction: SortDirection,
    tie_breaker: LookupColumn,
) -> tuple[ColumnElement[Any], ColumnElement[Any]]:
    primary = column.desc() if direction == "desc" else column.asc()
    return primary.nulls_last(), tie_breaker.asc()


async def _global_values(
    db: AsyncSession,
    column: LookupColumn,
) -> list[str]:
    result = await db.execute(
        select(column).where(column.is_not(None)).distinct().order_by(column.asc())
    )
    return [value for value in result.scalars().all() if value is not None]


async def lookup_instruments(
    db: AsyncSession,
    *,
    q: str | None,
    market: str | None,
    asset_class: str | None,
    status: str | None,
    sort_by: InstrumentLookupSort,
    sort_dir: SortDirection,
    page: int,
    page_size: int,
) -> InstrumentLookupResponse:
    """Search and paginate canonical instruments without mutating state."""
    query = select(
        Instrument,
        InstrumentStats.first_trade_date,
        InstrumentStats.latest_trade_date,
        InstrumentStats.latest_price,
    ).outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
    count_query = select(func.count()).select_from(Instrument)

    filters: list[ColumnElement[bool]] = []
    search_filter = _text_search(
        q,
        (
            cast(Instrument.instrument_id, String),
            Instrument.symbol,
            Instrument.name,
            Instrument.market,
            Instrument.asset_class,
            Instrument.currency,
        ),
    )
    if search_filter is not None:
        filters.append(search_filter)

    for filter_clause in (
        _case_insensitive_filter(Instrument.market, market),
        _case_insensitive_filter(Instrument.asset_class, asset_class),
        _case_insensitive_filter(Instrument.status, status),
    ):
        if filter_clause is not None:
            filters.append(filter_clause)

    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)

    total_records = int((await db.execute(count_query)).scalar_one())
    total_pages = (total_records + page_size - 1) // page_size

    sort_columns: dict[InstrumentLookupSort, LookupColumn] = {
        "market": Instrument.market,
        "symbol": Instrument.symbol,
        "name": Instrument.name,
        "asset_class": Instrument.asset_class,
        "currency": Instrument.currency,
        "first_trade_date": InstrumentStats.first_trade_date,
        "latest_trade_date": InstrumentStats.latest_trade_date,
        "latest_price": InstrumentStats.latest_price,
        "status": Instrument.status,
    }
    query = (
        query.order_by(
            *_order_by(
                sort_columns[sort_by],
                sort_dir,
                Instrument.instrument_id,
            )
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(query)).all()

    markets, asset_classes, statuses = await _instrument_facets(db)
    return InstrumentLookupResponse(
        data=[
            InstrumentLookupItem(
                instrument_id=instrument.instrument_id,
                market=instrument.market,
                symbol=instrument.symbol,
                name=instrument.name,
                asset_class=instrument.asset_class,
                currency=instrument.currency,
                status=instrument.status,
                first_trade_date=first_trade_date,
                latest_trade_date=latest_trade_date,
                latest_price=latest_price,
            )
            for instrument, first_trade_date, latest_trade_date, latest_price in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
        facets=InstrumentLookupFacets(
            markets=markets,
            asset_classes=asset_classes,
            statuses=statuses,
        ),
    )


async def _instrument_facets(
    db: AsyncSession,
) -> tuple[list[str], list[str], list[str]]:
    return (
        await _global_values(db, Instrument.market),
        await _global_values(db, Instrument.asset_class),
        await _global_values(db, Instrument.status),
    )


async def lookup_macro_series(
    db: AsyncSession,
    *,
    q: str | None,
    market: str | None,
    frequency: str | None,
    source: str | None,
    sort_by: MacroSeriesLookupSort,
    sort_dir: SortDirection,
    page: int,
    page_size: int,
) -> MacroSeriesLookupResponse:
    """Search and paginate canonical macro series without mutating state."""
    query = select(MacroSeries)
    count_query = select(func.count()).select_from(MacroSeries)

    filters: list[ColumnElement[bool]] = []
    search_filter = _text_search(
        q,
        (
            cast(MacroSeries.series_id, String),
            MacroSeries.name,
            MacroSeries.source_code,
            MacroSeries.unit,
            MacroSeries.frequency,
            MacroSeries.source,
            MacroSeries.market,
        ),
    )
    if search_filter is not None:
        filters.append(search_filter)

    for filter_clause in (
        _case_insensitive_filter(MacroSeries.market, market),
        _case_insensitive_filter(MacroSeries.frequency, frequency),
        _case_insensitive_filter(MacroSeries.source, source),
    ):
        if filter_clause is not None:
            filters.append(filter_clause)

    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)

    total_records = int((await db.execute(count_query)).scalar_one())
    total_pages = (total_records + page_size - 1) // page_size

    sort_columns: dict[MacroSeriesLookupSort, LookupColumn] = {
        "market": MacroSeries.market,
        "source_code": MacroSeries.source_code,
        "name": MacroSeries.name,
        "frequency": MacroSeries.frequency,
        "unit": MacroSeries.unit,
        "source": MacroSeries.source,
    }
    query = (
        query.order_by(
            *_order_by(
                sort_columns[sort_by],
                sort_dir,
                MacroSeries.series_id,
            )
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    series_items = (await db.execute(query)).scalars().all()

    markets, frequencies, sources = await _macro_facets(db)
    return MacroSeriesLookupResponse(
        data=[
            MacroSeriesLookupItem(
                series_id=series.series_id,
                market=series.market,
                source_code=series.source_code,
                name=series.name,
                frequency=series.frequency,
                unit=series.unit,
                source=series.source,
            )
            for series in series_items
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
        facets=MacroSeriesLookupFacets(
            markets=markets,
            frequencies=frequencies,
            sources=sources,
        ),
    )


async def _macro_facets(
    db: AsyncSession,
) -> tuple[list[str], list[str], list[str]]:
    return (
        await _global_values(db, MacroSeries.market),
        await _global_values(db, MacroSeries.frequency),
        await _global_values(db, MacroSeries.source),
    )
