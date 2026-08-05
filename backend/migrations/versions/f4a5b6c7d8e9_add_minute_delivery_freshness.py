"""Persist minute sequence identity and seed Shioaji delivery expectations.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8

The minute feed is a policy-only ``sequenced_snapshot`` delivery mode.  The
EOD ingress contract enum remains intentionally closed; the mode is stored as
plain text on ``ingestion_run`` just like the existing EOD modes.
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_MINUTE_DELIVERY_EXPECTATION = {
    "delivery_mode": "sequenced_snapshot",
    "baseline": {
        "enabled": False,
        "strategy": "rolling_median",
        "scope": "dataset_source_schema",
        "window_size": 7,
        "minimum_history": 3,
    },
    "record_count": {
        "minimum_record_count": 0,
        "maximum_count_drop_ratio": 0.0,
        "action": "disabled",
    },
    "freshness": {
        "maximum_fetch_age_hours": 6,
        "allowed_clock_skew_minutes": 5,
        "action": "warn",
    },
    "latest_date": {
        "calendar_market": "TW",
        "timezone": "Asia/Taipei",
        "market_close_time": "13:30:00",
        "availability_grace_minutes": 210,
        "action": "warn",
    },
    "missing_delivery": {
        "action": "warn",
        "expected_sources": ["shioaji"],
    },
    "schedule": {
        "enabled": True,
        "slot_id": "tw_1430",
        "local_time": "14:30:00",
        "timezone": "Asia/Taipei",
        "expected_sources": ["shioaji"],
    },
}
_MINUTE_EXPECTATION_JSON = json.dumps(
    _MINUTE_DELIVERY_EXPECTATION,
    ensure_ascii=False,
    separators=(",", ":"),
)


def _add_sequence_columns() -> None:
    # ``IF NOT EXISTS`` keeps a partially applied local migration recoverable.
    op.execute("ALTER TABLE ingestion_run ADD COLUMN IF NOT EXISTS snapshot_id VARCHAR(100)")
    op.execute("ALTER TABLE ingestion_run ADD COLUMN IF NOT EXISTS daily_update_id VARCHAR(100)")
    op.execute("ALTER TABLE ingestion_run ADD COLUMN IF NOT EXISTS sequence INTEGER")
    op.execute("ALTER TABLE ingestion_run ADD COLUMN IF NOT EXISTS sequence_count INTEGER")


def _backfill_sequence_identity() -> None:
    """Recover valid minute identity from retained raw contract metadata.

    Older runs were persisted before the sequence identity columns existed.
    Only rows with all four fields absent are candidates.  The CTE picks the
    raw row by explicit payload id first, then legacy run id, and the guarded
    CASE expressions make malformed JSON, oversized strings, and out-of-range
    numbers resolve to no candidate rather than aborting the migration.
    """
    op.execute(
        """
        WITH raw_candidates AS (
            SELECT DISTINCT ON (run.run_id)
                run.run_id,
                payload.payload -> 'batch' AS batch
            FROM ingestion_run AS run
            JOIN raw.market_payload AS payload
              ON payload.raw_payload_id = run.raw_payload_id
              OR payload.run_id = run.run_id
            WHERE run.dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
              AND run.delivery_mode = 'sequenced_snapshot'
              AND payload.dataset_key = run.dataset_key
              AND (run.source IS NULL OR payload.source = run.source)
              AND (run.schema_id IS NULL OR payload.schema_id = run.schema_id)
              AND (run.schema_version IS NULL OR payload.schema_version = run.schema_version)
              AND payload.expire_at >= now()
              AND run.snapshot_id IS NULL
              AND run.daily_update_id IS NULL
              AND run.sequence IS NULL
              AND run.sequence_count IS NULL
            ORDER BY
                run.run_id,
                CASE
                    WHEN payload.raw_payload_id = run.raw_payload_id THEN 0
                    ELSE 1
                END,
                payload.created_at DESC,
                payload.raw_payload_id DESC
        ), parsed AS (
            SELECT
                run_id,
                btrim(batch ->> 'snapshot_id') AS snapshot_id,
                btrim(batch ->> 'daily_update_id') AS daily_update_id,
                CASE
                    WHEN batch ->> 'sequence' ~ '^[1-9][0-9]{0,4}$'
                    THEN (batch ->> 'sequence')::integer
                END AS sequence,
                CASE
                    WHEN batch ->> 'sequence_count' ~ '^[1-9][0-9]{0,4}$'
                    THEN (batch ->> 'sequence_count')::integer
                END AS sequence_count,
                batch ->> 'delivery_mode' AS delivery_mode,
                jsonb_typeof(batch) AS batch_type,
                jsonb_typeof(batch -> 'snapshot_id') AS snapshot_id_type,
                jsonb_typeof(batch -> 'daily_update_id') AS daily_update_id_type
            FROM raw_candidates
        )
        UPDATE ingestion_run AS run
        SET snapshot_id = parsed.snapshot_id,
            daily_update_id = parsed.daily_update_id,
            sequence = parsed.sequence,
            sequence_count = parsed.sequence_count
        FROM parsed
        WHERE run.run_id = parsed.run_id
          AND parsed.batch_type = 'object'
          AND parsed.delivery_mode = 'sequenced_snapshot'
          AND parsed.snapshot_id_type = 'string'
          AND parsed.daily_update_id_type = 'string'
          AND length(parsed.snapshot_id) BETWEEN 1 AND 100
          AND length(parsed.daily_update_id) BETWEEN 1 AND 100
          AND parsed.sequence IS NOT NULL
          AND parsed.sequence_count IS NOT NULL
          AND parsed.sequence <= parsed.sequence_count
        """
    )


def _add_sequence_constraints() -> None:
    constraints = {
        "ck_ingestion_run_minute_identity_coherent": (
            "(snapshot_id IS NULL AND daily_update_id IS NULL "
            "AND sequence IS NULL AND sequence_count IS NULL) OR "
            "(delivery_mode = 'sequenced_snapshot' AND snapshot_id IS NOT NULL "
            "AND daily_update_id IS NOT NULL AND sequence IS NOT NULL "
            "AND sequence_count IS NOT NULL)"
        ),
        "ck_ingestion_run_minute_sequence_positive": "sequence IS NULL OR sequence >= 1",
        "ck_ingestion_run_minute_sequence_count_positive": (
            "sequence_count IS NULL OR sequence_count >= 1"
        ),
        "ck_ingestion_run_minute_sequence_order": (
            "sequence IS NULL OR sequence_count IS NULL OR sequence <= sequence_count"
        ),
    }
    for name, expression in constraints.items():
        # Alembic runs this migration transactionally, so a retry after a
        # failed upgrade replays the whole migration.  Direct constraint DDL
        # also avoids fragile nested quoting in PostgreSQL EXECUTE strings.
        op.create_check_constraint(name, "ingestion_run", expression)


def _backfill_minute_policy() -> None:
    # Only an absent delivery_expectation is filled.  Existing nested policy
    # objects (including operator-owned partial or custom values) are untouched.
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = jsonb_set(
                COALESCE(config, '{}'::jsonb),
                '{delivery_expectation}',
                CAST(:expectation AS jsonb),
                true
            ),
                updated_at = now()
            WHERE dataset_key IN ('tw_equity_minute', 'tw_etf_minute')
              AND (config IS NULL OR jsonb_typeof(config) = 'object')
              AND NOT (COALESCE(config, '{}'::jsonb) ? 'delivery_expectation')
            """
        ).bindparams(expectation=_MINUTE_EXPECTATION_JSON)
    )


