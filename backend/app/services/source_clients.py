"""Source-client credential lifecycle helpers."""

from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import SourceClient
from app.utils import utc_now, uuid7


def hash_source_key(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()


async def find_active_source_client(db: AsyncSession, api_key: str) -> SourceClient | None:
    key_hash = hash_source_key(api_key)
    result = await db.execute(
        select(SourceClient).where(
            SourceClient.key_hash == key_hash,
            SourceClient.revoked_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def create_source_client(
    db: AsyncSession,
    *,
    name: str,
    source_name: str,
    allowed_datasets: list[str] | None,
    rate_limit_requests: int,
    rate_limit_window: int,
) -> tuple[SourceClient, str]:
    plaintext = f"findb_src_{token_urlsafe(32)}"
    row = SourceClient(
        client_id=uuid7(),
        name=name.strip(),
        source_name=source_name.strip().lower(),
        key_hash=hash_source_key(plaintext),
        allowed_datasets=allowed_datasets,
        rate_limit_requests=rate_limit_requests,
        rate_limit_window=rate_limit_window,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, plaintext


async def list_source_clients(db: AsyncSession) -> list[SourceClient]:
    result = await db.execute(select(SourceClient).order_by(SourceClient.created_at.desc()))
    return list(result.scalars().all())


async def revoke_source_client(db: AsyncSession, client_id: UUID) -> SourceClient | None:
    row = await db.get(SourceClient, client_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = utc_now()
        row.updated_at = utc_now()
        await db.commit()
        await db.refresh(row)
    return row
