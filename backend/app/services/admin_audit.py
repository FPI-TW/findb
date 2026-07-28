"""Durable actor attribution for Admin identity and credential mutations."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminPrincipal
from app.models.registry import AdminAuditEvent
from app.utils import utc_now, uuid7


async def record_admin_audit(
    db: AsyncSession,
    principal: AdminPrincipal,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict | None = None,
    commit: bool = True,
) -> AdminAuditEvent:
    row = AdminAuditEvent(
        event_id=uuid7(),
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_display_name=principal.display_name,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        created_at=utc_now(),
    )
    db.add(row)
    if commit:
        await db.commit()
        await db.refresh(row)
    else:
        await db.flush()
    return row
