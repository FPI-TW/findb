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
from app.utils import utc_now

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
                    message_id=message_data.get("message_id"),
                    payload=payload_content,
                    fetched_at=metadata.get("query_time"),
                )
            except ValidationError as exc:
                logger.error("Envelope validation failed for IngestRequestV2: %s", exc)

                # Best-effort: create a failed run for audit trail.
                # request doesn't exist yet, so read raw fields from message_data directly.
                raw_dataset_key = message_data.get("dataset_key")
                raw_message_id = message_data.get("message_id")

                if raw_dataset_key and raw_message_id:
                    try:
                        await ensure_direct_dataset_exists(session, raw_dataset_key)
                        svc = IngestionService(session)
                        failed_run = await svc.create_ingestion_run_v2(
                            raw_dataset_key,
                            source=message_data.get("source")
                            or metadata.get("source")
                            or "unknown",
                            request_key=message_data.get("request_key"),
                            message_id=raw_message_id,
                            raw_records=0,
                            metadata={"raw_records": 0},
                            status="failed",
                        )
                        failed_run.error_message = f"Envelope validation error: {str(exc)[:500]}"
                        failed_run.completed_at = utc_now()
                        await session.commit()
                    except Exception as create_exc:
                        logger.error(
                            "Could not persist failed run for ValidationError "
                            "(dataset_key=%s message_id=%s): %s",
                            raw_dataset_key,
                            raw_message_id,
                            create_exc,
                        )
                else:
                    logger.error(
                        "Cannot create failed run: missing dataset_key or message_id in envelope "
                        "(dataset_key=%r message_id=%r)",
                        raw_dataset_key,
                        raw_message_id,
                    )
                raise

            # 2. Bootstrap dataset registry row if missing (worker owns all DB writes)
            await ensure_direct_dataset_exists(session, request.dataset_key)

            # 3. Run service logic
            service = IngestionService(session)
            try:
                await service.ingest_v2(request, expected_market=expected_market)
            except DatasetNotFoundError:
                # IngestionRun cannot be created: no DatasetRegistry row exists to satisfy
                # the FK constraint. Log with enough detail to trace by message_id.
                logger.error(
                    "DatasetNotFoundError: dataset_key=%s not in registry "
                    "(message_id=%s request_key=%s) — no IngestionRun created",
                    request.dataset_key,
                    request.message_id,
                    request.request_key,
                )
                raise
            except MarketMismatchError as exc:
                # Dataset exists but market tag doesn't match the endpoint.
                # Create a failed run so the rejection is auditable in the DB.
                run = await service.create_ingestion_run_v2(
                    request.dataset_key,
                    source=request.source,
                    request_key=request.request_key,
                    message_id=request.message_id,
                    raw_records=0,
                    metadata={
                        "source": request.source,
                        "request_key": request.request_key,
                        "raw_records": 0,
                    },
                    status="failed",
                )
                run.error_message = str(exc)
                run.completed_at = utc_now()
                await session.commit()
                raise

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
