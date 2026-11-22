"""Keep the provider catalogue symbols a list lacks, and freeze them with each plan.

Revision ID: a4c6e8f0b2d3
Revises: f3a5c7e9b1d2

The list measures gaps (#629). A member-scoped provider serves only what it is
asked for, so a refresh also records its catalogue's in-scope symbols outside the
list in ``universe_supplement``; ``daily_delivery_plan.supplement_id`` freezes the
latest one observed by the trade date. Both are additive: no refresh recorded a
supplement before this revision, so existing plans keep null and a previous
release ignores them. Downgrade is refused once any supplement is recorded,
because dropping it would lose what plans requested.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "a4c6e8f0b2d3"
down_revision = "f3a5c7e9b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universe_supplement",
        sa.Column("supplement_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("member_sha256", sa.String(64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("members", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "dataset_key",
            "provider",
            "effective_date",
            "member_sha256",
            name="uq_universe_supplement_observation",
        ),
    )
    op.add_column(
        "daily_delivery_plan",
        sa.Column(
            "supplement_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("universe_supplement.supplement_id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM universe_supplement)")):
        raise RuntimeError("catalogue supplements are recorded; roll forward instead")
    op.drop_column("daily_delivery_plan", "supplement_id")
    op.drop_table("universe_supplement")
