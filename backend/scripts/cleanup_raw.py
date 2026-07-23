"""
Raw data cleanup script.
Deletes expired raw data records based on retention policy.
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.raw import RawMarketPayload
from app.models.registry import IngestionRun, NormalizationJob, NormalizationOutbox
from app.utils import utc_now

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


async def cleanup_expired_raw(
    *,
    database_url: str | None = None,
    retention_enabled: bool | None = None,
):
    """Delete expired raw data records."""
    enabled = settings.RAW_RETENTION_ENABLED if retention_enabled is None else retention_enabled
    if not enabled:
        logger.info("Raw data retention cleanup is disabled; skipping delete pass.")
        return 0

    engine = create_async_engine(database_url or settings.DATABASE_URL)
    async_session = async_sessionmaker(engine, class_=AsyncSession)

    async with async_session() as session:
        try:
            cutoff = utc_now() - timedelta(days=settings.RAW_RETENTION_DAYS)
            terminal_age = func.coalesce(IngestionRun.completed_at, IngestionRun.created_at)
            terminal_run = exists(
                select(IngestionRun.run_id).where(
                    IngestionRun.raw_payload_id == RawMarketPayload.raw_payload_id,
                    IngestionRun.status.in_(("completed", "completed_with_errors", "failed")),
                    terminal_age < cutoff,
                )
            )
            nonterminal_run = exists(
                select(IngestionRun.run_id).where(
                    IngestionRun.raw_payload_id == RawMarketPayload.raw_payload_id,
                    IngestionRun.status.not_in(("completed", "completed_with_errors", "failed")),
                )
            )
            nonterminal_job = exists(
                select(NormalizationJob.job_id)
                .join(IngestionRun, IngestionRun.run_id == NormalizationJob.run_id)
                .where(
                    IngestionRun.raw_payload_id == RawMarketPayload.raw_payload_id,
                    NormalizationJob.status.not_in(
                        ("completed", "completed_with_errors", "failed")
                    ),
                )
            )
            recently_terminal_run = exists(
                select(IngestionRun.run_id).where(
                    IngestionRun.raw_payload_id == RawMarketPayload.raw_payload_id,
                    IngestionRun.status.in_(("completed", "completed_with_errors", "failed")),
                    terminal_age >= cutoff,
                )
            )
            unpublished_delivery = exists(
                select(NormalizationOutbox.outbox_id)
                .join(IngestionRun, IngestionRun.run_id == NormalizationOutbox.run_id)
                .where(
                    IngestionRun.raw_payload_id == RawMarketPayload.raw_payload_id,
                    NormalizationOutbox.status != "published",
                )
            )
            stmt = delete(RawMarketPayload).where(
                RawMarketPayload.expire_at < utc_now(),
                terminal_run,
                ~nonterminal_run,
                ~nonterminal_job,
                ~recently_terminal_run,
                ~unpublished_delivery,
            )
            result = await session.execute(stmt)
            deleted_count = result.rowcount

            await session.commit()

            logger.info(f"Cleaned up {deleted_count} expired raw records")
            return deleted_count

        except Exception as e:
            logger.error(f"Cleanup failed: {e}")
            await session.rollback()
            raise
        finally:
            await engine.dispose()


async def main():
    """Main entry point."""
    logger.info("Starting raw data cleanup...")
    deleted = await cleanup_expired_raw()
    logger.info(f"Cleanup completed. Deleted {deleted} records.")


if __name__ == "__main__":
    asyncio.run(main())
