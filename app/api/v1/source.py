"""
Source API endpoints.
Handles data ingestion from fetch layer.
"""

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from fastapi import status as http_status

from app.dependencies import get_db
from app.models.base import async_session_maker
from app.api.deps import verify_source_api_key
from app.schemas.source import (
    IngestRequest,
    IngestResponse,
    RunStatusResponse,
    DatasetListResponse,
    DatasetInfo,
)
from app.services.ingestion import (
    IngestionService,
    DatasetNotFoundError,
    DatasetInactiveError,
    PayloadValidationError,
    RawPayloadNotFoundError,
    trigger_normalization,
)

router = APIRouter()


@router.post("/ingest", response_model=IngestResponse)
async def ingest_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """
    Ingest raw data from fetch layer.

    - Validates API key
    - Checks for duplicate request (idempotency)
    - Stores raw payload
    - Creates ingestion run
    """
    service = IngestionService(db)

    try:
        run_id, run_status, is_duplicate = await service.ingest(request)
        if db.bind is None:
            session_factory = async_session_maker
        else:
            session_factory = async_sessionmaker(
                bind=db.bind,
                class_=AsyncSession,
                expire_on_commit=False,
            )
        if not is_duplicate:
            background_tasks.add_task(
                trigger_normalization,
                request.dataset_key,
                request.payload,
                run_id,
                session_factory,
            )
        return IngestResponse(
            success=True,
            run_id=run_id,
            status=run_status,
            message=(
                "Duplicate idempotency_key, returning existing run"
                if is_duplicate
                else "Data received, processing queued"
            ),
        )
    except DatasetNotFoundError as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except DatasetInactiveError as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except PayloadValidationError as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {str(e)}",
        )


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(
    run_id: UUID,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get ingestion run status."""
    service = IngestionService(db)
    run = await service.get_run_status(run_id)

    if not run:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found",
        )

    return RunStatusResponse(
        run_id=run.run_id,
        dataset_key=run.dataset_key,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        total_records=run.total_records,
        success_records=run.success_records,
        failed_records=run.failed_records,
        error_message=run.error_message,
    )


@router.post("/runs/{run_id}/rerun", response_model=IngestResponse)
async def rerun_from_raw(
    run_id: UUID,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Re-run normalization using stored raw payload for a run."""
    service = IngestionService(db)

    try:
        new_run_id, run_status, dataset_key, payload = await service.rerun_from_raw(run_id)
        if db.bind is None:
            session_factory = async_session_maker
        else:
            session_factory = async_sessionmaker(
                bind=db.bind,
                class_=AsyncSession,
                expire_on_commit=False,
            )
        background_tasks.add_task(
            trigger_normalization,
            dataset_key,
            payload,
            new_run_id,
            session_factory,
        )
        return IngestResponse(
            success=True,
            run_id=new_run_id,
            status=run_status,
            message=f"Rerun queued from raw payload {run_id}",
        )
    except RawPayloadNotFoundError as e:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except DatasetNotFoundError as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except DatasetInactiveError as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except PayloadValidationError as e:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Rerun failed: {str(e)}",
        )


@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets(
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List available datasets."""
    service = IngestionService(db)
    datasets = await service.list_datasets()

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
            )
            for ds in datasets
        ],
    )
