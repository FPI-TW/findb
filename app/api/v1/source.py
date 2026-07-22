"""
Source API 端點。
處理 fetch 層送入的資料匯入請求。
"""

import copy
import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_source_api_key
from app.dependencies import get_db
from app.models.registry import DatasetRegistry
from app.schemas.source import (
    CanonicalIngestResponse,
    DatasetInfo,
    DatasetListResponse,
    DirectIngestPayload,
    IngestionAttemptResponse,
    IngestRequest,
    IngestResponse,
    IngressErrorDetail,
    IngressErrorResponse,
    RunStatusResponse,
    TWStockDirectIngestPayload,
)
from app.services.canonical_ingestion import (
    CanonicalIngestRejectionError,
    accept_canonical_ingest,
)
from app.services.ingestion import (
    DatasetInactiveError,
    DatasetNotFoundError,
    IdempotencyPayloadMismatchError,
    IngestionService,
    MarketMismatchError,
    PayloadValidationError,
    RawPayloadNotFoundError,
    SourceIdentityMismatchError,
)
from app.services.ingestion_attempts import IngestionAttemptService
from app.services.ingress_contracts import (
    UnsupportedIngressContractError,
    get_contract_json_schema,
)
from app.utils import utc_now
from app.utils.datetime_utils import parse_datetime

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


DIRECT_DATASET_DEFAULTS = {
    "us_stock_eod": {
        "name": "美股日K (Bloomberg API)",
        "description": "Bloomberg API 美國股票每日價格資料",
        "asset_class": "equity",
        "market": "US",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_usstock_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    "hkchina_mixed_eod": {
        "name": "港中股票與指數日K (Bloomberg API)",
        "description": "Bloomberg API 港股與中國相關股票及指數每日價格資料",
        "asset_class": "mixed",
        "market": "GLOBAL",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_mixed_api",
            "data_path": "data",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            },
        },
    },
    "hkchina_stock_eod": {
        "name": "港中股票日K (Bloomberg API)",
        "description": "Bloomberg API 港股與中國相關股票每日價格資料",
        "asset_class": "equity",
        "market": "GLOBAL",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    "hkchina_index_eod": {
        "name": "港中指數日K (Bloomberg API)",
        "description": "Bloomberg API 港股與中國相關指數每日價格資料",
        "asset_class": "index",
        "market": "GLOBAL",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_index_api",
            "data_path": "data",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            },
        },
    },
    "crypto_bloomberg_eod": {
        "name": "加密貨幣日K — Bloomberg Direct",
        "description": "Bloomberg Direct 格式加密貨幣每日價格資料",
        "asset_class": "crypto",
        "market": "CRYPTO",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "bloomberg_crypto_direct"},
    },
    "fx_bloomberg_eod": {
        "name": "外匯日K — Bloomberg Direct",
        "description": "Bloomberg Direct 格式外匯每日價格資料",
        "asset_class": "fx",
        "market": "FX",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "bloomberg_fx_direct"},
    },
    "wtx_eod": {
        "name": "WTX 期貨日K",
        "description": "台灣加權指數期貨每日 OHLCV 資料（支援 FinLab 與 Bloomberg 來源）",
        "asset_class": "future",
        "market": "WTX",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "direct"},
    },
    "macro_bloomberg_observation": {
        "name": "總經觀測值 — Bloomberg Direct",
        "description": "Bloomberg Direct 格式宏觀經濟指標觀測值",
        "asset_class": "macro",
        "market": "MACRO",
        "frequency": "various",
        "is_active": True,
        "config": {"source_format": "bloomberg_macro_direct"},
    },
    "tw_equity_eod": {
        "name": "台股日K — FinLab Direct",
        "description": "FinLab Direct 格式台股每日 OHLCV 資料",
        "asset_class": "equity",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "finlab_twstock_direct",
            "data_path": "data",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "total_volume",
                "total_ticks": "total_ticks",
            },
        },
    },
    "tw_etf_eod": {
        "name": "台股 ETF 日K — FinLab Direct",
        "description": "FinLab Direct 格式台股 ETF 每日 OHLCV 資料",
        "asset_class": "etf",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "finlab_twstock_direct",
            "data_path": "data",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "total_volume",
                "total_ticks": "total_ticks",
            },
        },
    },
}


_TWSTOCK_ETF_ASSET_CLASSES = {"etf"}


def _resolve_twstock_dataset_key(payload: "TWStockDirectIngestPayload") -> str:
    """Decide whether a TW direct payload targets the equity or ETF dataset."""
    asset_class = (payload.metadata.asset_class or "").strip().lower()
    if asset_class in _TWSTOCK_ETF_ASSET_CLASSES:
        return "tw_etf_eod"
    return "tw_equity_eod"


