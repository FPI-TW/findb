"""Dedicated read-only endpoints for the Dashboard lookup experience."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_serve_api_key
from app.dependencies import get_db
from app.schemas.lookup import (
    InstrumentLookupResponse,
    InstrumentLookupSort,
    MacroSeriesLookupResponse,
    MacroSeriesLookupSort,
    SortDirection,
)
from app.services.lookup import lookup_instruments, lookup_macro_series

router = APIRouter()


@router.get("/instruments", response_model=InstrumentLookupResponse)
async def list_lookup_instruments(
    q: Annotated[str | None, Query(max_length=200)] = None,
    market: str | None = None,
    asset_class: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    sort_by: InstrumentLookupSort = "market",
    sort_dir: SortDirection = "asc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> InstrumentLookupResponse:
    """Return paginated instruments, global facets, and lookup statistics."""
    return await lookup_instruments(
        db,
        q=q,
        market=market,
        asset_class=asset_class,
        status=status_filter,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )


@router.get("/macro-series", response_model=MacroSeriesLookupResponse)
async def list_lookup_macro_series(
    q: Annotated[str | None, Query(max_length=200)] = None,
    market: str | None = None,
    frequency: str | None = None,
    source: str | None = None,
    sort_by: MacroSeriesLookupSort = "market",
    sort_dir: SortDirection = "asc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    _: str | None = Depends(verify_serve_api_key),
    db: AsyncSession = Depends(get_db),
) -> MacroSeriesLookupResponse:
    """Return paginated macro series and global facets."""
    return await lookup_macro_series(
        db,
        q=q,
        market=market,
        frequency=frequency,
        source=source,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )
