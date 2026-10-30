"""Append-only evidence; the original plan, attempts and grants remain referenced."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7


class ProviderWorkCalendarClosure(Base):
    __tablename__ = "provider_work_calendar_closure"

    work_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_work.work_id", ondelete="RESTRICT"), primary_key=True
    )
    closure_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, default=uuid7)
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("daily_delivery_plan.plan_id", ondelete="RESTRICT")
    )
    request_id: Mapped[str] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    environment: Mapped[str] = mapped_column(String(20))
    execution_profile: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
