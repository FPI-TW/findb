"""Shared transaction lock for one canonical feed identity."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def feed_scope_key(*, dataset_key: str, source: str, schema_id: str, schema_version: int) -> str:
    return ":".join(("canonical-feed", dataset_key, source, schema_id, str(schema_version)))


async def lock_feed_scope(
    db: AsyncSession,
    *,
    dataset_key: str,
    source: str,
    schema_id: str,
    schema_version: int,
    wait: bool,
) -> bool:
    """Acquire the transaction lock shared by ingress and delivery monitoring."""
    scope = feed_scope_key(
        dataset_key=dataset_key,
        source=source,
        schema_id=schema_id,
        schema_version=schema_version,
    )
    if wait:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": scope},
        )
        return True
    return bool(
        await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:scope, 0))"),
            {"scope": scope},
        )
    )
