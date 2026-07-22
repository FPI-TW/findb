"""Continuously publish durable normalization outbox rows to RabbitMQ."""

import asyncio
import logging
import time

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.ingestion_attempts import reconcile_stale_ingestion_attempts
from app.services.normalization_queue import (
    claim_outbox_batch,
    mark_outbox_publish_failure,
    mark_outbox_published,
    publish_outbox_event,
    reconcile_nonterminal_jobs,
    reconcile_stale_jobs,
)
from app.task_queue import declare_topology

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


async def reconcile_stale_attempts_safely(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Isolate attempt-audit maintenance failures from outbox dispatch."""
    try:
        async with session_factory() as session:
            try:
                aborted = await reconcile_stale_ingestion_attempts(session)
            except Exception:
                await session.rollback()
                raise
    except Exception:
        logger.exception("Failed to reconcile stale ingestion attempts")
        return 0
    if aborted:
        logger.warning("Marked %s stale ingestion attempts aborted", aborted)
    return aborted


async def run_dispatcher() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        await asyncio.to_thread(declare_topology)
        async with session_factory() as session:
            replayed = await reconcile_nonterminal_jobs(session)
            logger.info("Reconciled %s non-terminal normalization jobs", replayed)
        await reconcile_stale_attempts_safely(session_factory)

        broker_was_unavailable = False
        last_reconciliation = time.monotonic()
        while True:
            if time.monotonic() - last_reconciliation >= settings.OUTBOX_RECONCILE_SECONDS:
                try:
                    await asyncio.to_thread(declare_topology)
                except Exception:
                    broker_was_unavailable = True
                    logger.exception("RabbitMQ topology probe failed")
                else:
                    async with session_factory() as session:
                        if broker_was_unavailable:
                            replayed = await reconcile_nonterminal_jobs(session)
                            logger.warning(
                                "RabbitMQ recovered; replayed %s deliveries",
                                replayed,
                            )
                            broker_was_unavailable = False
                        else:
                            repaired = await reconcile_stale_jobs(session)
                            if repaired:
                                logger.warning(
                                    "Repaired %s stale normalization deliveries",
                                    repaired,
                                )
                await reconcile_stale_attempts_safely(session_factory)
                last_reconciliation = time.monotonic()

            async with session_factory() as session:
                rows = await claim_outbox_batch(session)
            if not rows:
                await asyncio.sleep(settings.OUTBOX_POLL_SECONDS)
                continue
            for row in rows:
                try:
                    await asyncio.to_thread(publish_outbox_event, row)
                except Exception as exc:
                    broker_was_unavailable = True
                    logger.exception("Outbox publish failed (outbox_id=%s)", row.outbox_id)
                    async with session_factory() as session:
                        await mark_outbox_publish_failure(session, row.outbox_id, exc)
                else:
                    async with session_factory() as session:
                        await mark_outbox_published(session, row.outbox_id)
                    if broker_was_unavailable:
                        async with session_factory() as session:
                            replayed = await reconcile_nonterminal_jobs(session)
                        logger.warning(
                            "RabbitMQ recovered during publish; replayed %s deliveries",
                            replayed,
                        )
                        broker_was_unavailable = False
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_dispatcher())
