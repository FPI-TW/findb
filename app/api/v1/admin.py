"""
Admin API 端點。
提供 canonical 層資料的人工修正與資料品質問題處理能力。
"""

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_admin_api_key
from app.dependencies import get_db
from app.models.raw import RawMarketPayload
from app.models.registry import IngestionRun
from app.schemas.admin import (
    APIKeyCreateRequest,
    APIKeyCreateResponse,
    APIKeyListResponse,
    APIKeyResponse,
    BulkRerunResponse,
    CacheTriggerResponse,
    CorrectionListResponse,
    CorrectionResponse,
    DQIssueListResponse,
    DQIssueResponse,
    InstrumentCacheDocument,
    InstrumentCacheItem,
    InstrumentCacheItemPatchRequest,
    InstrumentCacheItemUpdateResponse,
    InstrumentCacheReplaceRequest,
    InstrumentCacheWriteResponse,
    PatchEODRequest,
    PatchEODResponse,
    QueueHealthResponse,
    RawPayloadListResponse,
    RawPayloadResponse,
    ResolveDQIssueRequest,
    ResolveDQIssueResponse,
    SourceClientCreateRequest,
    SourceClientCreateResponse,
    SourceClientListResponse,
    SourceClientResponse,
)
from app.schemas.common import PaginationInfo
from app.services.admin import (
    AlreadyResolvedError,
    InvalidCorrectionError,
    NoChangesError,
    RecordNotFoundError,
    list_corrections,
    list_dq_issues,
    patch_eod_record,
    present_correction_actor,
    resolve_dq_issue,
)
from app.services.api_keys import create_api_key, list_api_keys, revoke_api_key
from app.services.ingestion import IngestionService, RawPayloadNotFoundError
from app.services.instrument_cache import (
    InstrumentCacheItemNotFoundError,
    InstrumentCacheNotFoundError,
    InstrumentCacheValidationError,
    read_instrument_cache,
    replace_instrument_cache,
    update_instrument_cache_item,
)
from app.services.normalization_queue import queue_health
from app.services.source_clients import (
    create_source_client,
    list_source_clients,
    revoke_source_client,
)
from scripts.generate_instrument_cache import main as run_cache_generation

router = APIRouter()


