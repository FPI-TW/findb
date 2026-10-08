"""Versioned Source API write paths.

The staging Source surface intentionally contains only the contract endpoint,
scheduler polling, run/attempt inspection, reruns, and dataset discovery.  All
market-specific legacy/direct routes were removed; providers must submit one
of the published versioned ingress contracts.
"""

import logging
from datetime import date
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_source_api_key
from app.dependencies import get_db
from app.models.registry import DailyDeliveryPlan
from app.schemas.full_market import (
    DeliveryOutcomeRequest,
    DeliveryPlanCreateRequest,
    DeliveryPlanResponse,
    DeliverySummaryResponse,
    UniverseListResponse,
    UniverseReleaseDetailResponse,
    UniverseReleaseResponse,
    UniverseSubmitRequest,
)
from app.schemas.full_market_readiness import EnrollmentVerification, ReadinessReport
from app.schemas.source import (
    CanonicalIngestResponse,
    DatasetInfo,
    DatasetListResponse,
    HistoricalBackfillClaimResponse,
    HistoricalBackfillItemUpdateRequest,
    IngestionAttemptResponse,
    IngestResponse,
    IngressErrorDetail,
    IngressErrorResponse,
    RunStatusResponse,
    SchedulerControlPollRequest,
    SchedulerControlPollResponse,
)
from app.services.canonical_ingestion import (
    CanonicalIngestRejectionError,
    accept_canonical_ingest,
)
from app.services.full_market import (
    FullMarketError,
    create_plan,
    get_plan,
    get_universe,
    list_universes,
    record_outcome,
    submit_universe,
)
from app.services.full_market_admission import (
    AdmissionError,
    account_authorization,
    admission_projection,
    report_readiness,
    verify_enrollment,
)
from app.services.historical_backfill import (
    HistoricalBackfillConflictError,
    HistoricalBackfillError,
    HistoricalBackfillValidationError,
    claim_next_item,
    complete_item,
)
from app.services.ingestion import (
    DatasetAccessDeniedError,
    DatasetInactiveError,
    DatasetNotFoundError,
    IngestionService,
    RawPayloadNotFoundError,
    SourceIdentityMismatchError,
)
from app.services.ingestion_attempts import IngestionAttemptService
from app.services.ingress_contracts import (
    UnsupportedIngressContractError,
    get_contract_json_schema,
)
from app.services.scheduler_control import (
    SchedulerControlNotFoundError,
    SchedulerControlScopeError,
    poll_scheduler_control,
    scheduler_dataset_keys,
)
from app.services.slot_identity import CanonicalSlotId

router = APIRouter()
logger = logging.getLogger(__name__)


def _full_market_source_scope(
    db: AsyncSession, provider: str | None = None
) -> tuple[str, list[str] | None]:
    authenticated_provider = db.info.get("source_name")
    if not isinstance(authenticated_provider, str) or not authenticated_provider:
        raise HTTPException(status_code=403, detail="Source provider identity unavailable")
    if provider is not None and provider != authenticated_provider:
        raise HTTPException(status_code=403, detail="Source provider scope mismatch")
    datasets = db.info.get("allowed_datasets")
    if datasets is not None and not isinstance(datasets, list):
        raise HTTPException(status_code=403, detail="Source dataset scope unavailable")
    return authenticated_provider, datasets


