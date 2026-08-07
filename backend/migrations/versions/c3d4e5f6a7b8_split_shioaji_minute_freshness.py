"""Split Shioaji minute availability and missing-delivery deadlines.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7

The minute policy originally used a 210-minute latest-date grace to delay
both ingest freshness and missing-delivery monitoring until 17:00 Taipei.
Ingest freshness is now due at 14:30 (60 minutes after the 13:30 close),
while the monitor keeps its separate, explicit 17:00 local deadline.

Only the known Shioaji minute policy shape is migrated.  Nested operator
configuration and unrelated registry keys remain untouched.  Downgrade is
deliberately conservative: it removes/reverts values only while the complete
post-upgrade shape is still present.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DATASET_KEYS = ("tw_equity_minute", "tw_etf_minute")


def _known_shape_predicate(*, grace: int, deadline: str | None) -> str:
    """Return guards for the policy fields written by f4/a1.

    The predicate intentionally does not compare the whole JSON document: an
    operator may keep unrelated top-level or nested annotations while the
    migration updates these specific canonical fields.
    """

    deadline_clause = (
        "AND NOT (config->'delivery_expectation'->'missing_delivery' ? 'deadline_local_time')"
        if deadline is None
        else "AND config->'delivery_expectation'->'missing_delivery'->>'deadline_local_time'"
        " = '17:00:00'"
    )
    return f"""
        config IS NOT NULL
        AND jsonb_typeof(config) = 'object'
        AND jsonb_typeof(config->'delivery_expectation') = 'object'
        AND config->'delivery_expectation'->>'delivery_mode' = 'sequenced_snapshot'
        AND jsonb_typeof(config->'delivery_expectation'->'latest_date') = 'object'
        AND config->'delivery_expectation'->'latest_date'->>'calendar_market' = 'TW'
        AND config->'delivery_expectation'->'latest_date'->>'timezone' = 'Asia/Taipei'
        AND config->'delivery_expectation'->'latest_date'->>'market_close_time' = '13:30:00'
        AND config->'delivery_expectation'->'latest_date'->>'availability_grace_minutes' = '{grace}'
        AND config->'delivery_expectation'->'latest_date'->>'action' = 'warn'
        AND jsonb_typeof(config->'delivery_expectation'->'missing_delivery') = 'object'
        AND config->'delivery_expectation'->'missing_delivery'->>'action' = 'warn'
        AND config->'delivery_expectation'->'missing_delivery'->'expected_sources'
              = '["shioaji"]'::jsonb
        {deadline_clause}
    """


def upgrade() -> None:
    """Set 14:30 ingest availability and an explicit 17:00 monitor cutoff."""

    predicate = _known_shape_predicate(grace=210, deadline=None)
    op.execute(
        sa.text(
            f"""
            UPDATE dataset_registry
            SET config = jsonb_set(
                    jsonb_set(
                        config,
                        '{{delivery_expectation,latest_date,availability_grace_minutes}}',
                        '60'::jsonb,
                        true
                    ),
                    '{{delivery_expectation,missing_delivery,deadline_local_time}}',
                    '"17:00:00"'::jsonb,
                    true
                ),
                updated_at = now()
            WHERE dataset_key IN :dataset_keys
              AND {predicate}
            """
        ).bindparams(sa.bindparam("dataset_keys", expanding=True, value=_DATASET_KEYS))
    )


def downgrade() -> None:
    """Revert only an untouched copy of this migration's exact values."""

    predicate = _known_shape_predicate(grace=60, deadline="17:00:00")
    op.execute(
        sa.text(
            f"""
            UPDATE dataset_registry
            SET config = jsonb_set(
                    jsonb_set(
                        config,
                        '{{delivery_expectation,latest_date,availability_grace_minutes}}',
                        '210'::jsonb,
                        true
                    ),
                    '{{delivery_expectation,missing_delivery}}',
                    (config->'delivery_expectation'->'missing_delivery')
                        - 'deadline_local_time',
                    true
                ),
                updated_at = now()
            WHERE dataset_key IN :dataset_keys
              AND {predicate}
            """
        ).bindparams(sa.bindparam("dataset_keys", expanding=True, value=_DATASET_KEYS))
    )