@router.post("/source-clients", response_model=SourceClientCreateResponse)
async def create_source_client_endpoint(
    body: SourceClientCreateRequest,
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Create a provider identity; the plaintext key is returned once."""
    try:
        row, plaintext = await create_source_client(
            db,
            name=body.name,
            source_name=body.source_name,
            allowed_datasets=body.allowed_datasets,
            rate_limit_requests=body.rate_limit_requests,
            rate_limit_window=body.rate_limit_window,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Source client conflicts with an existing key")
    return SourceClientCreateResponse(
        api_key=plaintext,
        data=SourceClientResponse.model_validate(row),
    )


@router.get("/source-clients", response_model=SourceClientListResponse)
async def list_source_clients_endpoint(
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_source_clients(db)
    return SourceClientListResponse(data=[SourceClientResponse.model_validate(row) for row in rows])


@router.delete("/source-clients/{client_id}", response_model=SourceClientResponse)
async def revoke_source_client_endpoint(
    client_id: UUID,
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    row = await revoke_source_client(db, client_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Source client not found")
    return SourceClientResponse.model_validate(row)


@router.get("/queue/health", response_model=QueueHealthResponse)
async def queue_health_endpoint(
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Return DB-authoritative delivery and worker health."""
    return QueueHealthResponse(**await queue_health(db))


@router.post("/api-keys", response_model=APIKeyCreateResponse)
async def create_api_key_endpoint(
    body: APIKeyCreateRequest,
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Issue a hashed Serve API key. Plaintext is returned once."""
    row, plaintext = await create_api_key(
        db,
        owner=body.owner,
        tier=body.tier,
        scopes=body.scopes,
        rate_limit_requests=body.rate_limit_requests,
        rate_limit_window=body.rate_limit_window,
        page_size_limit=body.page_size_limit,
    )
    return APIKeyCreateResponse(api_key=plaintext, data=APIKeyResponse.model_validate(row))


@router.get("/api-keys", response_model=APIKeyListResponse)
async def list_api_keys_endpoint(
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List API key metadata without hashes or plaintext."""
    rows = await list_api_keys(db)
    return APIKeyListResponse(data=[APIKeyResponse.model_validate(row) for row in rows])


@router.delete("/api-keys/{key_id}", response_model=APIKeyResponse)
async def revoke_api_key_endpoint(
    key_id: UUID,
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key."""
    row = await revoke_api_key(db, key_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return APIKeyResponse.model_validate(row)


@router.get("/instrument-cache", response_model=InstrumentCacheDocument)
async def get_instrument_cache(
    api_key: str = Depends(verify_admin_api_key),
):
    """回傳已產生的靜態商品查詢快取。"""
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
    """以已驗證且正規化的文件替換 instruments.json。"""
    try:
        payload = replace_instrument_cache(body.model_dump(mode="json"))
    except InstrumentCacheValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return InstrumentCacheWriteResponse(
        message="Instrument cache updated successfully",
        data=InstrumentCacheDocument.model_validate(payload),
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
    """修補產生後 instruments.json 快取中的單一商品。"""
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
        data=InstrumentCacheItem.model_validate(item),
    )


@router.get("/dq-issues", response_model=DQIssueListResponse)
async def list_dq_issues_endpoint(
    resolved: Optional[bool] = Query(None, description="依是否已解決篩選"),
    instrument_id: Optional[UUID] = Query(None, description="依商品 UUID 篩選"),
    severity: Optional[str] = Query(None, description="依嚴重程度篩選：warning / error"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出資料品質問題，可依解決狀態、商品或嚴重程度篩選。"""
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
    """修補 MarketDataEOD 紀錄的 OHLCV 欄位，並建立不可變的修正稽核紀錄。"""
    try:
        correction, eod = await patch_eod_record(db, instrument_id, trade_date, body, api_key)
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except NoChangesError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return PatchEODResponse(
        correction_id=correction.id,
        record_id=correction.record_id,
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
    """以稽核原因將資料品質問題標記為已解決，並建立修正紀錄。"""
    try:
        correction, issue = await resolve_dq_issue(db, issue_id, body, api_key)
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except AlreadyResolvedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    resolved_at = issue.resolved_at
    if resolved_at is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DQ issue resolution did not set resolved_at",
        )

    return ResolveDQIssueResponse(
        correction_id=correction.id,
        issue_id=issue.id,
        resolved_at=resolved_at,
        message="DQ issue resolved successfully",
    )


@router.get("/raw-payloads", response_model=RawPayloadListResponse)
async def list_raw_payloads(
    dataset_key: Optional[str] = Query(None, description="依資料集 key 篩選"),
    run_id: Optional[UUID] = Query(None, description="依匯入執行 UUID 篩選"),
    date_from: Optional[date] = Query(
        None, description="篩選 created_at 大於或等於此日期 (YYYY-MM-DD, UTC)"
    ),
    date_to: Optional[date] = Query(
        None, description="篩選 created_at 小於或等於此日期 (YYYY-MM-DD, UTC)"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出原始市場資料，最新資料在前；可依 dataset_key、run_id 或日期區間篩選。"""
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
    """取得指定匯入執行的原始資料。"""
    stmt = select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raw payload not found")
    return RawPayloadResponse.model_validate(row)


@router.get("/corrections", response_model=CorrectionListResponse)
async def list_corrections_endpoint(
    table_name: Optional[str] = Query(None, description="依 canonical 資料表名稱篩選"),
    instrument_id: Optional[UUID] = Query(None, description="依商品 UUID 篩選"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出修正稽核紀錄，最新資料在前；可依資料表或商品篩選。"""
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
                corrected_by=present_correction_actor(c.corrected_by),
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
    dataset_key: Optional[str] = Query(None, description="依 dataset_key 篩選"),
    run_status: Optional[str] = Query(
        "completed",
        alias="status",
        description="要包含的執行狀態：completed / failed / all",
    ),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="單次最多排入的 run 數量，避免無界重跑全部歷史資料",
    ),
    api_key: str = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    """針對符合篩選條件的既有執行紀錄重新執行正規化。

    會為每筆符合條件的紀錄建立新的匯入執行、job 與 outbox event。
    此端點會立即回傳摘要。
    """
    stmt = select(IngestionRun.run_id)
    if dataset_key:
        stmt = stmt.where(IngestionRun.dataset_key == dataset_key)
    if run_status and run_status != "all":
        stmt = stmt.where(IngestionRun.status == run_status)
    stmt = stmt.order_by(IngestionRun.created_at).limit(limit)

    result = await db.execute(stmt)
    run_ids = [row[0] for row in result.all()]

    service = IngestionService(db)
    queued = 0
    skipped = 0
    errors = 0
    new_run_ids: list[str] = []
    error_details: list[str] = []

    for run_id in run_ids:
        try:
            new_run_id, _, _, _ = await service.rerun_from_raw(run_id)
            new_run_ids.append(str(new_run_id))
            queued += 1
        except RawPayloadNotFoundError:
            await db.rollback()
            skipped += 1
        except Exception as e:
            await db.rollback()
            errors += 1
            error_details.append(f"{run_id}: {e}")

    return BulkRerunResponse(
        queued=queued,
        skipped=skipped,
        errors=errors,
        new_run_ids=new_run_ids,
        error_details=error_details,
    )


@router.post(
    "/instrument-cache/refresh",
    response_model=CacheTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_instrument_cache(
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_admin_api_key),
):
    """
    手動刷新靜態商品快取 instruments.json。
    這個長時間執行的任務會在背景處理。
    """

    def task_wrapper():
        print("Starting background cache generation...")
        exit_code = run_cache_generation()
        if exit_code == 0:
            print("Background cache generation completed successfully.")
        else:
            print("Background cache generation failed.")

    background_tasks.add_task(task_wrapper)

    return CacheTriggerResponse(
        message="Cache generation task has been queued in the background.", status="accepted"
    )
