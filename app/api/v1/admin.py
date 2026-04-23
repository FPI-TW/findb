"""
Admin API endpoints.
Provides manual correction and DQ resolution capabilities for the canonical layer.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import verify_admin_api_key
from app.dependencies import get_db
from app.models.base import async_session_maker
from app.models.raw import RawMarketPayload
from app.models.registry import IngestionRun
from app.schemas.admin import (
    BulkRerunResponse,
    CorrectionListResponse,
    CorrectionResponse,
    DQIssueListResponse,
    DQIssueResponse,
    InstrumentCacheDocument,
    InstrumentCacheItemPatchRequest,
    InstrumentCacheItemUpdateResponse,
    InstrumentCacheReplaceRequest,
    InstrumentCacheWriteResponse,
    PatchEODRequest,
    PatchEODResponse,
    RawPayloadListResponse,
    RawPayloadResponse,
    ResolveDQIssueRequest,
    ResolveDQIssueResponse,
    CacheTriggerResponse
)
from app.schemas.common import PaginationInfo
from app.services.admin import (
    AlreadyResolvedError,
    NoChangesError,
    RecordNotFoundError,
    _mask_key,
    list_corrections,
    list_dq_issues,
    patch_eod_record,
    resolve_dq_issue,
)
from app.services.ingestion import (
    IngestionService,
    RawPayloadNotFoundError,
    trigger_normalization,
)
from app.services.instrument_cache import (
    InstrumentCacheItemNotFoundError,
    InstrumentCacheNotFoundError,
    InstrumentCacheValidationError,
    read_instrument_cache,
    replace_instrument_cache,
    update_instrument_cache_item,
)

from scripts.generate_instrument_cache import main as run_cache_generation

router = APIRouter()


@router.get("/instrument-cache", response_model=InstrumentCacheDocument)
async def get_instrument_cache(
    api_key: str = Depends(verify_admin_api_key),
):
    """Return the generated static instrument lookup cache."""
    try:
        return read_instrument_cache()
    except InstrumentCacheNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstrumentCacheValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.put("/instrument-cache", response_model=InstrumentCacheWriteResponse)
async def put_instrument_cache(
    body: InstrumentCacheReplaceRequest,
    api_key: str = Depends(verify_admin_api_key),
):
    """Replace instruments.json with a validated and normalized document."""
    try:
        payload = replace_instrument_cache(body.model_dump(mode="json"))
    except InstrumentCacheValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return InstrumentCacheWriteResponse(
        message="Instrument cache updated successfully",
        data=payload,
    )


@router.patch(
    "/instrument-cache/items/{instrument_id}",
    response_model=InstrumentCacheItemUpdateResponse,
)
async def patch_instrument_cache_item(
    instrument_id: str,
    body: InstrumentCacheItemPatchRequest,
    api_key: str = Depends(verify_admin_api_key),
):
    """Patch a single instrument inside the generated instruments.json cache."""
    try:
        item = update_instrument_cache_item(
            instrument_id,
            body.model_dump(mode="json", exclude_unset=True),
        )
    except InstrumentCacheNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstrumentCacheItemNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstrumentCacheValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return InstrumentCacheItemUpdateResponse(
        message="Instrument cache item updated successfully",
        data=item,
    )


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


@router.get("/raw-payloads", response_model=RawPayloadListResponse)
async def list_raw_payloads(
    dataset_key: Optional[str] = Query(None, description="Filter by dataset key"),
    run_id: Optional[UUID] = Query(None, description="Filter by ingestion run UUID"),
    date_from: Optional[date] = Query(
        None, description="Filter created_at >= this date (YYYY-MM-DD, UTC)"
    ),
    date_to: Optional[date] = Query(
        None, description="Filter created_at <= this date (YYYY-MM-DD, UTC)"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List raw market payloads, newest first. Optionally filter by dataset_key, run_id, or date range."""
    stmt = select(RawMarketPayload)
    count_stmt = select(func.count()).select_from(RawMarketPayload)

    if dataset_key:
        stmt = stmt.where(RawMarketPayload.dataset_key == dataset_key)
        count_stmt = count_stmt.where(RawMarketPayload.dataset_key == dataset_key)
    if run_id:
        stmt = stmt.where(RawMarketPayload.run_id == run_id)
        count_stmt = count_stmt.where(RawMarketPayload.run_id == run_id)
    if date_from:
        dt_from = datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
        stmt = stmt.where(RawMarketPayload.created_at >= dt_from)
        count_stmt = count_stmt.where(RawMarketPayload.created_at >= dt_from)
    if date_to:
        dt_to = datetime(date_to.year, date_to.month, date_to.day, tzinfo=timezone.utc) + timedelta(
            days=1
        )
        stmt = stmt.where(RawMarketPayload.created_at < dt_to)
        count_stmt = count_stmt.where(RawMarketPayload.created_at < dt_to)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(RawMarketPayload.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    total_pages = (total + page_size - 1) // page_size if total else 0
    return RawPayloadListResponse(
        data=[RawPayloadResponse.model_validate(r) for r in rows],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=total_pages,
        ),
    )


