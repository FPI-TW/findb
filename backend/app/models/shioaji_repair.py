"""Append-only approval and installation evidence for frozen Shioaji members."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.utils import utc_now, uuid7


class ShioajiMemberRepair(Base):
    __tablename__ = "shioaji_member_repair"
    __table_args__ = (
        UniqueConstraint("work_id", "request_id", "member_key", name="uq_shioaji_repair_request"),
        CheckConstraint("observation_limit BETWEEN 1 AND 3", name="shioaji_repair_limit"),
    )
    repair_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)
    work_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_work.work_id", ondelete="RESTRICT"), index=True
    )
    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("daily_delivery_plan.plan_id", ondelete="RESTRICT"), index=True
    )
    environment: Mapped[str] = mapped_column(String(20))
    execution_profile: Mapped[str] = mapped_column(String(20))
    request_id: Mapped[str] = mapped_column(String(64))
    request_sha256: Mapped[str] = mapped_column(String(64))
    member_key: Mapped[str] = mapped_column(String(150))
    original_sha256: Mapped[str] = mapped_column(String(64))
    provider_symbol: Mapped[str | None] = mapped_column(String(100), nullable=True)
    observation_limit: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ShioajiRepairAck(Base):
    __tablename__ = "shioaji_repair_ack"
    repair_id: Mapped[UUID] = mapped_column(
        ForeignKey("shioaji_member_repair.repair_id", ondelete="RESTRICT"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(Integer)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
