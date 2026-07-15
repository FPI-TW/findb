"""phase_5_etf_bond_models

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-09 16:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "etf_details",
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tracking_index", sa.String(length=100), nullable=True),
        sa.Column("expense_ratio", sa.NUMERIC(10, 6), nullable=True),
        sa.Column("issuer", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_etf_details_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instrument_id", name=op.f("pk_etf_details")),
    )
    op.create_table(
        "bond_details",
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.String(length=100), nullable=True),
        sa.Column("coupon", sa.NUMERIC(10, 6), nullable=True),
        sa.Column("maturity_date", sa.Date(), nullable=True),
        sa.Column("rating", sa.String(length=30), nullable=True),
        sa.Column("face_value", sa.NUMERIC(20, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_bond_details_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instrument_id", name=op.f("pk_bond_details")),
    )
    op.create_index("idx_bond_issuer", "bond_details", ["issuer"], unique=False)
    op.create_index("idx_bond_maturity", "bond_details", ["maturity_date"], unique=False)

    op.create_table(
        "bond_eod",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("yield_to_maturity", sa.NUMERIC(20, 8), nullable=True),
        sa.Column("clean_price", sa.NUMERIC(20, 8), nullable=True),
        sa.Column("dirty_price", sa.NUMERIC(20, 8), nullable=True),
        sa.Column("duration", sa.NUMERIC(20, 8), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("asof_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_bond_eod_instrument_id_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bond_eod")),
        sa.UniqueConstraint("instrument_id", "trade_date", name="uq_bond_eod"),
    )
    op.create_index("idx_bond_eod_date", "bond_eod", ["trade_date"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_bond_eod_date", table_name="bond_eod")
    op.drop_table("bond_eod")
    op.drop_index("idx_bond_maturity", table_name="bond_details")
    op.drop_index("idx_bond_issuer", table_name="bond_details")
    op.drop_table("bond_details")
    op.drop_table("etf_details")