def _normalize_source(value: str | None, default: str = "bloomberg") -> str:
    """Normalize source label for ingestion records."""
    source = (value or default).strip().lower()
    if source.startswith("bloomberg"):
        return "bloomberg"
    return source


def _build_direct_ingest_request(
    payload: DirectIngestPayload | TWStockDirectIngestPayload,
    dataset_key: str,
    key_prefix: str,
    default_source: str = "bloomberg",
    idempotency_key: str | None = None,
) -> IngestRequest:
    """Convert direct payload format to internal IngestRequest."""
    raw_payload = payload.model_dump(mode="json")
    metadata = raw_payload.get("metadata", {}) or {}

    source = _normalize_source(metadata.get("source"), default=default_source)
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
        idempotency_key=idempotency_key or request_key,
        payload=raw_payload,
        fetched_at=fetched_at,
    )


async def _ensure_direct_dataset_exists(db: AsyncSession, dataset_key: str) -> None:
    """Ensure built-in direct ingest datasets exist in the registry."""
    defaults = DIRECT_DATASET_DEFAULTS.get(dataset_key)
    if defaults is None:
        return

    existing = await db.get(DatasetRegistry, dataset_key)
    if existing is not None:
        return

    dataset = DatasetRegistry(
        dataset_key=dataset_key,
        name=defaults["name"],
        description=defaults["description"],
        asset_class=defaults["asset_class"],
        market=defaults["market"],
        frequency=defaults["frequency"],
        is_active=defaults["is_active"],
        config=copy.deepcopy(defaults["config"]),
    )

    try:
        async with db.begin_nested():
            db.add(dataset)
            await db.flush()
    except IntegrityError:
        pass


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
    except IdempotencyPayloadMismatchError as e:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except SourceIdentityMismatchError as e:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail=str(e),
        )
    except (OperationalError, DBAPIError):
        logger.exception("Database unavailable while accepting ingestion")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is temporarily unavailable",
            headers={"Retry-After": "30"},
        )
    except Exception:
        logger.exception(
            "Unexpected ingestion error (dataset_key=%s, expected_market=%s)",
            request.dataset_key,
            expected_market,
        )
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Ingestion failed due to internal server error",
        )


async def _ingest_direct_payload(
    payload: DirectIngestPayload | TWStockDirectIngestPayload,
    dataset_key: str,
    key_prefix: str,
    expected_market: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    default_source: str = "bloomberg",
    idempotency_key: str | None = None,
) -> IngestResponse:
    """使用推斷出的匯入欄位匯入直接格式市場資料。"""
    try:
        await _ensure_direct_dataset_exists(db, dataset_key)
    except (OperationalError, DBAPIError):
        logger.exception("Database unavailable while preparing direct ingestion")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is temporarily unavailable",
            headers={"Retry-After": "30"},
        )
    request = _build_direct_ingest_request(
        payload,
        dataset_key,
        key_prefix,
        default_source=default_source,
        idempotency_key=idempotency_key,
    )
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market=expected_market,
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


@router.post("/ingest/crypto", response_model=IngestResponse, status_code=202)
async def ingest_crypto_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 CRYPTO 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="CRYPTO",
    )


@router.post("/ingest/us", response_model=IngestResponse, status_code=202)
async def ingest_us_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 US 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="US",
    )


@router.post("/ingest/usstock/direct", response_model=IngestResponse, status_code=202)
async def ingest_usstock_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 Bloomberg 美股直接格式資料。"""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="us_stock_eod",
        key_prefix="direct_usstock",
        expected_market="US",
        background_tasks=background_tasks,
        db=db,
        idempotency_key=idempotency_key,
    )


def _validate_twstock_direct_payload(payload: TWStockDirectIngestPayload) -> None:
    """Apply TW stock direct business validation and surface 400 errors."""
    try:
        payload.validate_required_fields()
    except ValueError as exc:
        raise PayloadValidationError(str(exc)) from exc


@router.post("/ingest/hkchina/direct", response_model=IngestResponse, status_code=202)
async def ingest_hkchina_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 Bloomberg 港股／中國股票與指數直接格式資料。"""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="hkchina_mixed_eod",
        key_prefix="direct_hkchina",
        expected_market="GLOBAL",
        background_tasks=background_tasks,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/ingest/hkchina-index/direct", response_model=IngestResponse, status_code=202)
