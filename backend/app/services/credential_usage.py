"""Process-local credential usage aggregation with periodic durable flushes."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from uuid import UUID

from sqlalchemy import case, func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import APIKey, CredentialUsageRollup, SourceClient
from app.utils import utc_now, uuid7

logger = logging.getLogger(__name__)


@dataclass
class UsageEvent:
    count: int
    last_used_at: datetime
    endpoint: str
    status: int


_lock = Lock()
_pending: dict[tuple[str, UUID], UsageEvent] = {}


def reset_usage_state() -> None:
    """Clear process-local state (used when isolating tests)."""
    with _lock:
        _pending.clear()


def record_usage(kind: str, credential_id: UUID, endpoint: str, status_code: int) -> None:
    """Record only a resolved credential reference; plaintext keys are never retained."""
    now = utc_now()
    key = (kind, credential_id)
    with _lock:
        existing = _pending.get(key)
        _pending[key] = UsageEvent(
            count=(existing.count if existing else 0) + 1,
            last_used_at=now,
            endpoint=endpoint[:255],
            status=status_code,
        )


async def flush_usage(db: AsyncSession) -> int:
    with _lock:
        batch = dict(_pending)
        _pending.clear()
    if not batch:
        return 0
    try:
        for (kind, credential_id), event in batch.items():
            stmt = insert(CredentialUsageRollup).values(
                usage_id=uuid7(),
                credential_kind=kind,
                credential_id=credential_id,
                request_count=event.count,
                last_used_at=event.last_used_at,
                last_endpoint=event.endpoint,
                last_status=event.status,
                updated_at=utc_now(),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_credential_usage_ref",
                set_={
                    "request_count": CredentialUsageRollup.request_count + event.count,
                    "last_used_at": func.greatest(
                        func.coalesce(CredentialUsageRollup.last_used_at, event.last_used_at),
                        event.last_used_at,
                    ),
                    "last_endpoint": case(
                        (
                            func.coalesce(CredentialUsageRollup.last_used_at, event.last_used_at)
                            <= event.last_used_at,
                            event.endpoint,
                        ),
                        else_=CredentialUsageRollup.last_endpoint,
                    ),
                    "last_status": case(
                        (
                            func.coalesce(CredentialUsageRollup.last_used_at, event.last_used_at)
                            <= event.last_used_at,
                            event.status,
                        ),
                        else_=CredentialUsageRollup.last_status,
                    ),
                    "updated_at": utc_now(),
                },
            )
            await db.execute(stmt)
            model = SourceClient if kind == "source" else APIKey
            id_column = SourceClient.client_id if kind == "source" else APIKey.key_id
            await db.execute(
                update(model)
                .where(id_column == credential_id)
                .values(
                    usage_count=model.usage_count + event.count,
                    last_used_at=func.greatest(
                        func.coalesce(model.last_used_at, event.last_used_at),
                        event.last_used_at,
                    ),
                )
            )
        await db.commit()
        return len(batch)
    except Exception:
        await db.rollback()
        with _lock:
            for key, event in batch.items():
                existing = _pending.get(key)
                if existing and existing.last_used_at > event.last_used_at:
                    existing.count += event.count
                    _pending[key] = existing
                else:
                    event.count += existing.count if existing else 0
                    _pending[key] = event
        raise


async def periodic_flush(session_factory, interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with session_factory() as db:
                await flush_usage(db)
        except Exception:
            logger.exception("Credential usage flush failed; events retained for retry")
