"""Thin HTTP routes for canonical read-only Serve queries."""

from datetime import date
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_serve_api_key, verify_serve_api_key
from app.dependencies import get_db
from app.models.canonical import CalendarRevisionDay
from app.schemas.serve import (
    CalendarListResponse,
    CalendarResponse,
    DatasetListResponse,
    EODListResponse,
    FuturesEODListResponse,
    InstrumentDetailResponse,
    InstrumentListResponse,
    InstrumentSort,
    MarketFreshnessSummaryListResponse,
    MarketFreshnessSummaryResponse,
    MinuteListResponse,
    PublishedCalendarYearEnvelope,
    PublishedCalendarYearResponse,
    SortDirection,
)
from app.services import serve as serve_queries
from app.services.calendar_management import published_year
from app.services.market_freshness import list_market_freshness
from app.services.slot_identity import CanonicalSlotId

router = APIRouter()


@router.get("/datasets", response_model=DatasetListResponse)
async def get_datasets(
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> DatasetListResponse:
    return await serve_queries.list_datasets(db)


@router.get("/instruments", response_model=InstrumentListResponse)
async def get_instruments(
    q: Annotated[str | None, Query(max_length=200)] = None,
    market: str | None = None,
    asset_class: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    has_eod: bool | None = None,
    has_minute: bool | None = None,
    sort_by: InstrumentSort = "market",
    sort_dir: SortDirection = "asc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> InstrumentListResponse:
    return await serve_queries.list_instruments(
        db,
        q=q,
        market=market,
        asset_class=asset_class,
        status=status_filter,
        has_eod=has_eod,
        has_minute=has_minute,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )


@router.get("/instruments/{instrument_id}", response_model=InstrumentDetailResponse)
async def get_instrument(
    instrument_id: UUID,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> InstrumentDetailResponse:
    return await serve_queries.get_instrument(db, instrument_id)


@router.get("/eod", response_model=EODListResponse)
async def get_eod(
    instrument_id: UUID | None = None,
    market: str | None = None,
    symbols: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    cursor: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> EODListResponse:
    return await serve_queries.list_eod(
        db,
        instrument_id=instrument_id,
        market=market,
        symbols_raw=symbols,
        start_date=start_date,
        end_date=end_date,
        cursor=cursor,
        page_size=page_size,
    )


@router.get("/minute", response_model=MinuteListResponse)
async def get_minute(
    instrument_id: UUID,
    start_date: date | None = None,
    end_date: date | None = None,
    cursor: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> MinuteListResponse:
    return await serve_queries.list_minute(
        db,
        instrument_id=instrument_id,
        start_date=start_date,
        end_date=end_date,
        cursor=cursor,
        page_size=page_size,
    )


@router.get("/futures/eod", response_model=FuturesEODListResponse)
async def get_futures_eod(
    product_code: str | None = None,
    contract_code: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    session: str | None = None,
    cursor: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> FuturesEODListResponse:
    return await serve_queries.list_futures_eod(
        db,
        product_code=product_code,
        contract_code=contract_code,
        start_date=start_date,
        end_date=end_date,
        session=session,
        cursor=cursor,
        page_size=page_size,
    )


@router.get("/calendar/years/{market}/{year}", response_model=PublishedCalendarYearEnvelope)
async def get_published_calendar_year(
    market: str,
    year: int,
    _: str = Depends(require_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> PublishedCalendarYearEnvelope:
    if year < 1900 or year > 2200:
        raise HTTPException(status_code=422, detail="year must be between 1900 and 2200")
    resolved = await published_year(db, market.upper(), year)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Complete published calendar year not found")
    revision, _market_config, days = resolved
    return PublishedCalendarYearEnvelope(
        data=PublishedCalendarYearResponse(
            market=revision.market,
            year=revision.year,
            revision=revision.revision,
            status="published",
            coverage_complete=True,
            timezone=revision.timezone,
            expected_days=revision.expected_days,
            actual_days=revision.actual_days,
            days=[_calendar_response(day, revision.market) for day in days],
        )
    )


@router.get("/calendar", response_model=CalendarListResponse)
async def get_calendar(
    market: str,
    start_date: date | None = None,
    end_date: date | None = None,
    is_open: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 100,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> CalendarListResponse:
    return await serve_queries.list_calendar(
        db,
        market=market,
        start_date=start_date,
        end_date=end_date,
        is_open=is_open,
        page=page,
        page_size=page_size,
    )


def _calendar_response(day: CalendarRevisionDay, market: str) -> CalendarResponse:
    return CalendarResponse(
        market=market,
        trade_date=day.trade_date,
        is_open=day.is_open,
        session_open=str(day.session_open) if day.session_open else None,
        session_close=str(day.session_close) if day.session_close else None,
        holiday_name=day.holiday_name,
        day_status=day.day_status,
        description=day.description,
    )


@router.get("/market-freshness", response_model=MarketFreshnessSummaryListResponse)
async def get_market_freshness(
    market: Annotated[str | None, Query(min_length=1, max_length=10)] = None,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> MarketFreshnessSummaryListResponse:
    rows = await list_market_freshness(db, market=market)
    return MarketFreshnessSummaryListResponse(
        data=[
            MarketFreshnessSummaryResponse(
                market=row.market,
                slot_id=cast(CanonicalSlotId, row.slot_id),
                scheduled_local_time=row.scheduled_local_time,
                timezone=row.timezone,
                status=row.status,
                expected_data_date=row.expected_data_date,
                coverage_data_date=row.coverage_data_date,
                last_successful_update_at=row.last_successful_update_at,
                last_complete_at=row.last_complete_at,
                next_scheduled_at=row.next_scheduled_at,
                feed_count=row.feed_count,
                fresh_feed_count=row.fresh_feed_count,
                late_feed_count=row.late_feed_count,
            )
            for row in rows
        ]
    )
