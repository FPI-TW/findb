"""
Admin API endpoints.
Provides manual correction and DQ resolution capabilities for the canonical layer.
"""

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_admin_api_key
from app.dependencies import get_db
from app.schemas.admin import (
    CorrectionListResponse,
    CorrectionResponse,
    DQIssueListResponse,
    DQIssueResponse,
    PatchEODRequest,
    PatchEODResponse,
    ResolveDQIssueRequest,
    ResolveDQIssueResponse,
)
from app.schemas.common import PaginationInfo
from app.services.admin import (
    AlreadyResolvedError,
    NoChangesError,
    RecordNotFoundError,
    list_corrections,
    list_dq_issues,
    patch_eod_record,
    resolve_dq_issue,
)
from app.services.admin import _mask_key

router = APIRouter()


@router.get("/dq-issues", response_model=DQIssueListResponse)
async def list_dq_issues_endpoint(
    resolved: Optional[bool] = Query(None, description="Filter by resolved status"),
    instrument_id: Optional[UUID] = Query(None, description="Filter by instrument UUID"),
    severity: Optional[str] = Query(None, description="Filter by severity (warning / error)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List DQ issues. Filter by resolved status, instrument, or severity."""
    rows, total = await list_dq_issues(db, resolved, instrument_id, severity, page, page_size)
    total_pages = (total + page_size - 1) // page_size if total else 0
    return DQIssueListResponse(
        data=[DQIssueResponse.model_validate(r) for r in rows],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=total_pages,
        ),
    )


@router.patch("/eod/{instrument_id}/{trade_date}", response_model=PatchEODResponse)
async def patch_eod(
    instrument_id: UUID,
    trade_date: date,
    body: PatchEODRequest,
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Patch OHLCV fields on a MarketDataEOD record. Creates an immutable audit correction row."""
    try:
        correction, eod = await patch_eod_record(db, instrument_id, trade_date, body, api_key)
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except NoChangesError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return PatchEODResponse(
        correction_id=correction.id,
        record_id=eod.id,
        instrument_id=eod.instrument_id,
        trade_date=eod.trade_date,
        message="EOD record corrected successfully",
    )


@router.patch("/dq-issues/{issue_id}/resolve", response_model=ResolveDQIssueResponse)
async def resolve_dq_issue_endpoint(
    issue_id: UUID,
    body: ResolveDQIssueRequest,
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Mark a DQ issue as resolved with an audit reason. Creates a correction row."""
    try:
        correction, issue = await resolve_dq_issue(db, issue_id, body, api_key)
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except AlreadyResolvedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return ResolveDQIssueResponse(
        correction_id=correction.id,
        issue_id=issue.id,
        resolved_at=issue.resolved_at,
        message="DQ issue resolved successfully",
    )


@router.get("/corrections", response_model=CorrectionListResponse)
async def list_corrections_endpoint(
    table_name: Optional[str] = Query(None, description="Filter by canonical table name"),
    instrument_id: Optional[UUID] = Query(None, description="Filter by instrument UUID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List correction audit records, newest first. Optionally filter by table or instrument."""
    rows, total = await list_corrections(db, table_name, instrument_id, page, page_size)
    total_pages = (total + page_size - 1) // page_size if total else 0

    return CorrectionListResponse(
        data=[
            CorrectionResponse(
                id=c.id,
                table_name=c.table_name,
                record_id=c.record_id,
                instrument_id=c.instrument_id,
                trade_date=c.trade_date,
                corrected_by=_mask_key(c.corrected_by),
                correction_reason=c.correction_reason,
                before_snapshot=c.before_snapshot,
                after_snapshot=c.after_snapshot,
                created_at=c.created_at,
            )
            for c in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=total_pages,
        ),
    )
