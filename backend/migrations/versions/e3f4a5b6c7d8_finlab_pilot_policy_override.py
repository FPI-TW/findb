"""Add the bounded FinLab pilot delivery-policy override.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7

The normal full-market threshold remains 2,100 records.  This migration only
adds a source-scoped FinLab pilot override when no FinLab override exists and
does not replace operator-owned policy JSON.
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FINLAB_OVERRIDE_JSON = json.dumps(
    {
        "baseline": {"enabled": False},
        "record_count": {"minimum_record_count": 2},
    },
    separators=(",", ":"),
)


def upgrade() -> None:
    """Insert the default only when the FinLab source key is absent."""
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = jsonb_set(
                config,
                '{delivery_expectation,source_overrides}',
                CASE
                  WHEN jsonb_typeof(config->'delivery_expectation'->'source_overrides') = 'object'
                    THEN config->'delivery_expectation'->'source_overrides'
                      || jsonb_build_object('finlab', CAST(:finlab_override AS jsonb))
                  ELSE jsonb_build_object('finlab', CAST(:finlab_override AS jsonb))
                END,
                true
            )
            WHERE dataset_key = 'tw_equity_eod'
              AND jsonb_typeof(config) = 'object'
              AND jsonb_typeof(config->'delivery_expectation') = 'object'
              AND (
                NOT (config->'delivery_expectation' ? 'source_overrides')
                OR (
                  jsonb_typeof(config->'delivery_expectation'->'source_overrides') = 'object'
                  AND NOT (config->'delivery_expectation'->'source_overrides' ? 'finlab')
                )
              )
            """
        ).bindparams(finlab_override=_FINLAB_OVERRIDE_JSON)
    )


def downgrade() -> None:
    """Remove only an untouched copy of this migration's exact default."""
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = CASE
                WHEN (config->'delivery_expectation'->'source_overrides') - 'finlab' = '{}'::jsonb
                  THEN config #- '{delivery_expectation,source_overrides}'
                ELSE jsonb_set(
                    config,
                    '{delivery_expectation,source_overrides}',
                    (config->'delivery_expectation'->'source_overrides') - 'finlab',
                    true
                )
            END
            WHERE dataset_key = 'tw_equity_eod'
              AND config->'delivery_expectation'->'source_overrides'->'finlab'
                    = CAST(:finlab_override AS jsonb)
            """
        ).bindparams(finlab_override=_FINLAB_OVERRIDE_JSON)
    )
