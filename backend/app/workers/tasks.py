"""Celery task wrappers."""

import asyncio
from uuid import UUID

from celery.exceptions import Reject

from app.config import get_settings
from app.services.normalization_queue import execute_normalization
from app.task_queue import celery_app

settings = get_settings()


@celery_app.task(
    bind=True,
    name="app.workers.tasks.normalize_run",
    acks_late=True,
    reject_on_worker_lost=True,
)
def normalize_run(self, run_id: str, delivery_id: str) -> None:
    """Run async normalization in a fresh, process-local event loop."""
    try:
        parsed_run_id = UUID(run_id)
        parsed_delivery_id = UUID(delivery_id)
    except (ValueError, TypeError) as exc:
        raise Reject(str(exc), requeue=False) from exc

    try:
        asyncio.run(execute_normalization(parsed_run_id, parsed_delivery_id))
    except Exception as exc:
        # A DB outage can prevent the worker from persisting its normal retry
        # decision. Use Celery's delayed, bounded retry instead of an immediate
        # broker requeue loop that can monopolize every worker slot.
        retries = int(self.request.retries or 0)
        if retries >= settings.NORMALIZATION_MAX_ATTEMPTS:
            raise Reject(str(exc), requeue=False) from exc
        countdown = min(
            settings.NORMALIZATION_RETRY_BASE_SECONDS * (2**retries),
            settings.NORMALIZATION_RETRY_MAX_SECONDS,
        )
        raise self.retry(
            exc=exc,
            countdown=countdown,
            max_retries=settings.NORMALIZATION_MAX_ATTEMPTS,
        ) from exc
