"""Celery task wrappers."""

import asyncio
from uuid import UUID

from celery.exceptions import Reject

from app.services.normalization_queue import execute_normalization
from app.task_queue import celery_app


@celery_app.task(
    bind=True,
    name="app.workers.tasks.normalize_run",
    acks_late=True,
    reject_on_worker_lost=True,
)
def normalize_run(self, run_id: str, delivery_id: str) -> None:
    """Run async normalization in a fresh, process-local event loop."""
    try:
        asyncio.run(execute_normalization(UUID(run_id), UUID(delivery_id)))
    except (ValueError, TypeError) as exc:
        raise Reject(str(exc), requeue=False) from exc
    except Exception as exc:
        # If the worker cannot persist a retry decision, keep broker ownership
        # instead of acknowledging and losing the delivery.
        raise Reject(str(exc), requeue=True) from exc
