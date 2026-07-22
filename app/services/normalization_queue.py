"""Durable job, outbox dispatch, reconciliation, and worker orchestration."""

import logging
import os
import random
import socket
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
    NormalizationWorkerHeartbeat,
)
from app.services.ingestion import _select_normalizer_for_payload
from app.services.ingress_contracts import (
    parse_dataset_contract_declaration,
    validate_dataset_contract_scope,
)
from app.task_queue import celery_app
from app.utils import utc_now, uuid7

settings = get_settings()
logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {"completed", "completed_with_errors", "failed"}


class PermanentNormalizationError(RuntimeError):
    """A run-level error that retrying cannot repair."""

    def __init__(self, failure_code: str):
        super().__init__(failure_code)
        self.failure_code = failure_code


def _bounded_error(exc: BaseException, limit: int = 2_000) -> str:
    value = f"{type(exc).__name__}: {exc}".strip()
    return value if len(value) <= limit else f"{value[: limit - 3]}..."


async def reconcile_nonterminal_jobs(session: AsyncSession) -> int:
    """Requeue expired processing jobs and replay all non-terminal jobs."""
    now = utc_now()
    result = await session.execute(
        select(NormalizationJob)
        .where(
            NormalizationJob.status.not_in(TERMINAL_STATUSES),
            or_(
                NormalizationJob.status != "processing",
                NormalizationJob.lease_expires_at.is_(None),
                NormalizationJob.lease_expires_at < now,
            ),
        )
        .with_for_update(skip_locked=True)
    )
    jobs = list(result.scalars().all())
    created = 0
    for job in jobs:
        if job.status == "processing":
            job.status = "queued"
            job.lease_expires_at = None
        active_result = await session.execute(
            select(NormalizationOutbox).where(
                NormalizationOutbox.job_id == job.job_id,
                NormalizationOutbox.status.in_(("pending", "publishing")),
            )
        )
        active_deliveries = list(active_result.scalars().all())
        if active_deliveries:
            # Preserve the delivery generation that is already waiting to be
            # published. Rotating only the job id would make every active
            # outbox row stale and strand the run until lease reconciliation.
            for delivery in active_deliveries:
                delivery.delivery_id = job.delivery_id
                delivery.updated_at = now
            job.updated_at = now
            continue
        job.delivery_id = uuid7()
        session.add(
            NormalizationOutbox(
                outbox_id=uuid7(),
                job_id=job.job_id,
                run_id=job.run_id,
                delivery_id=job.delivery_id,
                event_type="normalize_run",
                status="pending",
                available_at=max(job.available_at, now),
                publish_attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
        job.updated_at = now
        created += 1
    await session.commit()
    return created


async def reconcile_stale_jobs(session: AsyncSession) -> int:
    """Repair expired execution leases and deliveries missing from the broker."""
    now = utc_now()
    stale_before = now - timedelta(seconds=settings.NORMALIZATION_LEASE_SECONDS)
    latest_published_at = (
        select(func.max(NormalizationOutbox.published_at))
        .where(NormalizationOutbox.job_id == NormalizationJob.job_id)
        .correlate(NormalizationJob)
        .scalar_subquery()
    )
    active_outbox = (
        select(NormalizationOutbox.outbox_id)
        .where(
            NormalizationOutbox.job_id == NormalizationJob.job_id,
            NormalizationOutbox.status.in_(("pending", "publishing")),
        )
        .exists()
    )
    result = await session.execute(
        select(NormalizationJob)
        .where(
            NormalizationJob.status.not_in(TERMINAL_STATUSES),
            ~active_outbox,
            or_(
                (
                    (NormalizationJob.status == "processing")
                    & (NormalizationJob.lease_expires_at < now)
                ),
                (
                    NormalizationJob.status.in_(("queued", "retrying"))
                    & (NormalizationJob.available_at <= now)
                    & or_(
                        latest_published_at.is_(None),
                        latest_published_at < stale_before,
                    )
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    )
    jobs = list(result.scalars().all())
    for job in jobs:
        delivery_id = uuid7()
        job.status = "queued"
        job.delivery_id = delivery_id
        job.available_at = now
        job.lease_expires_at = None
        job.updated_at = now
        session.add(
            NormalizationOutbox(
                outbox_id=uuid7(),
                job_id=job.job_id,
                run_id=job.run_id,
                delivery_id=delivery_id,
                event_type="normalize_run",
                status="pending",
                available_at=now,
                publish_attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
    await session.commit()
    return len(jobs)


async def claim_outbox_batch(session: AsyncSession) -> list[NormalizationOutbox]:
    now = utc_now()
    result = await session.execute(
        select(NormalizationOutbox)
        .where(
            NormalizationOutbox.available_at <= now,
            or_(
                NormalizationOutbox.status == "pending",
                (
                    (NormalizationOutbox.status == "publishing")
                    & (NormalizationOutbox.claim_until < now)
                ),
            ),
        )
        .order_by(NormalizationOutbox.available_at, NormalizationOutbox.created_at)
        .limit(settings.OUTBOX_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    rows = list(result.scalars().all())
    claim_until = now + timedelta(seconds=settings.OUTBOX_CLAIM_SECONDS)
    for row in rows:
        row.status = "publishing"
        row.claim_until = claim_until
        row.publish_attempts += 1
        row.updated_at = now
    await session.commit()
    return rows


def publish_outbox_event(row: NormalizationOutbox) -> None:
    """Publish a persistent, confirmed Celery task containing identifiers only."""
    celery_app.send_task(
        "app.workers.tasks.normalize_run",
        args=[str(row.run_id), str(row.delivery_id)],
        task_id=str(row.delivery_id),
        queue=settings.NORMALIZATION_QUEUE,
        routing_key="normalize",
        delivery_mode=2,
        mandatory=True,
    )


async def mark_outbox_published(session: AsyncSession, outbox_id: UUID) -> None:
    row = await session.get(NormalizationOutbox, outbox_id)
    if row is None:
        return
    now = utc_now()
    row.status = "published"
    row.published_at = now
    row.claim_until = None
    row.last_error = None
    row.updated_at = now
    await session.commit()


async def mark_outbox_publish_failure(
    session: AsyncSession, outbox_id: UUID, exc: BaseException
) -> None:
    row = await session.get(NormalizationOutbox, outbox_id)
    if row is None:
        return
    delay = min(2 ** min(row.publish_attempts, 6), 60)
    now = utc_now()
    row.status = "pending"
    row.available_at = now + timedelta(seconds=delay)
    row.claim_until = None
    row.last_error = _bounded_error(exc)
    row.updated_at = now
    await session.commit()


async def _heartbeat(session: AsyncSession, worker_id: str, run_id: UUID | None) -> None:
    stmt = insert(NormalizationWorkerHeartbeat).values(
        worker_id=worker_id,
        current_run_id=run_id,
        last_seen_at=utc_now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["worker_id"],
        set_={"current_run_id": run_id, "last_seen_at": utc_now()},
    )
    await session.execute(stmt)
    await session.commit()


async def _schedule_retry(
    session: AsyncSession,
    job_id: UUID,
    run_id: UUID,
    exc: BaseException,
) -> None:
    job = await session.get(NormalizationJob, job_id, with_for_update=True)
    run = await session.get(IngestionRun, run_id, with_for_update=True)
    if job is None or run is None:
        return
    now = utc_now()
    message = _bounded_error(exc)
    if job.attempt_count >= job.max_attempts:
        job.status = "failed"
        job.last_error = message
        job.lease_expires_at = None
        job.completed_at = now
        job.updated_at = now
        run.status = "failed"
        run.failure_code = "RETRY_EXHAUSTED"
        run.error_message = message
        run.completed_at = now
        run.next_retry_at = None
        await session.commit()
        return

    exponent = max(0, job.attempt_count - 1)
    base = min(
        settings.NORMALIZATION_RETRY_BASE_SECONDS * (2**exponent),
        settings.NORMALIZATION_RETRY_MAX_SECONDS,
    )
    delay = base + random.uniform(0, base * 0.25)
    available_at = now + timedelta(seconds=delay)
    delivery_id = uuid7()
    job.status = "retrying"
    job.delivery_id = delivery_id
    job.available_at = available_at
    job.lease_expires_at = None
    job.last_error = message
    job.updated_at = now
    run.status = "retrying"
    run.attempt_count = job.attempt_count
    run.next_retry_at = available_at
    run.error_message = message
    session.add(
        NormalizationOutbox(
            outbox_id=uuid7(),
            job_id=job.job_id,
            run_id=run.run_id,
            delivery_id=delivery_id,
            event_type="normalize_run",
            status="pending",
            available_at=available_at,
            publish_attempts=0,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()


async def _mark_permanent_failure(
    session: AsyncSession,
    job_id: UUID,
    run_id: UUID,
    exc: PermanentNormalizationError,
) -> None:
    job = await session.get(NormalizationJob, job_id, with_for_update=True)
    run = await session.get(IngestionRun, run_id, with_for_update=True)
    if job is None or run is None:
        return
    now = utc_now()
    message = _bounded_error(exc)
    job.status = "failed"
    job.last_error = message
    job.lease_expires_at = None
    job.completed_at = now
    job.updated_at = now
    run.status = "failed"
    run.failure_code = exc.failure_code
    run.error_message = message
    run.completed_at = now
    run.next_retry_at = None
    await session.commit()


async def execute_normalization(
    run_id: UUID,
    delivery_id: UUID,
    *,
    database_url: str | None = None,
) -> None:
    """Execute one idempotent normalization delivery in a process-local event loop."""
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    engine = create_async_engine(
        database_url or settings.DATABASE_URL,
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with engine.connect() as connection:
            session_factory = async_sessionmaker(
                bind=connection,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            async with session_factory() as session:
                await _heartbeat(session, worker_id, run_id)
                job_result = await session.execute(
                    select(NormalizationJob).where(NormalizationJob.run_id == run_id)
                )
                job = job_result.scalar_one_or_none()
                run = await session.get(IngestionRun, run_id)
                if job is None or run is None:
                    logger.warning("Ignoring normalization message for missing run %s", run_id)
                    await _heartbeat(session, worker_id, None)
                    return
                if run.status in TERMINAL_STATUSES or job.status in TERMINAL_STATUSES:
                    if job.status not in TERMINAL_STATUSES:
                        job.status = run.status
                        job.completed_at = run.completed_at or utc_now()
                        job.updated_at = utc_now()
                        await session.commit()
                    await _heartbeat(session, worker_id, None)
                    return
                if job.delivery_id != delivery_id:
                    logger.info("Ignoring stale delivery %s for run %s", delivery_id, run_id)
                    await _heartbeat(session, worker_id, None)
                    return
                job_id = job.job_id
                dataset_key = job.dataset_key

                lock_acquired = bool(
                    await session.scalar(
                        text("SELECT pg_try_advisory_lock(hashtext(:dataset_key))"),
                        {"dataset_key": dataset_key},
                    )
                )
                if not lock_acquired:
                    await session.refresh(job)
                    if job.status == "processing":
                        logger.info(
                            "Ignoring duplicate in-flight delivery %s for run %s",
                            delivery_id,
                            run_id,
                        )
                        await _heartbeat(session, worker_id, None)
                        return
                    now = utc_now()
                    job.status = "queued"
                    job.available_at = now + timedelta(seconds=random.uniform(5, 15))
                    job.updated_at = now
                    session.add(
                        NormalizationOutbox(
                            outbox_id=uuid7(),
                            job_id=job.job_id,
                            run_id=run_id,
                            delivery_id=delivery_id,
                            event_type="normalize_run",
                            status="pending",
                            available_at=job.available_at,
                            publish_attempts=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await session.commit()
                    await _heartbeat(session, worker_id, None)
                    return

                try:
                    if job.status != "processing":
                        job.attempt_count += 1
                    attempt_count = job.attempt_count
                    now = utc_now()
                    job.status = "processing"
                    job.lease_expires_at = now + timedelta(
                        seconds=settings.NORMALIZATION_LEASE_SECONDS
                    )
                    job.updated_at = now
                    run.status = "processing"
                    run.attempt_count = attempt_count
                    run.next_retry_at = None
                    if run.started_at is None:
                        run.started_at = now
                    await session.commit()

                    raw = None
                    if run.raw_payload_id:
                        raw = await session.get(RawMarketPayload, run.raw_payload_id)
                    if raw is None:
                        raw_result = await session.execute(
                            select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
                        )
                        raw = raw_result.scalar_one_or_none()
                    if raw is None:
                        raise PermanentNormalizationError("RAW_PAYLOAD_MISSING")
                    dataset = await session.get(DatasetRegistry, run.dataset_key)
                    if dataset is None:
                        raise PermanentNormalizationError("DATASET_NOT_FOUND")
                    if not dataset.is_active:
                        raise PermanentNormalizationError("DATASET_INACTIVE")
                    normalizer_cls = _select_normalizer_for_payload(
                        run.dataset_key,
                        raw.payload,
                        schema_id=raw.schema_id,
                        schema_version=raw.schema_version,
                    )
                    if normalizer_cls is None:
                        raise PermanentNormalizationError("NORMALIZER_NOT_CONFIGURED")

                    normalizer_config = dict(dataset.config or {})
                    if raw.schema_id is not None:
                        try:
                            declaration = parse_dataset_contract_declaration(dataset.config)
                            if declaration is None:
                                raise ValueError("missing contract declaration")
                            validate_dataset_contract_scope(
                                declaration,
                                market=dataset.market,
                                asset_class=dataset.asset_class,
                            )
                        except ValueError as exc:
                            logger.error(
                                "Invalid dataset contract configuration for %s: %s",
                                dataset.dataset_key,
                                exc,
                            )
                            raise PermanentNormalizationError("DATASET_CONTRACT_INVALID") from exc
                        normalizer_config["defaults"] = declaration.defaults.model_dump()
                    normalizer_config["_ingest_fetched_at"] = raw.fetched_at
                    normalizer_config["_ingest_source"] = raw.source
                    normalizer = normalizer_cls(session, normalizer_config)
                    result = await normalizer.process(raw.payload, run_id, commit=False)
                    now = utc_now()
                    job.status = (
                        "completed" if result.failed_records == 0 else "completed_with_errors"
                    )
                    job.lease_expires_at = None
                    job.last_error = result.error_message
                    job.completed_at = now
                    job.updated_at = now
                    run.attempt_count = attempt_count
                    run.failure_code = None
                    run.next_retry_at = None
                    await session.commit()
                except PermanentNormalizationError as exc:
                    await session.rollback()
                    logger.warning(
                        "Normalization permanently failed (run_id=%s, code=%s)",
                        run_id,
                        exc.failure_code,
                    )
                    await _mark_permanent_failure(session, job_id, run_id, exc)
                except Exception as exc:
                    await session.rollback()
                    logger.exception(
                        "Normalization attempt failed and will be retried (run_id=%s)",
                        run_id,
                    )
                    await _schedule_retry(session, job_id, run_id, exc)
                finally:
                    await session.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:dataset_key))"),
                        {"dataset_key": dataset_key},
                    )
                    await session.commit()
                    await _heartbeat(session, worker_id, None)
    finally:
        await engine.dispose()


async def queue_health(session: AsyncSession) -> dict:
    """Return DB-authoritative queue health for the Admin API."""
    now = utc_now()
    counts_result = await session.execute(
        select(NormalizationJob.status, func.count())
        .group_by(NormalizationJob.status)
        .order_by(NormalizationJob.status)
    )
    counts = {status: count for status, count in counts_result.all()}
    oldest_queued_at = await session.scalar(
        select(func.min(NormalizationJob.created_at)).where(
            NormalizationJob.status.in_(("queued", "retrying"))
        )
    )
    unpublished = await session.scalar(
        select(func.count())
        .select_from(NormalizationOutbox)
        .where(NormalizationOutbox.status != "published")
    )
    oldest_outbox_at = await session.scalar(
        select(func.min(NormalizationOutbox.created_at)).where(
            NormalizationOutbox.status != "published"
        )
    )
    heartbeat = await session.scalar(select(func.max(NormalizationWorkerHeartbeat.last_seen_at)))
    expired_leases = await session.scalar(
        select(func.count())
        .select_from(NormalizationJob)
        .where(
            NormalizationJob.status == "processing",
            NormalizationJob.lease_expires_at < utc_now(),
        )
    )
    retry_exhausted = await session.scalar(
        select(func.count())
        .select_from(IngestionRun)
        .where(IngestionRun.failure_code == "RETRY_EXHAUSTED")
    )
    return {
        "counts": counts,
        "oldest_queued_at": oldest_queued_at,
        "oldest_queued_age_seconds": (
            max(0.0, (now - oldest_queued_at).total_seconds()) if oldest_queued_at else None
        ),
        "unpublished_outbox": int(unpublished or 0),
        "oldest_unpublished_outbox_at": oldest_outbox_at,
        "oldest_unpublished_outbox_age_seconds": (
            max(0.0, (now - oldest_outbox_at).total_seconds()) if oldest_outbox_at else None
        ),
        "last_worker_heartbeat_at": heartbeat,
        "worker_heartbeat_age_seconds": (
            max(0.0, (now - heartbeat).total_seconds()) if heartbeat else None
        ),
        "expired_leases": int(expired_leases or 0),
        "retry_exhausted": int(retry_exhausted or 0),
    }
