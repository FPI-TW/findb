"""
Bulk rerun normalization for existing ingestion runs.

Usage (in Docker container):
  # Rerun all completed runs
  python /app/scripts/bulk_rerun.py

  # Rerun only a specific dataset
  python /app/scripts/bulk_rerun.py --dataset crypto_bloomberg_eod

  # Rerun specific statuses (e.g. also include failed)
  python /app/scripts/bulk_rerun.py --status completed failed

  # Dry-run: show what would be rerun without actually doing it
  python /app/scripts/bulk_rerun.py --dry-run
"""

import argparse
import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, "/app")

from app.config import get_settings
from app.models.registry import IngestionRun
from app.services.ingestion import IngestionService, trigger_normalization

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()


async def bulk_rerun(
    dataset_key: str | None,
    statuses: list[str],
    dry_run: bool,
) -> None:
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        stmt = select(IngestionRun.run_id, IngestionRun.dataset_key, IngestionRun.status).where(
            IngestionRun.status.in_(statuses)
        )
        if dataset_key:
            stmt = stmt.where(IngestionRun.dataset_key == dataset_key)
        stmt = stmt.order_by(IngestionRun.created_at)

        result = await session.execute(stmt)
        runs = result.all()

    logger.info(
        "Found %d run(s) matching: dataset=%s status=%s",
        len(runs),
        dataset_key or "(all)",
        statuses,
    )

    if dry_run:
        for run_id, dk, status in runs:
            logger.info("  [DRY-RUN] would rerun: run_id=%s dataset=%s status=%s", run_id, dk, status)
        logger.info("Dry-run complete, no changes made.")
        await engine.dispose()
        return

    ok = 0
    failed = 0
    for run_id, dk, status in runs:
        try:
            async with session_factory() as session:
                service = IngestionService(session)
                new_run_id, new_status, new_dk, payload = await service.rerun_from_raw(run_id)

            async with session_factory() as session:
                await trigger_normalization(new_dk, payload, new_run_id, session_factory)

            logger.info(
                "Rerun OK: old_run=%s -> new_run=%s dataset=%s",
                run_id,
                new_run_id,
                new_dk,
            )
            ok += 1
        except Exception as e:
            logger.error("Rerun FAILED: run_id=%s error=%s", run_id, e)
            failed += 1

    logger.info("Done. ok=%d failed=%d total=%d", ok, failed, len(runs))
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk rerun normalization for existing ingestion runs.")
    parser.add_argument("--dataset", default=None, help="Filter by dataset_key (e.g. crypto_bloomberg_eod)")
    parser.add_argument(
        "--status",
        nargs="+",
        default=["completed"],
        help="Run statuses to include (default: completed). Options: pending running completed failed",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing")
    args = parser.parse_args()

    asyncio.run(bulk_rerun(args.dataset, args.status, args.dry_run))


if __name__ == "__main__":
    main()