def _full_market_http_error(exc: FullMarketError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


@router.post("/full-market/readiness")
async def report_full_market_readiness(
    body: ReadinessReport,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        return await report_readiness(db, body)
    except AdmissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/full-market/authorization")
async def full_market_authorization(
    consumer: str = "full_market",
    declaration_sha256: str = "",
    runtime_id: str = "",
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider, _ = _full_market_source_scope(db)
    projection = await admission_projection(db, provider, source=True)
    if declaration_sha256:
        account = await account_authorization(
            db, provider, consumer, declaration_sha256, runtime_id
        )
        return {**projection, **account}
    return projection


@router.get("/full-market/enrollment-verification", response_model=EnrollmentVerification)
async def full_market_enrollment_verification(
    enrollment_id: UUID,
    source_client_id: UUID,
    declaration_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    runtime_id: str = Query(min_length=1, max_length=200),
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> EnrollmentVerification:
    try:
        return await verify_enrollment(
            db,
            enrollment_id=enrollment_id,
            source_client_id=source_client_id,
            declaration_sha256=declaration_sha256,
            runtime_id=runtime_id,
        )
    except AdmissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/universes", response_model=UniverseReleaseResponse)
async def submit_universe_endpoint(
    body: UniverseSubmitRequest,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> UniverseReleaseResponse:
    _, datasets = _full_market_source_scope(db, body.provider)
    try:
        return await submit_universe(db, body, allowed_datasets=datasets)
    except FullMarketError as exc:
        raise _full_market_http_error(exc) from exc


@router.get("/universes", response_model=UniverseListResponse)
async def list_universes_endpoint(
    dataset_key: str,
    as_of: date | None = None,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> UniverseListResponse:
    provider, datasets = _full_market_source_scope(db)
    try:
        return await list_universes(
            db, dataset_key=dataset_key, provider=provider, allowed_datasets=datasets, as_of=as_of
        )
    except FullMarketError as exc:
        raise _full_market_http_error(exc) from exc


@router.get("/universes/{release_id}", response_model=UniverseReleaseDetailResponse)
async def get_universe_endpoint(
    release_id: UUID,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> UniverseReleaseDetailResponse:
    provider, datasets = _full_market_source_scope(db)
    try:
        return await get_universe(db, release_id, provider=provider, allowed_datasets=datasets)
    except FullMarketError as exc:
        raise _full_market_http_error(exc) from exc


@router.post("/delivery-plans", response_model=DeliveryPlanResponse)
async def create_delivery_plan_endpoint(
    body: DeliveryPlanCreateRequest,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> DeliveryPlanResponse:
    _, datasets = _full_market_source_scope(db, body.provider)
    try:
        return await create_plan(db, body, allowed_datasets=datasets)
    except FullMarketError as exc:
        raise _full_market_http_error(exc) from exc


@router.get("/delivery-plans/{plan_id}", response_model=DeliveryPlanResponse)
async def get_delivery_plan_endpoint(
    plan_id: UUID,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> DeliveryPlanResponse:
    provider, datasets = _full_market_source_scope(db)
    try:
        return await get_plan(db, plan_id, provider=provider, allowed_datasets=datasets)
    except FullMarketError as exc:
        raise _full_market_http_error(exc) from exc


@router.get("/delivery-plans")
async def list_delivery_plans_endpoint(
    dataset_key: str,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 100,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    provider, datasets = _full_market_source_scope(db)
    if not datasets or dataset_key not in datasets:
        raise HTTPException(status_code=403, detail="Source dataset scope denied")
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    stmt = select(DailyDeliveryPlan).where(
        DailyDeliveryPlan.dataset_key == dataset_key,
        DailyDeliveryPlan.provider == provider,
    )
    if start_date is not None:
        stmt = stmt.where(DailyDeliveryPlan.trade_date >= start_date)
    if end_date is not None:
        stmt = stmt.where(DailyDeliveryPlan.trade_date <= end_date)
    plans = (
        await db.scalars(stmt.order_by(DailyDeliveryPlan.trade_date.desc()).limit(limit))
    ).all()
    responses = [
        await get_plan(db, plan.plan_id, provider=provider, allowed_datasets=datasets)
        for plan in plans
    ]
    return {"data": [response.model_dump(mode="json") for response in responses]}


@router.post("/delivery-plans/{plan_id}/outcomes", response_model=DeliverySummaryResponse)
async def record_delivery_outcome_endpoint(
    plan_id: UUID,
    body: DeliveryOutcomeRequest,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
) -> DeliverySummaryResponse:
    provider, datasets = _full_market_source_scope(db)
    try:
        return await record_outcome(db, plan_id, body, provider=provider, allowed_datasets=datasets)
    except FullMarketError as exc:
        raise _full_market_http_error(exc) from exc


@router.post("/historical-backfills/claim", response_model=HistoricalBackfillClaimResponse)
async def claim_historical_backfill_item(
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Lease one safe date for the authenticated provider without exposing credentials."""
    provider = db.info.get("source_name")
    if not isinstance(provider, str) or not provider:
        raise HTTPException(status_code=403, detail="Source provider identity unavailable")
    datasets = db.info.get("allowed_datasets")
    if datasets is not None and not isinstance(datasets, list):
        raise HTTPException(status_code=403, detail="Source dataset scope unavailable")
    item = await claim_next_item(db, provider=provider, allowed_datasets=datasets)
    await db.commit()
    if item is None:
        return HistoricalBackfillClaimResponse()
    return HistoricalBackfillClaimResponse(
        item_id=item.item_id,
        request_id=item.request_id,
        request_key=item.request.request_key,
        provider=item.request.provider,
        dataset_key=item.request.dataset_key,
        market=item.request.market,
        trade_date=item.trade_date,
        lease_token=item.lease_token,
    )


@router.post(
    "/historical-backfills/{item_id}/complete", response_model=HistoricalBackfillClaimResponse
)
async def complete_historical_backfill_item(
    item_id: UUID,
    body: HistoricalBackfillItemUpdateRequest,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    provider = db.info.get("source_name")
    if not isinstance(provider, str) or not provider:
        raise HTTPException(status_code=403, detail="Source provider identity unavailable")
    if body.status == "completed" and (body.failure_code or body.failure_message):
        raise HTTPException(status_code=422, detail="completed item cannot include failure details")
    try:
        item = await complete_item(
            db,
            item_id,
            provider=provider,
            outcome=body.status,
            lease_token=body.lease_token,
            run_id=body.run_id,
            failure_code=body.failure_code,
            failure_message=body.failure_message,
        )
    except HistoricalBackfillConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HistoricalBackfillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HistoricalBackfillError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Completion is audited as a provider-system action, never as a credential value.
    from app.api.deps import AdminPrincipal
    from app.services.admin_audit import record_admin_audit

    await record_admin_audit(
        db,
        AdminPrincipal("system", provider, provider, "operator"),
        action="complete",
        resource_type="historical_backfill",
        resource_id=str(item.request_id),
        details={
            "item_id": str(item.item_id),
            "status": item.status,
            "trade_date": item.trade_date.isoformat(),
        },
    )
    return HistoricalBackfillClaimResponse(
        item_id=item.item_id,
        request_id=item.request_id,
        request_key=item.request.request_key,
        provider=item.request.provider,
        dataset_key=item.request.dataset_key,
        market=item.request.market,
        trade_date=item.trade_date,
    )


@router.get("/contracts/{schema_id}/versions/{schema_version}")
async def get_ingress_contract_schema(
    schema_id: str,
    schema_version: int,
    _api_key: str = Depends(verify_source_api_key),
) -> dict[str, Any]:
    """Publish one immutable, machine-readable ingress JSON Schema."""
    try:
        return get_contract_json_schema(schema_id, schema_version)
    except UnsupportedIngressContractError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc


@router.post(
    "/scheduler-controls/{scheduler_key}/poll",
    response_model=SchedulerControlPollResponse,
)
async def poll_scheduler(
    scheduler_key: str,
    body: SchedulerControlPollRequest,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Return desired state and record a provider-scoped scheduler heartbeat."""
    try:
        row, server_time = await poll_scheduler_control(
            db,
            scheduler_key=scheduler_key,
            observed_state=body.observed_state,
            cycle_started_at=body.cycle_started_at,
            cycle_completed_at=body.cycle_completed_at,
            last_error=body.last_error,
        )
    except SchedulerControlNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Scheduler not found") from exc
    except SchedulerControlScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return SchedulerControlPollResponse(
        scheduler_key=row.scheduler_key,
        provider=row.provider,
        dataset_keys=scheduler_dataset_keys(row),
        slot_id=cast(CanonicalSlotId, row.slot_id),
        scheduled_local_time=row.scheduled_local_time,
        timezone=row.timezone,
        desired_state=cast(Literal["running", "stopped"], row.desired_state),
        revision=row.revision,
        server_time=server_time,
    )


def _ingress_error_response(
    attempt_id: UUID | None,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = IngressErrorResponse(
        attempt_id=attempt_id,
        error=IngressErrorDetail(code=code, message=message),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=headers,
    )


@router.post(
    "/ingest",
    response_model=CanonicalIngestResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
    responses={
        400: {"model": IngressErrorResponse},
        403: {"model": IngressErrorResponse},
        409: {"model": IngressErrorResponse},
        422: {"model": IngressErrorResponse},
        500: {"model": IngressErrorResponse},
        503: {"model": IngressErrorResponse},
    },
)
async def ingest_canonical_contract(
    request: Request,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Accept a provider-neutral, explicitly versioned ingress contract."""
    raw_body = await request.body()
    try:
        result = await accept_canonical_ingest(db, raw_body)
    except (OperationalError, DBAPIError):
        logger.exception("Database unavailable while creating ingestion attempt")
        return _ingress_error_response(
            None,
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            code="DATABASE_UNAVAILABLE",
            message="Ingestion is temporarily unavailable",
            headers={"Retry-After": "30"},
        )
    except CanonicalIngestRejectionError as exc:
        return _ingress_error_response(
            exc.attempt_id,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            headers=exc.headers,
        )

    return CanonicalIngestResponse(
        attempt_id=result.attempt_id,
        run_id=result.run_id,
        status=result.run_status,
        schema_id=result.request.schema_id,
        schema_version=result.request.schema_version,
        message=(
            "Duplicate idempotency_key, returning existing run"
            if result.is_duplicate
            else "Data received, processing queued"
        ),
    )


@router.get("/attempts/{attempt_id}", response_model=IngestionAttemptResponse)
async def get_ingestion_attempt(
    attempt_id: UUID,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Return one canonical ingestion attempt in the caller's ownership scope."""
    attempt = await IngestionAttemptService(db).get(attempt_id)
    if attempt is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Ingestion attempt {attempt_id} not found",
        )
    return IngestionAttemptResponse.model_validate(attempt)


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: UUID,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """取得資料匯入執行狀態。"""
    run = await IngestionService(db).get_run_status(run_id)
    if not run:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    return RunStatusResponse(
        run_id=run.run_id,
        dataset_key=run.dataset_key,
        schema_id=run.schema_id,
        schema_version=run.schema_version,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        total_records=run.total_records,
        success_records=run.success_records,
        failed_records=run.failed_records,
        error_message=run.error_message,
        failure_code=run.failure_code,
        attempt_count=run.attempt_count,
        max_attempts=run.max_attempts,
        next_retry_at=run.next_retry_at,
    )


@router.post("/runs/{run_id}/rerun", response_model=IngestResponse, status_code=202)
async def rerun_from_raw(
    run_id: UUID,
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Requeue a retained, versioned contract payload for normalization."""
    try:
        new_run_id, run_status, _, _ = await IngestionService(db).rerun_from_raw(run_id)
        return IngestResponse(
            success=True,
            run_id=new_run_id,
            status=run_status,
            message=f"Rerun durably queued from raw payload {run_id}",
        )
    except RawPayloadNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DatasetInactiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (DatasetAccessDeniedError, SourceIdentityMismatchError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OperationalError, DBAPIError):
        logger.exception("Database unavailable while accepting rerun")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is temporarily unavailable",
            headers={"Retry-After": "30"},
        ) from None
    except Exception as exc:
        logger.exception("Unexpected rerun error (run_id=%s)", run_id)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rerun failed due to internal server error",
        ) from exc


@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets(
    _api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出此 source credential 可用的四條 versioned feeds。"""
    datasets = await IngestionService(db).list_datasets()
    return DatasetListResponse(
        success=True,
        data=[
            DatasetInfo(
                dataset_key=ds.dataset_key,
                name=ds.name,
                description=ds.description,
                asset_class=ds.asset_class,
                market=ds.market,
                frequency=ds.frequency,
                is_active=ds.is_active,
                schema_id=(ds.config or {}).get("schema_id"),
                accepted_schema_versions=(ds.config or {}).get("accepted_schema_versions", []),
                current_schema_version=(ds.config or {}).get("current_schema_version"),
                schema_enforcement=(ds.config or {}).get("schema_enforcement"),
            )
            for ds in datasets
        ],
    )
