import asyncio
import logging

from pydantic import ValidationError

from app.core.celery_app import celery_app
from app.models.base import async_session_maker
from app.schemas.source import IngestRequestV2
from app.services.ingestion import (
    DatasetInactiveError,
    DatasetNotFoundError,
    IngestionService,
    MarketMismatchError,
    PayloadValidationError,
    ensure_direct_dataset_exists,
)

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.workers.tasks.process_ingestion_task",
    max_retries=3,
    default_retry_delay=60,
    queue="raw_data_ingest",
    acks_late=True,
)
def process_ingestion_task(self, message_data: dict, expected_market: str):
    async def run_task():
        async with async_session_maker() as session:
            payload_content = message_data.get("payload", {})
            metadata = payload_content.get("metadata", {})

            # 1. Build Request Obj
            try:
                request = IngestRequestV2(
                    dataset_key=message_data.get("dataset_key"),
                    source=message_data.get("source") or metadata.get("source") or "unknown",
                    request_key=message_data.get("request_key"),
                    idempotency_key=message_data.get("message_id"),
                    message_id=message_data.get("message_id"),
                    payload=payload_content,
                    fetched_at=metadata.get("query_time"),
                )
            except ValidationError as e:
                logger.error(f"Payload structure invalid for IngestRequestV2: {e}")
                raise

            # 2. Bootstrap dataset registry row if missing (worker owns all DB writes)
            await ensure_direct_dataset_exists(session, request.dataset_key)

            # 3. Run service logic
            # ingest_v2 commits internally; no second commit needed here.
            service = IngestionService(session)
            await service.ingest_v2(request, expected_market=expected_market)

    try:
        asyncio.run(run_task())

    except (
        ValidationError,
        PayloadValidationError,
        DatasetNotFoundError,
        DatasetInactiveError,
        MarketMismatchError,
    ) as exc:
        # Non-retryable business logic errors.
        # DB state is already correct (ingest_v2 commits a failed run before raising
        # DatasetInactiveError / PayloadValidationError; other errors raise before any run
        # is created). Re-raise so Celery marks the task FAILURE with full exception info.
        logger.error(f"Ingestion rejected with non-retryable error: {exc}")
        raise

    except Exception as exc:
        # Transient failures (DB connectivity, broker issues) — retry up to max_retries.
        logger.error(f"Task encountered temporary failure, retrying... Error: {exc}")
        raise self.retry(exc=exc)
