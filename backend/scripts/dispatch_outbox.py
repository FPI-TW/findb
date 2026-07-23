"""Continuously publish durable normalization outbox rows to RabbitMQ."""

import asyncio
import logging
import time
from contextlib import suppress

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.delivery_monitor import scan_missing_deliveries
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


async def scan_missing_deliveries_safely(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Keep delivery-monitor failures isolated from durable outbox dispatch."""
    try:
        async with session_factory() as session:
            try:
                async with asyncio.timeout(settings.DELIVERY_MONITOR_TIMEOUT_SECONDS):
                    result = await scan_missing_deliveries(session)
            except BaseException:
                await asyncio.shield(session.rollback())
                raise
    except TimeoutError:
        logger.error(
            "Missing dataset delivery scan timed out after %s seconds",
            settings.DELIVERY_MONITOR_TIMEOUT_SECONDS,
        )
        return
    except Exception:
        logger.exception("Failed to scan missing dataset deliveries")
        return
    if result.skipped_locked:
        logger.info("Delivery monitor scan skipped because another replica owns the lock")
    elif result.created_or_refreshed or result.resolved or result.diagnostics:
        logger.info(
            "Delivery monitor refreshed=%s resolved=%s diagnostics=%s",
            result.created_or_refreshed,
            result.resolved,
            len(result.diagnostics),
        )


async def delivery_monitor_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run monitor scans independently and serially with one immediate startup scan."""
    while True:
        await scan_missing_deliveries_safely(session_factory)
        await asyncio.sleep(settings.DELIVERY_MONITOR_SECONDS)


async def run_dispatcher() -> None:
    engine = create_async_engine(settings.DATABASE_URL)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    monitor_task: asyncio.Task[None] | None = None
    try:
        await asyncio.to_thread(declare_topology)
        async with session_factory() as session:
            replayed = await reconcile_nonterminal_jobs(session)
            logger.info("Reconciled %s non-terminal normalization jobs", replayed)
        await reconcile_stale_attempts_safely(session_factory)
        monitor_task = asyncio.create_task(
            delivery_monitor_loop(session_factory),
            name="delivery-monitor",
        )

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
        if monitor_task is not None:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_dispatcher())
