"""API key issuing and lookup helpers."""

from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import APIKey
from app.utils import utc_now, uuid7


def hash_api_key(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()


async def find_active_api_key(db: AsyncSession, api_key: str) -> APIKey | None:
    key_hash = hash_api_key(api_key)
    result = await db.execute(
        select(APIKey).where(APIKey.key_hash == key_hash, APIKey.revoked_at.is_(None))
    )
    row = result.scalar_one_or_none()
    if row is None or not compare_digest(row.key_hash, key_hash):
        return None
    return row


async def has_active_api_keys(db: AsyncSession) -> bool:
    result = await db.execute(select(APIKey.key_id).where(APIKey.revoked_at.is_(None)).limit(1))
    return result.scalar_one_or_none() is not None


async def create_api_key(
    db: AsyncSession,
    *,
    owner: str,
    tier: str,
    scopes: list[str],
    rate_limit_requests: int,
    rate_limit_window: int,
    page_size_limit: int,
) -> tuple[APIKey, str]:
    plaintext = f"findb_{token_urlsafe(32)}"
    row = APIKey(
        key_id=uuid7(),
        key_hash=hash_api_key(plaintext),
        owner=owner,
        tier=tier,
        scopes=scopes,
        rate_limit_requests=rate_limit_requests,
        rate_limit_window=rate_limit_window,
        page_size_limit=page_size_limit,
        usage_count=0,
        created_at=utc_now(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row, plaintext


async def list_api_keys(db: AsyncSession) -> list[APIKey]:
    result = await db.execute(select(APIKey).order_by(APIKey.created_at.desc()))
    return list(result.scalars().all())


async def revoke_api_key(db: AsyncSession, key_id: UUID) -> APIKey | None:
    row = await db.get(APIKey, key_id)
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = utc_now()
        await db.commit()
        await db.refresh(row)
    return row