@router.get("/raw-payloads/{run_id}", response_model=RawPayloadResponse)
async def get_raw_payload_by_run(
    run_id: UUID,
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get the raw payload for a specific ingestion run."""
    stmt = select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raw payload not found")
    return RawPayloadResponse.model_validate(row)


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


@router.post("/runs/bulk-rerun", response_model=BulkRerunResponse)
async def bulk_rerun_runs(
    background_tasks: BackgroundTasks,
    dataset_key: Optional[str] = Query(None, description="Filter by dataset_key"),
    run_status: Optional[str] = Query(
        "completed",
        alias="status",
        description="Run status to include: completed / failed / all",
    ),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Rerun normalization for all existing runs matching the given filters.

    Creates a new ingestion run for each match and queues normalization as
    a background task. Returns immediately with a summary.
    """
    stmt = select(IngestionRun.run_id)
    if dataset_key:
        stmt = stmt.where(IngestionRun.dataset_key == dataset_key)
    if run_status and run_status != "all":
        stmt = stmt.where(IngestionRun.status == run_status)
    stmt = stmt.order_by(IngestionRun.created_at)

    result = await db.execute(stmt)
    run_ids = [row[0] for row in result.all()]

    if db.bind is None:
        session_factory = async_session_maker
    else:
        session_factory = async_sessionmaker(
            bind=db.bind,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    service = IngestionService(db)
    queued = 0
    skipped = 0
    errors = 0
    new_run_ids: list[str] = []
    error_details: list[str] = []

    for run_id in run_ids:
        try:
            new_run_id, _, dk, payload = await service.rerun_from_raw(run_id)
            background_tasks.add_task(
                trigger_normalization, dk, payload, new_run_id, session_factory
            )
            new_run_ids.append(str(new_run_id))
            queued += 1
        except RawPayloadNotFoundError:
            skipped += 1
        except Exception as e:
            errors += 1
            error_details.append(f"{run_id}: {e}")

    return BulkRerunResponse(
        queued=queued,
        skipped=skipped,
        errors=errors,
        new_run_ids=new_run_ids,
        error_details=error_details,
    )

@router.post("/instrument-cache/refresh", response_model=CacheTriggerResponse)
async def refresh_instrument_cache(
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_admin_api_key),
):
    """
    Manually refresh the static instrument cache (instruments.json). 
    This long-running task will be processed in the background.
    """
    # Define background task
    def task_wrapper():
        print("Starting background cache generation...")
        exit_code = run_cache_generation()
        if exit_code == 0:
            print("Background cache generation completed successfully.")
        else:
            print("Background cache generation failed.")

    # Assign task into FastAPI background task queue
    background_tasks.add_task(task_wrapper)

    return CacheTriggerResponse(
        message="Cache generation task has been queued in the background.",
        status="accepted"
    )