def upgrade() -> None:
    _add_sequence_columns()
    _backfill_sequence_identity()
    _add_sequence_constraints()
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_minute_sequence_group
        ON ingestion_run (
            dataset_key, source, schema_id, schema_version, batch_data_date,
            delivery_mode, daily_update_id, snapshot_id, sequence, sequence_count,
            is_rerun
        )
        """
    )
    _backfill_minute_policy()


def downgrade() -> None:
    # Policy provenance is not stored in the registry row.  Even an exact
    # match may be an operator-owned value (or a value seeded before this
    # migration), so rollback must never guess that it is safe to delete.
    op.execute("DROP INDEX IF EXISTS idx_run_minute_sequence_group")
    for name in (
        "ck_ingestion_run_minute_sequence_order",
        "ck_ingestion_run_minute_sequence_count_positive",
        "ck_ingestion_run_minute_sequence_positive",
        "ck_ingestion_run_minute_identity_coherent",
    ):
        op.execute(f'ALTER TABLE ingestion_run DROP CONSTRAINT IF EXISTS "{name}"')
    op.execute("ALTER TABLE ingestion_run DROP COLUMN IF EXISTS sequence_count")
    op.execute("ALTER TABLE ingestion_run DROP COLUMN IF EXISTS sequence")
    op.execute("ALTER TABLE ingestion_run DROP COLUMN IF EXISTS daily_update_id")
    op.execute("ALTER TABLE ingestion_run DROP COLUMN IF EXISTS snapshot_id")
