"""Thin HTTP routes for canonical read-only Serve queries."""

from datetime import date, time
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    require_intraday_serve_api_key,
    require_serve_api_key,
    verify_paginated_serve_api_key,
    verify_serve_api_key,
)
from app.dependencies import get_db
from app.schemas.serve import (
    CalendarListResponse,
    CalendarResponse,
    DatasetListResponse,
    EODListResponse,
    FuturesEODListResponse,
    InstrumentDetailResponse,
    InstrumentListResponse,
    InstrumentSort,
    IntradayBarListResponse,
    IntradayDownloadManifestResponse,
    IntradayInterval,
    IntradayTickListResponse,
    MarketFreshnessSummaryListResponse,
    MarketFreshnessSummaryResponse,
    MinuteListResponse,
    PublishedCalendarYearEnvelope,
    PublishedCalendarYearResponse,
    SortDirection,
)
from app.services import serve as serve_queries
from app.services import serve_downloads, serve_intraday
from app.services.effective_calendar import published_calendar_year
from app.services.market_freshness import list_market_freshness
from app.services.serve_downloads import DOWNLOAD_SCOPE
from app.services.serve_errors import serve_error
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
    _: str | None = Depends(verify_paginated_serve_api_key(50)),
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
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
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
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
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


@router.get("/intraday/bars", response_model=IntradayBarListResponse)
async def get_intraday_bars(
    instrument_id: UUID,
    interval: IntradayInterval = "1m",
    start_date: date | None = None,
    end_date: date | None = None,
    cursor: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=1000)] = 1000,
    _: str = Depends(require_intraday_serve_api_key(1000, cost=lambda page_size: 1)),
    db: AsyncSession = Depends(get_db),
) -> IntradayBarListResponse:
    return await serve_intraday.list_intraday_bars(
        db,
        instrument_id=instrument_id,
        interval=interval,
        start_date=start_date,
        end_date=end_date,
        cursor=cursor,
        page_size=page_size,
    )


@router.get("/intraday/ticks", response_model=IntradayTickListResponse)
async def get_intraday_ticks(
    instrument_id: UUID,
    trade_date: date | None = None,
    start_time: time | None = None,
    end_time: time | None = None,
    cursor: str | None = None,
    page_size: Annotated[int, Query(ge=1, le=5000)] = 1000,
    # A tick page counts one unit per started thousand rows.
    _: str = Depends(
        require_intraday_serve_api_key(1000, cost=lambda page_size: -(-page_size // 1000))
    ),
    db: AsyncSession = Depends(get_db),
) -> IntradayTickListResponse:
    return await serve_intraday.list_intraday_ticks(
        db,
        instrument_id=instrument_id,
        trade_date=trade_date,
        start_time=start_time,
        end_time=end_time,
        cursor=cursor,
        page_size=page_size,
    )


@router.get("/intraday/downloads", response_model=IntradayDownloadManifestResponse)
async def get_intraday_download_manifest(
    instrument_id: UUID,
    month: Annotated[str, Query(min_length=7, max_length=7, description="YYYY-MM")],
    kind: Literal["bars", "ticks"] = "bars",
    key_id: str = Depends(
        require_intraday_serve_api_key(None, cost=lambda page_size: 1, scope=DOWNLOAD_SCOPE)
    ),
    db: AsyncSession = Depends(get_db),
) -> IntradayDownloadManifestResponse:
    return await serve_downloads.intraday_download_manifest(
        db, key_id=UUID(key_id), instrument_id=instrument_id, kind=kind, month=month
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
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
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
    try:
        year_data = await published_calendar_year(db, market, year)
    except ValueError as exc:
        raise serve_error(422, "INVALID_PARAMETER", str(exc)) from exc
    return PublishedCalendarYearEnvelope(
        data=PublishedCalendarYearResponse(
            **{
                **year_data,
                "days": [CalendarResponse(**day) for day in year_data["days"]],
            }
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
    _: str | None = Depends(verify_paginated_serve_api_key(100)),
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
