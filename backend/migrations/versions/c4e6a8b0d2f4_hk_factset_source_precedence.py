"""Give FactSet Hong Kong precedence without the retired provider cutover.

Revision ID: c4e6a8b0d2f4
Revises: b3d5f7a9c1e2

The HK provider cutover used to be the only writer of the FactSet-first
``source_precedence``. With the cutover gate retired, an environment that never
applied it still carries the Twelve Data-only list, which ranks FactSet last.
Only that exact value is replaced, with the value the cutover wrote; any other
shape is operator policy and stays unchanged. Prices and lineage are untouched.
Downgrade restores the list only where no cutover decision was ever recorded.
"""

import sqlalchemy as sa
from alembic import op

revision = "c4e6a8b0d2f4"
down_revision = "b3d5f7a9c1e2"
branch_labels = None
depends_on = None

_OLD = '["twelve_data"]'
_NEW = '{"factset": -1, "twelve_data": 0}'


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE dataset_registry SET config = jsonb_set(config, '{source_precedence}', "
            "CAST(:new AS jsonb), false), updated_at = now() "
            "WHERE dataset_key = 'hk_equity_eod' "
            "AND config->'source_precedence' = CAST(:old AS jsonb)"
        ).bindparams(old=_OLD, new=_NEW)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE dataset_registry SET config = jsonb_set(config, '{source_precedence}', "
            "CAST(:old AS jsonb), false), updated_at = now() "
            "WHERE dataset_key = 'hk_equity_eod' "
            "AND config->'source_precedence' = CAST(:new AS jsonb) "
            "AND config->'hk_provider_cutover' IS NULL"
        ).bindparams(old=_OLD, new=_NEW)
    )