async def ingest_hkchina_index_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 Bloomberg 港股／中國指數直接格式資料。"""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="hkchina_index_eod",
        key_prefix="direct_hkchina_index",
        expected_market="GLOBAL",
        background_tasks=background_tasks,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/ingest/fx", response_model=IngestResponse, status_code=202)
async def ingest_fx_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 FX 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="FX",
    )


@router.post("/ingest/macro", response_model=IngestResponse, status_code=202)
async def ingest_macro_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 MACRO 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="MACRO",
    )


@router.post("/ingest/crypto/direct", response_model=IngestResponse, status_code=202)
async def ingest_crypto_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 Bloomberg 加密貨幣直接格式資料。"""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="crypto_bloomberg_eod",
        key_prefix="direct_crypto",
        expected_market="CRYPTO",
        background_tasks=background_tasks,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/ingest/fx/direct", response_model=IngestResponse, status_code=202)
async def ingest_fx_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 Bloomberg 外匯直接格式資料。"""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="fx_bloomberg_eod",
        key_prefix="direct_fx",
        expected_market="FX",
        background_tasks=background_tasks,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/ingest/macro/direct", response_model=IngestResponse, status_code=202)
async def ingest_macro_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 Bloomberg 總經直接格式資料。"""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="macro_bloomberg_observation",
        key_prefix="direct_macro",
        expected_market="MACRO",
        background_tasks=background_tasks,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/ingest/wtx/direct", response_model=IngestResponse, status_code=202)
async def ingest_wtx_direct_data(
    payload: DirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 WTX 期貨直接格式資料（支援 FinLab 與 Bloomberg 來源）。"""
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key="wtx_eod",
        key_prefix="direct_wtx",
        expected_market="WTX",
        background_tasks=background_tasks,
        db=db,
        idempotency_key=idempotency_key,
    )


@router.post("/ingest/twstock/direct", response_model=IngestResponse, status_code=202)
async def ingest_twstock_direct_data(
    payload: TWStockDirectIngestPayload,
    background_tasks: BackgroundTasks,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100),
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 FinLab 台股 / ETF 直接格式資料。依 metadata.asset_class 區分個股 (預設) 與 ETF。"""
    try:
        _validate_twstock_direct_payload(payload)
    except PayloadValidationError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    dataset_key = _resolve_twstock_dataset_key(payload)
    return await _ingest_direct_payload(
        payload=payload,
        dataset_key=dataset_key,
        key_prefix="direct_twstock",
        expected_market="TW",
        background_tasks=background_tasks,
        db=db,
        default_source="finlab",
        idempotency_key=idempotency_key,
    )


@router.post("/ingest/wtx", response_model=IngestResponse, status_code=202)
async def ingest_wtx_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 WTX 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="WTX",
    )


@router.post("/ingest/global", response_model=IngestResponse, status_code=202)
async def ingest_global_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 GLOBAL 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="GLOBAL",
    )


@router.post("/ingest/tw", response_model=IngestResponse, status_code=202)
async def ingest_tw_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 TW 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="TW",
    )


@router.post("/ingest/hk", response_model=IngestResponse, status_code=202)
async def ingest_hk_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 HK 市場資料。"""
    return await _ingest_by_market(
        request=request,
        background_tasks=background_tasks,
        db=db,
        expected_market="HK",
    )


@router.post("/ingest/cn", response_model=IngestResponse, status_code=202)
async def ingest_cn_data(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """匯入 CN 市場資料。"""
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
    """取得資料匯入執行狀態。"""
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
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """使用已儲存的原始資料重新執行正規化。"""
    service = IngestionService(db)

    try:
        new_run_id, run_status, _, _ = await service.rerun_from_raw(run_id)
        return IngestResponse(
            success=True,
            run_id=new_run_id,
            status=run_status,
            message=f"Rerun durably queued from raw payload {run_id}",
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
    except (OperationalError, DBAPIError):
        logger.exception("Database unavailable while accepting rerun")
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is temporarily unavailable",
            headers={"Retry-After": "30"},
        )
    except Exception:
        logger.exception("Unexpected rerun error (run_id=%s)", run_id)
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rerun failed due to internal server error",
        )


@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets(
    api_key: str = Depends(verify_source_api_key),
    db: AsyncSession = Depends(get_db),
):
    """列出可用資料集。"""
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
                schema_id=(ds.config or {}).get("schema_id"),
                accepted_schema_versions=(ds.config or {}).get("accepted_schema_versions", []),
                current_schema_version=(ds.config or {}).get("current_schema_version"),
                schema_enforcement=(ds.config or {}).get("schema_enforcement"),
            )
            for ds in datasets
        ],
    )
