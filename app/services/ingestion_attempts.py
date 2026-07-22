"""Durable audit lifecycle for canonical ingestion attempts."""

import hashlib
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.registry import IngestionAttempt
from app.utils import utc_now, uuid7

_ERROR_MESSAGE_LIMIT = 1_000
_STALE_ATTEMPT_MESSAGE = "Request processing did not reach a terminal state"


class IngestionAttemptClaimError(RuntimeError):
    """Raised after durable persistence when the live row claim cannot be acquired."""

    def __init__(self, attempt_id: UUID):
        super().__init__(f"Failed to claim persisted ingestion attempt {attempt_id}")
        self.attempt_id = attempt_id


def _bounded_string(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized[:limit]


def _schema_version(value: Any) -> int | None:
    if type(value) is not int:
        return None
    if value < -(2**31) or value > 2**31 - 1:
        return None
    return value


def _request_fields(body: Any) -> dict[str, Any]:
    if not isinstance(body, Mapping):
        return {}
    return {
        "dataset_key": _bounded_string(body.get("dataset_key"), 50),
        "source": _bounded_string(body.get("source"), 50),
        "schema_id": _bounded_string(body.get("schema_id"), 50),
        "schema_version": _schema_version(body.get("schema_version")),
        "request_key": _bounded_string(body.get("request_key"), 100),
        "idempotency_key": _bounded_string(body.get("idempotency_key"), 100),
    }


class IngestionAttemptService:
    """Persist attempt state independently from raw/run/job transactions."""

    def __init__(self, db: AsyncSession):
        self.db = db
        session_info = db.info if isinstance(db.info, dict) else {}
        self.ownership_enforced = "source_client_id" in session_info
        self.source_client_id: UUID | None = session_info.get("source_client_id")

    async def begin(self, body: Any, raw_body: bytes) -> IngestionAttempt:
        """Persist ``received``, then claim its row until the request is terminal."""
        now = utc_now()
        attempt = IngestionAttempt(
            attempt_id=uuid7(),
            source_client_id=self.source_client_id,
            request_sha256=hashlib.sha256(raw_body).hexdigest(),
            status="received",
            created_at=now,
            updated_at=now,
            **_request_fields(body),
        )
        persisted_attempt_id = attempt.attempt_id
        self.db.add(attempt)
        await self.db.commit()
        try:
            claimed = await self.db.scalar(
                select(IngestionAttempt)
                .where(IngestionAttempt.attempt_id == persisted_attempt_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        except Exception as exc:
            try:
                await self.db.rollback()
            except Exception:
                pass
            raise IngestionAttemptClaimError(persisted_attempt_id) from exc
        if claimed is None:
            raise IngestionAttemptClaimError(persisted_attempt_id)
        return claimed

    async def reject(
        self,
        attempt_id: UUID,
        *,
        http_status: int,
        failure_code: str,
        error_message: str,
    ) -> IngestionAttempt:
        """Mark an attempt rejected and commit the terminal audit state."""
        attempt = await self.db.scalar(
            select(IngestionAttempt)
            .where(IngestionAttempt.attempt_id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if attempt is None:
            raise RuntimeError(f"Ingestion attempt {attempt_id} not found")
        if attempt.status != "received":
            return attempt
        now = utc_now()
        attempt.status = "rejected"
        attempt.http_status = http_status
        attempt.failure_code = failure_code[:50]
        attempt.error_message = error_message[:_ERROR_MESSAGE_LIMIT]
        attempt.completed_at = now
        attempt.updated_at = now
        await self.db.commit()
        return attempt

    async def mark_accepted(
        self,
        attempt_id: UUID,
        run_id: UUID,
        *,
        duplicate: bool = False,
    ) -> IngestionAttempt:
        """Stage terminal success; the caller owns the surrounding commit."""
        attempt = await self.db.scalar(
            select(IngestionAttempt)
            .where(IngestionAttempt.attempt_id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if attempt is None:
            raise RuntimeError(f"Ingestion attempt {attempt_id} not found")
        if attempt.status != "received":
            raise RuntimeError(
                f"Ingestion attempt {attempt_id} is already terminal ({attempt.status})"
            )
        now = utc_now()
        attempt.status = "duplicate" if duplicate else "accepted"
        attempt.http_status = 202
        attempt.failure_code = None
        attempt.error_message = None
        attempt.run_id = run_id
        attempt.completed_at = now
        attempt.updated_at = now
        return attempt

    async def get(self, attempt_id: UUID) -> Optional[IngestionAttempt]:
        """Return an attempt within the authenticated source-client scope."""
        attempt = await self.db.get(IngestionAttempt, attempt_id)
        if attempt is None:
            return None
        if self.ownership_enforced and attempt.source_client_id != self.source_client_id:
            return None
        return attempt


async def reconcile_stale_ingestion_attempts(
    db: AsyncSession,
    *,
    stale_seconds: int | None = None,
    batch_size: int | None = None,
) -> int:
    """Batch-terminalize orphaned attempts, skipping rows claimed by live requests."""
    settings = get_settings()
    threshold_seconds = (
        settings.INGESTION_ATTEMPT_STALE_SECONDS if stale_seconds is None else stale_seconds
    )
    reconcile_batch_size = (
        settings.INGESTION_ATTEMPT_RECONCILE_BATCH_SIZE if batch_size is None else batch_size
    )
    if threshold_seconds < 1:
        raise ValueError("stale_seconds must be positive")
    if reconcile_batch_size < 1:
        raise ValueError("batch_size must be positive")
    now = utc_now()
    result = await db.execute(
        select(IngestionAttempt)
        .where(
            IngestionAttempt.status == "received",
            IngestionAttempt.created_at < now - timedelta(seconds=threshold_seconds),
        )
        .order_by(IngestionAttempt.created_at)
        .limit(reconcile_batch_size)
        .with_for_update(skip_locked=True)
    )
    attempts = list(result.scalars().all())
    for attempt in attempts:
        attempt.status = "aborted"
        attempt.http_status = None
        attempt.failure_code = "ATTEMPT_INTERRUPTED"
        attempt.error_message = _STALE_ATTEMPT_MESSAGE
        attempt.completed_at = now
        attempt.updated_at = now
    await db.commit()
    return len(attempts)
