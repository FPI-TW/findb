"""Unified presentation and lifecycle operations for DB-backed credentials."""

from datetime import timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.registry import APIKey, CredentialUsageRollup, SourceClient
from app.schemas.admin import CredentialResponse
from app.services.security_events import auth_failure_counts
from app.utils import utc_now


def credential_status(
    expires_at, revoked_at
) -> Literal["active", "expiring", "expired", "revoked"]:
    now = utc_now()
    if revoked_at is not None:
        return "revoked"
    if expires_at is not None and expires_at <= now:
        return "expired"
    if expires_at is not None and expires_at <= now + timedelta(days=30):
        return "expiring"
    return "active"


def present_source(row: SourceClient) -> CredentialResponse:
    return CredentialResponse(
        credential_ref=f"source:{row.client_id}",
        id=row.client_id,
        kind="source",
        name=row.name,
        owner=row.owner,
        description=row.description,
        status=credential_status(row.expires_at, row.revoked_at),
        fingerprint=row.fingerprint or row.key_hash[:16],
        scopes=row.allowed_datasets,
        policies={
            "source_name": row.source_name,
            "allowed_datasets": row.allowed_datasets,
            "rate_limit_requests": row.rate_limit_requests,
            "rate_limit_window": row.rate_limit_window,
        },
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        usage_count=row.usage_count,
        rotated_from_id=row.rotated_from_id,
    )


def present_api_key(row: APIKey) -> CredentialResponse:
    return CredentialResponse(
        credential_ref=f"{row.kind}:{row.key_id}",
        id=row.key_id,
        kind=row.kind,
        name=row.name or row.owner,
        owner=row.owner,
        description=row.description,
        status=credential_status(row.expires_at, row.revoked_at),
        fingerprint=row.fingerprint or row.key_hash[:16],
        role=row.role,
        scopes=row.scopes,
        policies={
            "tier": row.tier,
            "rate_limit_requests": row.rate_limit_requests,
            "rate_limit_window": row.rate_limit_window,
            "page_size_limit": row.page_size_limit,
        },
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        usage_count=row.usage_count,
        rotated_from_id=row.rotated_from_id,
    )


async def list_credentials(
    db: AsyncSession,
    *,
    kind: str | None = None,
    status: str | None = None,
    owner: str | None = None,
) -> list[CredentialResponse]:
    sources = list((await db.execute(select(SourceClient))).scalars().all())
    keys = list((await db.execute(select(APIKey))).scalars().all())
    rows = [present_source(row) for row in sources]
    rows.extend(present_api_key(row) for row in keys)
    if kind:
        rows = [row for row in rows if row.kind == kind]
    if status:
        rows = [row for row in rows if row.status == status]
    if owner:
        rows = [row for row in rows if row.owner == owner]
    return sorted(rows, key=lambda row: row.created_at or utc_now(), reverse=True)


async def credential_overview(db: AsyncSession) -> dict[str, Any]:
    rows = await list_credentials(db)
    usage_updated_at = (
        await db.execute(select(func.max(CredentialUsageRollup.updated_at)))
    ).scalar_one_or_none()
    settings = get_settings()
    counts = {value: 0 for value in ("active", "expiring", "expired", "revoked")}
    for row in rows:
        counts[row.status] += 1
    return {
        "auth_mode": "db_only",
        "serve_require_auth": settings.SERVE_REQUIRE_AUTH,
        "counts": counts,
        "auth_failure_counts": auth_failure_counts(),
        "usage_updated_at": usage_updated_at,
    }
