"""
Raw data cleanup script.
Deletes expired raw data records based on retention policy.
"""

import asyncio
import logging
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.raw import RawMarketPayload
from app.utils import utc_now

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


async def cleanup_expired_raw():
    """Delete expired raw data records."""
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = async_sessionmaker(engine, class_=AsyncSession)

    async with async_session() as session:
        try:
            # Delete expired records
            stmt = delete(RawMarketPayload).where(
                RawMarketPayload.expire_at < utc_now()
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
