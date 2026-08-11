"""Versioned Source API write paths.

The staging Source surface intentionally contains only the contract endpoint,
scheduler polling, run/attempt inspection, reruns, and dataset discovery.  All
market-specific legacy/direct routes were removed; providers must submit one
of the published versioned ingress contracts.
"""

import logging
from typing import Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_source_api_key
from app.dependencies import get_db
from app.schemas.source import (
    CanonicalIngestResponse,
    DatasetInfo,
    DatasetListResponse,
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
    normalize_scheduler_key,
    poll_scheduler_control,
    scheduler_dataset_keys,
)
from app.services.slot_identity import CanonicalSlotId, normalize_slot_id

router = APIRouter()
logger = logging.getLogger(__name__)


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
        scheduler_key=normalize_scheduler_key(row.scheduler_key),
        provider=row.provider,
        dataset_keys=scheduler_dataset_keys(row),
        slot_id=cast(CanonicalSlotId, normalize_slot_id(row.slot_id)),
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
