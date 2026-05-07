import asyncio
import logging

from pydantic import ValidationError

from app.core.celery_app import celery_app
from app.models.base import async_session_maker
from app.schemas.source import IngestRequest
from app.services.ingestion import (
    DatasetInactiveError,
    DatasetNotFoundError,
    IngestionService,
    MarketMismatchError,
    PayloadValidationError,
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
            # Note：fetched_at should be datetime or ISO string，IngestRequest will exame it
            try:
                request = IngestRequest(
                    dataset_key=message_data.get("dataset_key"),
                    source=message_data.get("source") or metadata.get("source") or "unknown",
                    request_key=message_data.get("request_key"),
                    idempotency_key=message_data.get("idempotency_key"),
                    payload=payload_content,
                    fetched_at=metadata.get("query_time"),
                )
            except ValidationError as e:
                logger.error(f"Payload structure invalid for IngestRequest: {e}")
                raise  # Don't re-try on validation issue

            # 2. Run service logic
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
        # No re-try on business logic errors
        logger.error(f"Ingestion rejected with non-retryable error: {exc}")
        # Fail the task

    except Exception as exc:
        # Re-try on temporary fails like connection issue
        logger.error(f"Task encountered temporary failure, retrying... Error: {exc}")
        raise self.retry(exc=exc)
