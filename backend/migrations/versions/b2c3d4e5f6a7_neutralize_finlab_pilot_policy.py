"""Neutralize the historical environment-agnostic FinLab pilot override.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6

The old FinLab migration applied a two-row source override to every
environment.  Target-specific policy now belongs to
``scripts/provision_registry.py``.  This migration removes only the exact JSON
value written by that historical migration; non-exact operator-owned values
remain untouched.
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FINLAB_PILOT_OVERRIDE = {
    "baseline": {"enabled": False},
    "record_count": {"minimum_record_count": 2},
}
_FINLAB_PILOT_OVERRIDE_JSON = json.dumps(_FINLAB_PILOT_OVERRIDE, separators=(",", ":"))


def upgrade() -> None:
    """Remove only the exact historical pilot value from TW equity EOD."""
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = CASE
                WHEN (config->'delivery_expectation'->'source_overrides') - 'finlab'
                        = '{}'::jsonb
                  THEN config #- '{delivery_expectation,source_overrides}'
                ELSE jsonb_set(
                    config,
                    '{delivery_expectation,source_overrides}',
                    (config->'delivery_expectation'->'source_overrides') - 'finlab',
                    true
                )
            END,
            updated_at = now()
            WHERE dataset_key = 'tw_equity_eod'
              AND jsonb_typeof(config) = 'object'
              AND jsonb_typeof(config->'delivery_expectation') = 'object'
              AND jsonb_typeof(config->'delivery_expectation'->'source_overrides') = 'object'
              AND config->'delivery_expectation'->'source_overrides'->'finlab'
                    = CAST(:finlab_override AS jsonb)
            """
        ).bindparams(finlab_override=_FINLAB_PILOT_OVERRIDE_JSON)
    )


def downgrade() -> None:
    """Restore the historical value only when no FinLab override exists."""
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
            ),
            updated_at = now()
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
        ).bindparams(finlab_override=_FINLAB_PILOT_OVERRIDE_JSON)
    )
