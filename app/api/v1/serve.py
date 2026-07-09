"""
Serve API 端點。
提供消費端查詢市場資料。
"""

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_serve_api_key
from app.dependencies import get_db
from app.models.canonical import (
    CorporateAction,
    FuturesContinuousEOD,
    FuturesContract,
    Instrument,
    InstrumentStats,
    MacroObservation,
    MacroSeries,
    MarketDataEOD,
    RollRule,
    TradingCalendar,
)
from app.schemas.common import PaginationInfo
from app.schemas.serve import (
    CalendarListResponse,
    CalendarResponse,
    CorporateActionListResponse,
    CorporateActionResponse,
    EODListResponse,
    EODResponse,
    FuturesContinuousListResponse,
    FuturesContinuousResponse,
    FuturesContractListResponse,
    FuturesContractResponse,
    InstrumentListResponse,
    InstrumentResponse,
    MacroObservationListResponse,
    MacroObservationResponse,
    MacroSeriesListResponse,
    MacroSeriesResponse,
)

router = APIRouter()


@router.get("/instruments", response_model=InstrumentListResponse)
async def list_instruments(
    market: Optional[str] = Query(None, description="依市場篩選，例如 CRYPTO、US"),
    asset_class: Optional[str] = Query(None, description="依資產類別篩選"),
    status_filter: Optional[str] = Query(None, alias="status", description="依狀態篩選"),
    symbol: Optional[str] = Query(None, description="依商品代號精確篩選"),
    cursor: Optional[str] = Query(None, description="Keyset cursor from pagination.next_cursor"),
    include_count: bool = Query(True, description="是否回傳 total_records / total_pages"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出商品，支援選用篩選條件。"""
    query = select(
        Instrument,
        InstrumentStats.first_trade_date,
        InstrumentStats.latest_trade_date,
        InstrumentStats.latest_price,
    ).outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
    count_query = select(func.count(Instrument.instrument_id))

    # Apply filters
    filters = []
    if market:
        filters.append(Instrument.market == market.upper())
    if asset_class:
        filters.append(Instrument.asset_class == asset_class.lower())
    if status_filter:
        filters.append(Instrument.status == status_filter)
    if symbol:
        filters.append(Instrument.symbol == symbol.upper())
    if cursor:
        filters.append(Instrument.symbol > cursor.upper())

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_records: int | None = None
    total_pages: int | None = None
    if include_count:
        total_result = await db.execute(count_query)
        total_records = int(total_result.scalar() or 0)
        total_pages = (total_records + page_size - 1) // page_size

    query = query.order_by(Instrument.symbol).limit(page_size + 1)
    if not cursor:
        offset = (page - 1) * page_size
        query = query.offset(offset)

    result = await db.execute(query)
    rows = result.all()
    next_cursor = None
    if len(rows) > page_size:
        next_cursor = rows[page_size - 1][0].symbol
        rows = rows[:page_size]

    return InstrumentListResponse(
        success=True,
        data=[
            InstrumentResponse(
                instrument_id=inst.instrument_id,
                asset_class=inst.asset_class,
                market=inst.market,
                symbol=inst.symbol,
                name=inst.name,
                currency=inst.currency,
                timezone=inst.timezone,
                status=inst.status,
                listed_date=inst.listed_date,
                delisted_date=inst.delisted_date,
                first_trade_date=first_trade_date,
                latest_trade_date=latest_trade_date,
                latest_price=latest_price,
            )
            for inst, first_trade_date, latest_trade_date, latest_price in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
            next_cursor=next_cursor,
        ),
    )


@router.get("/instruments/{instrument_id}", response_model=InstrumentResponse)
async def get_instrument(
    instrument_id: UUID,
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """依 ID 取得單一商品。"""
    result = await db.execute(
        select(
            Instrument,
            InstrumentStats.first_trade_date,
            InstrumentStats.latest_trade_date,
            InstrumentStats.latest_price,
        )
        .outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
        .where(Instrument.instrument_id == instrument_id)
    )
    row = result.one_or_none()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument {instrument_id} not found",
        )

    instrument, first_trade_date_value, latest_trade_date_value, latest_price_value = row
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
        first_trade_date=first_trade_date_value,
        latest_trade_date=latest_trade_date_value,
        latest_price=latest_price_value,
    )


@router.get("/eod", response_model=EODListResponse)
async def list_eod_data(
    market: Optional[str] = Query(None, description="依市場篩選"),
    symbols: Optional[str] = Query(None, description="以逗號分隔的商品代號"),
    start_date: Optional[date] = Query(None, description="起始日期 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="結束日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出日線資料，支援選用篩選條件。"""
    # Build query with join to get instrument info
    query = select(MarketDataEOD, Instrument).join(
        Instrument, MarketDataEOD.instrument_id == Instrument.instrument_id
    )
    count_query = (
        select(func.count())
        .select_from(MarketDataEOD)
        .join(Instrument, MarketDataEOD.instrument_id == Instrument.instrument_id)
    )

    # Apply filters
    filters = []
    if market:
        filters.append(Instrument.market == market.upper())
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",")]
        filters.append(Instrument.symbol.in_(symbol_list))
    if start_date:
        filters.append(MarketDataEOD.trade_date >= start_date)
    if end_date:
        filters.append(MarketDataEOD.trade_date <= end_date)

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    # Get total count
    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    # Apply pagination
    offset = (page - 1) * page_size
    query = (
        query.offset(offset)
        .limit(page_size)
        .order_by(Instrument.symbol, MarketDataEOD.trade_date.desc())
    )

    # Execute query
    result = await db.execute(query)
    rows = result.all()

    # Calculate total pages
    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return EODListResponse(
        success=True,
        data=[
            EODResponse(
                instrument_id=eod.instrument_id,
                symbol=inst.symbol,
                name=inst.name,
                market=inst.market,
                trade_date=eod.trade_date,
                open=eod.open,
                high=eod.high,
                low=eod.low,
                close=eod.close,
                volume=eod.volume,
                total_ticks=eod.total_ticks,
                turnover=eod.turnover,
                source=eod.source,
                created_at=eod.created_at,
                updated_at=eod.updated_at,
            )
            for eod, inst in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/eod/{instrument_id}", response_model=EODListResponse)
async def get_instrument_eod(
    instrument_id: UUID,
    start_date: Optional[date] = Query(None, description="起始日期 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="結束日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """取得指定商品的日線資料。"""
    # Check instrument exists
    inst_result = await db.execute(
        select(Instrument).where(Instrument.instrument_id == instrument_id)
    )
    instrument = inst_result.scalar_one_or_none()

    if not instrument:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument {instrument_id} not found",
        )

    # Build query
    query = select(MarketDataEOD).where(MarketDataEOD.instrument_id == instrument_id)
    count_query = (
        select(func.count())
        .select_from(MarketDataEOD)
        .where(MarketDataEOD.instrument_id == instrument_id)
    )

    # Apply date filters
    if start_date:
        query = query.where(MarketDataEOD.trade_date >= start_date)
        count_query = count_query.where(MarketDataEOD.trade_date >= start_date)
    if end_date:
        query = query.where(MarketDataEOD.trade_date <= end_date)
        count_query = count_query.where(MarketDataEOD.trade_date <= end_date)

    # Get total count
    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    # Apply pagination
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(MarketDataEOD.trade_date.desc())

    # Execute query
    result = await db.execute(query)
    eod_records = result.scalars().all()

    # Calculate total pages
    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return EODListResponse(
        success=True,
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
            )
            for eod in eod_records
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/corporate-actions", response_model=CorporateActionListResponse)
async def list_corporate_actions(
    market: Optional[str] = Query(None, description="依市場篩選"),
    symbols: Optional[str] = Query(None, description="以逗號分隔的商品代號"),
    action_type: Optional[str] = Query(None, description="依公司行動類型篩選"),
    start_date: Optional[date] = Query(None, description="除權息起始日 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="除權息結束日 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出公司行動，支援選用篩選條件。"""
    query = select(CorporateAction, Instrument).join(
        Instrument, CorporateAction.instrument_id == Instrument.instrument_id
    )
    count_query = select(func.count(CorporateAction.action_id)).join(
        Instrument, CorporateAction.instrument_id == Instrument.instrument_id
    )

    filters = []
    if market:
        filters.append(Instrument.market == market.upper())
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",")]
        filters.append(Instrument.symbol.in_(symbol_list))
    if action_type:
        filters.append(CorporateAction.action_type == action_type.lower())
    if start_date:
        filters.append(CorporateAction.ex_date >= start_date)
    if end_date:
        filters.append(CorporateAction.ex_date <= end_date)

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = (
        query.offset(offset)
        .limit(page_size)
        .order_by(Instrument.symbol, CorporateAction.ex_date.desc())
    )

    result = await db.execute(query)
    rows = result.all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return CorporateActionListResponse(
        success=True,
        data=[
            CorporateActionResponse(
                action_id=action.action_id,
                instrument_id=action.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                market=instrument.market,
                action_type=action.action_type,
                ex_date=action.ex_date,
                record_date=action.record_date,
                pay_date=action.pay_date,
                ratio=action.ratio,
                cash_amount=action.cash_amount,
                currency=action.currency,
                source=action.source,
                extra=action.extra,
            )
            for action, instrument in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/corporate-actions/{instrument_id}", response_model=CorporateActionListResponse)
async def get_instrument_corporate_actions(
    instrument_id: UUID,
    action_type: Optional[str] = Query(None, description="依公司行動類型篩選"),
    start_date: Optional[date] = Query(None, description="除權息起始日 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="除權息結束日 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """取得指定商品的公司行動。"""
    inst_result = await db.execute(
        select(Instrument).where(Instrument.instrument_id == instrument_id)
    )
    instrument = inst_result.scalar_one_or_none()

    if not instrument:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument {instrument_id} not found",
        )

    query = select(CorporateAction).where(CorporateAction.instrument_id == instrument_id)
    count_query = select(func.count(CorporateAction.action_id)).where(
        CorporateAction.instrument_id == instrument_id
    )

    filters = []
    if action_type:
        filters.append(CorporateAction.action_type == action_type.lower())
    if start_date:
        filters.append(CorporateAction.ex_date >= start_date)
    if end_date:
        filters.append(CorporateAction.ex_date <= end_date)

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(CorporateAction.ex_date.desc())

    result = await db.execute(query)
    actions = result.scalars().all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return CorporateActionListResponse(
        success=True,
        data=[
            CorporateActionResponse(
                action_id=action.action_id,
                instrument_id=action.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                market=instrument.market,
                action_type=action.action_type,
                ex_date=action.ex_date,
                record_date=action.record_date,
                pay_date=action.pay_date,
                ratio=action.ratio,
                cash_amount=action.cash_amount,
                currency=action.currency,
                source=action.source,
                extra=action.extra,
            )
            for action in actions
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/macro/series", response_model=MacroSeriesListResponse)
async def list_macro_series(
    market: Optional[str] = Query(None, description="依市場篩選"),
    source: Optional[str] = Query(None, description="依資料來源篩選"),
    source_code: Optional[str] = Query(None, description="依來源代碼篩選"),
    name: Optional[str] = Query(None, description="依時間序列名稱篩選"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出總經時間序列。"""
    query = select(MacroSeries)
    count_query = select(func.count(MacroSeries.series_id))

    filters = []
    if market:
        filters.append(MacroSeries.market == market.upper())
    if source:
        filters.append(MacroSeries.source == source.lower())
    if source_code:
        filters.append(MacroSeries.source_code == source_code)
    if name:
        filters.append(MacroSeries.name.ilike(f"%{name}%"))

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(MacroSeries.name)

    result = await db.execute(query)
    series_list = result.scalars().all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return MacroSeriesListResponse(
        success=True,
        data=[
            MacroSeriesResponse(
                series_id=series.series_id,
                name=series.name,
                unit=series.unit,
                frequency=series.frequency,
                market=series.market,
                source_code=series.source_code,
                source=series.source,
            )
            for series in series_list
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/macro/observations", response_model=MacroObservationListResponse)
async def list_macro_observations(
    market: Optional[str] = Query(None, description="依市場篩選"),
    source_code: Optional[str] = Query(None, description="依來源代碼篩選"),
    start_date: Optional[date] = Query(None, description="起始日期 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="結束日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出總經觀測值。"""
    query = select(MacroObservation, MacroSeries).join(
        MacroSeries, MacroObservation.series_id == MacroSeries.series_id
    )
    count_query = select(func.count(MacroObservation.id)).join(
        MacroSeries, MacroObservation.series_id == MacroSeries.series_id
    )

    filters = []
    if market:
        filters.append(MacroSeries.market == market.upper())
    if source_code:
        filters.append(MacroSeries.source_code == source_code)
    if start_date:
        filters.append(MacroObservation.obs_date >= start_date)
    if end_date:
        filters.append(MacroObservation.obs_date <= end_date)

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = (
        query.offset(offset)
        .limit(page_size)
        .order_by(MacroSeries.source_code, MacroObservation.obs_date.desc())
    )

    result = await db.execute(query)
    rows = result.all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return MacroObservationListResponse(
        success=True,
        data=[
            MacroObservationResponse(
                id=obs.id,
                series_id=series.series_id,
                series_name=series.name,
                obs_date=obs.obs_date,
                value=obs.value,
                source=obs.source,
            )
            for obs, series in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/macro/observations/{series_id}", response_model=MacroObservationListResponse)
async def get_macro_observations(
    series_id: UUID,
    start_date: Optional[date] = Query(None, description="起始日期 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="結束日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """取得指定總經時間序列的觀測值。"""
    series_result = await db.execute(select(MacroSeries).where(MacroSeries.series_id == series_id))
    series = series_result.scalar_one_or_none()

    if not series:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Series {series_id} not found",
        )

    query = select(MacroObservation).where(MacroObservation.series_id == series_id)
    count_query = select(func.count(MacroObservation.id)).where(
        MacroObservation.series_id == series_id
    )

    if start_date:
        query = query.where(MacroObservation.obs_date >= start_date)
        count_query = count_query.where(MacroObservation.obs_date >= start_date)
    if end_date:
        query = query.where(MacroObservation.obs_date <= end_date)
        count_query = count_query.where(MacroObservation.obs_date <= end_date)

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(MacroObservation.obs_date.desc())

    result = await db.execute(query)
    observations = result.scalars().all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return MacroObservationListResponse(
        success=True,
        data=[
            MacroObservationResponse(
                id=obs.id,
                series_id=series.series_id,
                series_name=series.name,
                obs_date=obs.obs_date,
                value=obs.value,
                source=obs.source,
            )
            for obs in observations
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/futures/contracts", response_model=FuturesContractListResponse)
async def list_futures_contracts(
    market: Optional[str] = Query(None, description="依市場篩選"),
    symbols: Optional[str] = Query(None, description="以逗號分隔的商品代號"),
    contract_code: Optional[str] = Query(None, description="依合約代碼篩選"),
    start_expiry: Optional[date] = Query(None, description="到期日起始日 (YYYY-MM-DD)"),
    end_expiry: Optional[date] = Query(None, description="到期日結束日 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出期貨合約。"""
    query = select(FuturesContract, Instrument).join(
        Instrument, FuturesContract.instrument_id == Instrument.instrument_id
    )
    count_query = select(func.count(FuturesContract.contract_id)).join(
        Instrument, FuturesContract.instrument_id == Instrument.instrument_id
    )

    filters = []
    if market:
        filters.append(Instrument.market == market.upper())
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",")]
        filters.append(Instrument.symbol.in_(symbol_list))
    if contract_code:
        filters.append(FuturesContract.contract_code == contract_code)
    if start_expiry:
        filters.append(FuturesContract.expiry_date >= start_expiry)
    if end_expiry:
        filters.append(FuturesContract.expiry_date <= end_expiry)

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = (
        query.offset(offset)
        .limit(page_size)
        .order_by(Instrument.symbol, FuturesContract.expiry_date.desc())
    )

    result = await db.execute(query)
    rows = result.all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return FuturesContractListResponse(
        success=True,
        data=[
            FuturesContractResponse(
                contract_id=contract.contract_id,
                instrument_id=instrument.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                contract_code=contract.contract_code,
                contract_month=contract.contract_month,
                expiry_date=contract.expiry_date,
                currency=contract.currency,
                source=contract.source,
                extra=contract.extra,
            )
            for contract, instrument in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/futures/continuous", response_model=FuturesContinuousListResponse)
async def list_futures_continuous(
    market: Optional[str] = Query(None, description="依市場篩選"),
    symbols: Optional[str] = Query(None, description="以逗號分隔的商品代號"),
    start_date: Optional[date] = Query(None, description="起始日期 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="結束日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出連續期貨日線資料。"""
    query = (
        select(FuturesContinuousEOD, Instrument, RollRule)
        .join(Instrument, FuturesContinuousEOD.instrument_id == Instrument.instrument_id)
        .outerjoin(RollRule, FuturesContinuousEOD.roll_rule_id == RollRule.rule_id)
    )
    count_query = (
        select(func.count(FuturesContinuousEOD.id))
        .join(Instrument, FuturesContinuousEOD.instrument_id == Instrument.instrument_id)
        .outerjoin(RollRule, FuturesContinuousEOD.roll_rule_id == RollRule.rule_id)
    )

    filters = []
    if market:
        filters.append(Instrument.market == market.upper())
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",")]
        filters.append(Instrument.symbol.in_(symbol_list))
    if start_date:
        filters.append(FuturesContinuousEOD.trade_date >= start_date)
    if end_date:
        filters.append(FuturesContinuousEOD.trade_date <= end_date)

    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = (
        query.offset(offset)
        .limit(page_size)
        .order_by(Instrument.symbol, FuturesContinuousEOD.trade_date.desc())
    )

    result = await db.execute(query)
    rows = result.all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return FuturesContinuousListResponse(
        success=True,
        data=[
            FuturesContinuousResponse(
                id=eod.id,
                instrument_id=instrument.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                trade_date=eod.trade_date,
                open=eod.open,
                high=eod.high,
                low=eod.low,
                close=eod.close,
                volume=eod.volume,
                turnover=eod.turnover,
                source=eod.source,
                roll_rule_id=eod.roll_rule_id,
                roll_rule_name=rule.name if rule else None,
            )
            for eod, instrument, rule in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/futures/continuous/{instrument_id}", response_model=FuturesContinuousListResponse)
async def get_futures_continuous(
    instrument_id: UUID,
    start_date: Optional[date] = Query(None, description="起始日期 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="結束日期 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """取得指定商品的連續期貨日線資料。"""
    inst_result = await db.execute(
        select(Instrument).where(Instrument.instrument_id == instrument_id)
    )
    instrument = inst_result.scalar_one_or_none()

    if not instrument:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instrument {instrument_id} not found",
        )

    query = (
        select(FuturesContinuousEOD, RollRule)
        .outerjoin(RollRule, FuturesContinuousEOD.roll_rule_id == RollRule.rule_id)
        .where(FuturesContinuousEOD.instrument_id == instrument_id)
    )
    count_query = select(func.count(FuturesContinuousEOD.id)).where(
        FuturesContinuousEOD.instrument_id == instrument_id
    )

    if start_date:
        query = query.where(FuturesContinuousEOD.trade_date >= start_date)
        count_query = count_query.where(FuturesContinuousEOD.trade_date >= start_date)
    if end_date:
        query = query.where(FuturesContinuousEOD.trade_date <= end_date)
        count_query = count_query.where(FuturesContinuousEOD.trade_date <= end_date)

    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(FuturesContinuousEOD.trade_date.desc())

    result = await db.execute(query)
    rows = result.all()

    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return FuturesContinuousListResponse(
        success=True,
        data=[
            FuturesContinuousResponse(
                id=eod.id,
                instrument_id=instrument.instrument_id,
                symbol=instrument.symbol,
                name=instrument.name,
                trade_date=eod.trade_date,
                open=eod.open,
                high=eod.high,
                low=eod.low,
                close=eod.close,
                volume=eod.volume,
                turnover=eod.turnover,
                source=eod.source,
                roll_rule_id=eod.roll_rule_id,
                roll_rule_name=rule.name if rule else None,
            )
            for eod, rule in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )


@router.get("/calendar", response_model=CalendarListResponse)
async def list_calendar(
    market: str = Query(..., description="市場代碼，必填"),
    start_date: Optional[date] = Query(None, description="起始日期 (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="結束日期 (YYYY-MM-DD)"),
    is_open: Optional[bool] = Query(None, description="依是否為交易日篩選"),
    page: int = Query(1, ge=1, description="頁碼"),
    page_size: int = Query(100, ge=1, le=1000, description="每頁筆數"),
    _: str = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
):
    """取得指定市場的交易日曆。"""
    # Build query
    query = select(TradingCalendar).where(TradingCalendar.market == market.upper())
    count_query = select(func.count(TradingCalendar.id)).where(
        TradingCalendar.market == market.upper()
    )

    # Apply filters
    if start_date:
        query = query.where(TradingCalendar.trade_date >= start_date)
        count_query = count_query.where(TradingCalendar.trade_date >= start_date)
    if end_date:
        query = query.where(TradingCalendar.trade_date <= end_date)
        count_query = count_query.where(TradingCalendar.trade_date <= end_date)
    if is_open is not None:
        query = query.where(TradingCalendar.is_open == is_open)
        count_query = count_query.where(TradingCalendar.is_open == is_open)

    # Get total count
    total_result = await db.execute(count_query)
    total_records = int(total_result.scalar() or 0)

    # Apply pagination
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size).order_by(TradingCalendar.trade_date)

    # Execute query
    result = await db.execute(query)
    calendar_records = result.scalars().all()

    # Calculate total pages
    total_pages = (total_records + page_size - 1) // page_size if total_records else 0

    return CalendarListResponse(
        success=True,
        data=[
            CalendarResponse(
                market=cal.market,
                trade_date=cal.trade_date,
                is_open=cal.is_open,
                session_open=str(cal.session_open) if cal.session_open else None,
                session_close=str(cal.session_close) if cal.session_close else None,
                holiday_name=cal.holiday_name,
            )
            for cal in calendar_records
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total_records,
            total_pages=total_pages,
        ),
    )
