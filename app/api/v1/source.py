"""
Source API endpoints.
Handles data ingestion from fetch layer.
"""

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import verify_source_api_key
from app.dependencies import get_db
from app.models.base import async_session_maker
from app.schemas.source import (
    DatasetInfo,
    DatasetListResponse,
    DirectIngestPayload,
    IngestRequest,
    IngestResponse,
    RunStatusResponse,
)
from app.services.ingestion import (
    DatasetInactiveError,
    DatasetNotFoundError,
    IngestionService,
    MarketMismatchError,
    PayloadValidationError,
    RawPayloadNotFoundError,
    trigger_normalization,
)
from app.utils import utc_now
from app.utils.datetime_utils import parse_datetime

router = APIRouter()


def _build_session_factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Build session factory for background tasks."""
    if db.bind is None:
        return async_session_maker

    return async_sessionmaker(
        bind=db.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def _normalize_source(value: str | None) -> str:
    """Normalize source label for ingestion records."""
    source = (value or "bloomberg").strip().lower()
    if source.startswith("bloomberg"):
        return "bloomberg"
    return source


def _build_direct_ingest_request(
    payload: DirectIngestPayload,
    dataset_key: str,
    key_prefix: str,
) -> IngestRequest:
    """Convert direct payload format to internal IngestRequest."""
    raw_payload = payload.model_dump()
    metadata = raw_payload.get("metadata", {}) or {}

    source = _normalize_source(metadata.get("source"))
    query_time = metadata.get("query_time")
    try:
        fetched_at = parse_datetime(str(query_time)) if query_time else utc_now()
    except ValueError:
        fetched_at = utc_now()

    digest = hashlib.sha256(
        json.dumps(raw_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    request_key = f"{key_prefix}_{digest}"

    return IngestRequest(
        dataset_key=dataset_key,
        source=source,
        request_key=request_key,
        idempotency_key=request_key,
        payload=raw_payload,
        fetched_at=fetched_at,
    )


async def _ingest_by_market(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    expected_market: str | None = None,
):
    """
    Ingest raw data from fetch layer for a market.

    - Validates API key
    - Validates dataset market matches endpoint market
    - Checks for duplicate request (idempotency)
    - Stores raw payload
    - Creates ingestion run
    """
    service = IngestionService(db)

    try:
        run_id, run_status, is_duplicate = await service.ingest(
            request,
            expected_market=expected_market,
        )
        session_factory = _build_session_factory(db)
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
    except MarketMismatchError as e:
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


async def _ingest_direct_payload(
    payload: DirectIngestPayload,
    dataset_key: str,
    key_prefix: str,
    expected_market: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> IngestResponse:
    """Ingest direct market payload with inferred ingestion fields."""
    request = _build_direct_ingest_request(payload, dataset_key, key_prefix)
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market=expected_market,
    )


@router.post("/ingest/crypto", response_model=IngestResponse)
async def ingest_crypto_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for CRYPTO market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="CRYPTO",
    )


@router.post("/ingest/us", response_model=IngestResponse)
async def ingest_us_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for US market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="US",
    )


@router.post("/ingest/usstock/direct", response_model=IngestResponse)
async def ingest_usstock_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest Bloomberg US stock direct payload format."""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="us_stock_eod",
        key_prefix="direct_usstock",
        expected_market="US",
        background_tasks=background_tasks,
        db=db,
    )


@router.post("/ingest/hkchina/direct", response_model=IngestResponse)
async def ingest_hkchina_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest Bloomberg HK/China stock direct payload format."""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="hkchina_stock_eod",
        key_prefix="direct_hkchina",
        expected_market="GLOBAL",
        background_tasks=background_tasks,
        db=db,
    )


@router.post("/ingest/fx", response_model=IngestResponse)
async def ingest_fx_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for FX market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="FX",
    )


@router.post("/ingest/macro", response_model=IngestResponse)
async def ingest_macro_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for MACRO market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="MACRO",
    )


@router.post("/ingest/crypto/direct", response_model=IngestResponse)
async def ingest_crypto_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest Bloomberg crypto direct payload format."""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="crypto_eod",
        key_prefix="direct_crypto",
        expected_market="CRYPTO",
        background_tasks=background_tasks,
        db=db,
    )


@router.post("/ingest/macro/direct", response_model=IngestResponse)
async def ingest_macro_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest Bloomberg macro direct payload format."""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="macro_observation",
        key_prefix="direct_macro",
        expected_market="MACRO",
        background_tasks=background_tasks,
        db=db,
    )


@router.post("/ingest/wtx", response_model=IngestResponse)
async def ingest_wtx_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for WTX market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="WTX",
    )


@router.post("/ingest/global", response_model=IngestResponse)
async def ingest_global_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for GLOBAL market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="GLOBAL",
    )


@router.post("/ingest/tw", response_model=IngestResponse)
async def ingest_tw_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for TW market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="TW",
    )


@router.post("/ingest/hk", response_model=IngestResponse)
async def ingest_hk_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for HK market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="HK",
    )


@router.post("/ingest/cn", response_model=IngestResponse)
async def ingest_cn_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Ingest data for CN market."""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="CN",
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
        session_factory = _build_session_factory(db)
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
