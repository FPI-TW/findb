"""Source API v2.

All /direct endpoints:
- Validate payload via Pydantic (DirectIngestPayloadV2); schema errors → 422.
- Check RabbitMQ queue depth and node memory via management API; overload → 429.
- Dispatch a Celery task to `raw_data_ingest` queue (no DB write at request time).
- Downstream worker (process_ingestion_task) persists + normalizes.
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
import urllib.request
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status

from app.api.deps import verify_source_api_key
from app.config import get_settings
from app.schemas.source import DirectIngestPayloadV2, IngestQueueResponse
from app.utils import utc_now, uuid7
from app.workers.tasks import process_ingestion_task

logger = logging.getLogger(__name__)
router = APIRouter()

INGEST_QUEUE = "raw_data_ingest"

# ---------------------------------------------------------------------------
# RabbitMQ back-pressure guard
# ---------------------------------------------------------------------------

_PRESSURE_CACHE_TTL = 5.0  # seconds between management API polls
_pressure_lock = asyncio.Lock()
_pressure_pressured: bool = False
_pressure_expires_at: float = 0.0


def _derive_management_url(amqp_url: str) -> tuple[str, str]:
    """Return (base_url, Basic-Auth header) derived from the AMQP URL.

    amqp://user:pass@host:5672/ → ("http://host:15672", "Basic <b64>")
    """
    p = urlparse(amqp_url)
    user = p.username or "guest"
    password = p.password or "guest"
    host = p.hostname or "rabbitmq"
    base_url = f"http://{host}:15672"
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return base_url, f"Basic {token}"


def _http_get_json(url: str, auth: str) -> dict | list | None:
    """Blocking HTTP GET with Basic Auth. Returns parsed JSON or None on any error."""
    req = urllib.request.Request(url, headers={"Authorization": auth})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


async def _poll_rabbitmq_pressure() -> bool:
    """Return True if queue depth or node memory exceeds the configured threshold."""
    settings = get_settings()
    base_url, auth = _derive_management_url(settings.RABBITMQ_URL)
    threshold = settings.RABBITMQ_PRESSURE_THRESHOLD
    max_messages = settings.RABBITMQ_QUEUE_MAX_MESSAGES

    try:
        # 1. Queue depth
        qdata = await asyncio.to_thread(
            _http_get_json,
            f"{base_url}/api/queues/%2F/{INGEST_QUEUE}",
            auth,
        )
        if isinstance(qdata, dict):
            messages = qdata.get("messages", 0)
            limit = (qdata.get("arguments") or {}).get("x-max-length") or max_messages
            if limit and messages / limit >= threshold:
                logger.warning(
                    "RabbitMQ queue pressure: %d/%d (%.1f%%)",
                    messages,
                    limit,
                    messages / limit * 100,
                )
                return True

        # 2. Node memory
        ndata = await asyncio.to_thread(
            _http_get_json,
            f"{base_url}/api/nodes",
            auth,
        )
        if isinstance(ndata, list):
            for node in ndata:
                mem_used = node.get("mem_used", 0)
                mem_limit = node.get("mem_limit") or 1
                ratio = mem_used / mem_limit
                if ratio >= threshold:
                    logger.warning(
                        "RabbitMQ node memory pressure [%s]: %.1f%%",
                        node.get("name", "?"),
                        ratio * 100,
                    )
                    return True

    except Exception as exc:
        logger.warning("RabbitMQ pressure check failed, skipping: %s", exc)

    return False


async def check_rabbitmq_pressure() -> None:
    """Raise HTTP 429 if RabbitMQ queue depth or node memory is at ≥ 80%.

    Results are cached for 5 seconds to avoid hammering the management API.
    When the management API is unreachable the check is skipped (fail-open).
    """
    global _pressure_pressured, _pressure_expires_at

    now = time.monotonic()

    # Fast path: valid cache entry, no I/O needed
    if now < _pressure_expires_at:
        if _pressure_pressured:
            raise HTTPException(
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Message queue is at capacity, please retry later.",
                headers={"Retry-After": "30"},
            )
        return

    # Slow path: refresh under lock so only one coroutine polls at a time
    async with _pressure_lock:
        now = time.monotonic()
        if now < _pressure_expires_at:  # another coroutine already refreshed
            if _pressure_pressured:
                raise HTTPException(
                    status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Message queue is at capacity, please retry later.",
                    headers={"Retry-After": "30"},
                )
            return

        _pressure_pressured = await _poll_rabbitmq_pressure()
        _pressure_expires_at = time.monotonic() + _PRESSURE_CACHE_TTL

    if _pressure_pressured:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Message queue is at capacity, please retry later.",
            headers={"Retry-After": "30"},
        )


# ---------------------------------------------------------------------------
# Shared dispatch helper
# ---------------------------------------------------------------------------


def _normalize_source(value: str | None) -> str:
    source = (value or "bloomberg").strip().lower()
    if source.startswith("bloomberg"):
        return "bloomberg"
    return source


def _build_request_key(raw_payload: dict, key_prefix: str) -> str:
    digest = hashlib.sha256(
        json.dumps(raw_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"{key_prefix}_{digest}"


async def _dispatch_direct_payload(
    payload: DirectIngestPayloadV2,
    dataset_key: str,
    key_prefix: str,
    expected_market: str,
) -> IngestQueueResponse:
    await check_rabbitmq_pressure()

    message_id = uuid7()
    raw_payload = payload.model_dump(mode="json")
    metadata = raw_payload.get("metadata") or {}
    source = _normalize_source(metadata.get("source"))
    request_key = _build_request_key(raw_payload, key_prefix)

    envelope = {
        "message_id": str(message_id),
        "dataset_key": dataset_key,
        "expected_market": expected_market,
        "source": source,
        "request_key": request_key,
        "idempotency_key": request_key,
        "enqueued_at": utc_now().isoformat(),
        "payload": raw_payload,
    }

    try:
        await asyncio.to_thread(
            process_ingestion_task.apply_async,
            args=[envelope, expected_market],
            queue=INGEST_QUEUE,
            task_id=str(message_id),
        )
    except Exception:
        logger.exception(
            "Failed to dispatch Celery task queue=%s message_id=%s dataset=%s",
            INGEST_QUEUE,
            message_id,
            dataset_key,
        )
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Message broker unavailable",
        )

    item_count = len(raw_payload.get("data") or [])
    logger.info(
        "Dispatched Celery task: queue=%s message_id=%s dataset=%s items=%s",
        INGEST_QUEUE,
        message_id,
        dataset_key,
        item_count,
    )

    return IngestQueueResponse(message_id=message_id, request_key=request_key, status="queued")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/ingest/usstock/direct", response_model=IngestQueueResponse)
async def ingest_usstock_direct_data(
    payload: DirectIngestPayloadV2,
    api_key: str = Depends(verify_source_api_key),
) -> IngestQueueResponse:
    """Validate Bloomberg US stock direct payload and enqueue via Celery."""
    return await _dispatch_direct_payload(
        payload=payload,
        dataset_key="us_stock_eod",
        key_prefix="direct_usstock",
        expected_market="US",
    )


@router.post("/ingest/hkchina/direct", response_model=IngestQueueResponse)
async def ingest_hkchina_direct_data(
    payload: DirectIngestPayloadV2,
    api_key: str = Depends(verify_source_api_key),
) -> IngestQueueResponse:
    """Validate Bloomberg HK/China mixed direct payload and enqueue via Celery."""
    return await _dispatch_direct_payload(
        payload=payload,
        dataset_key="hkchina_mixed_eod",
        key_prefix="direct_hkchina",
        expected_market="GLOBAL",
    )


@router.post("/ingest/hkchina-index/direct", response_model=IngestQueueResponse)
async def ingest_hkchina_index_direct_data(
    payload: DirectIngestPayloadV2,
    api_key: str = Depends(verify_source_api_key),
) -> IngestQueueResponse:
    """Validate Bloomberg HK/China index direct payload and enqueue via Celery."""
    return await _dispatch_direct_payload(
        payload=payload,
        dataset_key="hkchina_index_eod",
        key_prefix="direct_hkchina_index",
        expected_market="GLOBAL",
    )


@router.post("/ingest/crypto/direct", response_model=IngestQueueResponse)
async def ingest_crypto_direct_data(
    payload: DirectIngestPayloadV2,
    api_key: str = Depends(verify_source_api_key),
) -> IngestQueueResponse:
    """Validate Bloomberg crypto direct payload and enqueue via Celery."""
    return await _dispatch_direct_payload(
        payload=payload,
        dataset_key="crypto_bloomberg_eod",
        key_prefix="direct_crypto",
        expected_market="CRYPTO",
    )


@router.post("/ingest/fx/direct", response_model=IngestQueueResponse)
async def ingest_fx_direct_data(
    payload: DirectIngestPayloadV2,
    api_key: str = Depends(verify_source_api_key),
) -> IngestQueueResponse:
    """Validate Bloomberg FX direct payload and enqueue via Celery."""
    return await _dispatch_direct_payload(
        payload=payload,
        dataset_key="fx_bloomberg_eod",
        key_prefix="direct_fx",
        expected_market="FX",
    )


@router.post("/ingest/macro/direct", response_model=IngestQueueResponse)
async def ingest_macro_direct_data(
    payload: DirectIngestPayloadV2,
    api_key: str = Depends(verify_source_api_key),
) -> IngestQueueResponse:
    """Validate Bloomberg macro direct payload and enqueue via Celery."""
    return await _dispatch_direct_payload(
        payload=payload,
        dataset_key="macro_bloomberg_observation",
        key_prefix="direct_macro",
        expected_market="MACRO",
    )


@router.post("/ingest/wtx/direct", response_model=IngestQueueResponse)
async def ingest_wtx_direct_data(
    payload: DirectIngestPayloadV2,
    api_key: str = Depends(verify_source_api_key),
) -> IngestQueueResponse:
    """Validate Bloomberg WTX futures direct payload and enqueue via Celery."""
    return await _dispatch_direct_payload(
        payload=payload,
        dataset_key="wtx_bloomberg_eod",
        key_prefix="direct_wtx",
        expected_market="WTX",
    )
