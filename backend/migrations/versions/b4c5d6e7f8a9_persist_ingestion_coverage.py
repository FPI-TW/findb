"""Persist bounded EOD coverage on ingestion runs.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, Sequence[str], None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _backfill_retained_coverage() -> None:
    """Copy only paired, parseable coverage dates from retained raw JSON.

    ``pg_input_is_valid`` keeps malformed provider metadata from aborting the
    migration.  The CASE expressions additionally keep the date casts inside
    the validated branch.  Semantic bounds are checked before writing so the
    new run constraints cannot reject legacy rows.
    """
    op.execute(
        sa.text(
            """
            WITH candidates AS (
                SELECT DISTINCT ON (run.run_id)
                    run.run_id,
                    run.delivery_mode,
                    run.batch_data_date,
                    raw.payload->'batch'->>'coverage_start_date' AS start_text,
                    raw.payload->'batch'->>'coverage_end_date' AS end_text
                FROM ingestion_run AS run
                JOIN raw.market_payload AS raw
                  ON raw.raw_payload_id = run.raw_payload_id
                  OR raw.run_id = run.run_id
                WHERE raw.dataset_key = run.dataset_key
                  AND (run.source IS NULL OR raw.source = run.source)
                  AND (run.schema_id IS NULL OR raw.schema_id = run.schema_id)
                  AND (run.schema_version IS NULL OR raw.schema_version = run.schema_version)
                  AND raw.expire_at >= now()
                  AND jsonb_typeof(raw.payload) = 'object'
                  AND jsonb_typeof(raw.payload->'batch') = 'object'
                ORDER BY
                    run.run_id,
                    CASE
                        WHEN raw.raw_payload_id = run.raw_payload_id THEN 0
                        ELSE 1
                    END,
                    raw.created_at DESC,
                    raw.raw_payload_id DESC
            ), parsed AS (
                SELECT
                    run_id,
                    delivery_mode,
                    batch_data_date,
                    CASE
                        WHEN start_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                         AND pg_input_is_valid(start_text, 'date')
                        THEN start_text::date
                        ELSE NULL
                    END AS coverage_start_date,
                    CASE
                        WHEN end_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                         AND pg_input_is_valid(end_text, 'date')
                        THEN end_text::date
                        ELSE NULL
                    END AS coverage_end_date
                FROM candidates
            )
            UPDATE ingestion_run AS run
            SET coverage_start_date = parsed.coverage_start_date,
                coverage_end_date = parsed.coverage_end_date
            FROM parsed
            WHERE run.run_id = parsed.run_id
              AND parsed.coverage_start_date IS NOT NULL
              AND parsed.coverage_end_date IS NOT NULL
              AND parsed.coverage_start_date <= parsed.coverage_end_date
              AND (
                    parsed.delivery_mode IS DISTINCT FROM 'backfill'
                    OR parsed.coverage_end_date = parsed.batch_data_date
              )
            """
        )
    )


def upgrade() -> None:
    op.add_column("ingestion_run", sa.Column("coverage_start_date", sa.Date(), nullable=True))
    op.add_column("ingestion_run", sa.Column("coverage_end_date", sa.Date(), nullable=True))

    _backfill_retained_coverage()

    op.create_check_constraint(
        "ck_ingestion_run_coverage_dates_paired",
        "ingestion_run",
        "(coverage_start_date IS NULL AND coverage_end_date IS NULL) OR "
        "(coverage_start_date IS NOT NULL AND coverage_end_date IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_ingestion_run_coverage_date_order",
        "ingestion_run",
        "coverage_start_date IS NULL OR coverage_start_date <= coverage_end_date",
    )
    op.create_check_constraint(
        "ck_ingestion_run_backfill_coverage_end_matches_batch_date",
        "ingestion_run",
        "delivery_mode IS DISTINCT FROM 'backfill' OR coverage_end_date IS NULL "
        "OR coverage_end_date = batch_data_date",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ingestion_run_backfill_coverage_end_matches_batch_date",
        "ingestion_run",
        type_="check",
    )
    op.drop_constraint("ck_ingestion_run_coverage_date_order", "ingestion_run", type_="check")
    op.drop_constraint("ck_ingestion_run_coverage_dates_paired", "ingestion_run", type_="check")
    op.drop_column("ingestion_run", "coverage_end_date")
    op.drop_column("ingestion_run", "coverage_start_date")
