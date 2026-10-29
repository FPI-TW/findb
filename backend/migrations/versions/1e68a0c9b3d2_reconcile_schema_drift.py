"""Reconcile CHECK names and calendar defaults with ORM metadata.

Revision ID: 1e68a0c9b3d2
Revises: 0d57f9b8a2c1
"""

import sqlalchemy as sa
from alembic import op

revision = "1e68a0c9b3d2"
down_revision = "0d57f9b8a2c1"
branch_labels = None
depends_on = None

# Historical migrations supplied already-prefixed CHECK names to the naming
# convention. These are the actual PostgreSQL names, including SQLAlchemy's
# deterministic 63-byte truncation. Renaming preserves definitions and OIDs.
CHECK_RENAMES = (
    (
        "scheduler_control",
        "ck_scheduler_control_ck_scheduler_control_desired_state_valid",
        "ck_scheduler_control_desired_state_valid",
    ),
    (
        "scheduler_control",
        "ck_scheduler_control_ck_scheduler_control_observed_state_valid",
        "ck_scheduler_control_observed_state_valid",
    ),
    (
        "scheduler_control",
        "ck_scheduler_control_ck_scheduler_control_provider_nonempty",
        "ck_scheduler_control_provider_nonempty",
    ),
    (
        "scheduler_control",
        "ck_scheduler_control_ck_scheduler_control_revision_positive",
        "ck_scheduler_control_revision_positive",
    ),
    (
        "scheduler_control",
        "ck_scheduler_control_ck_scheduler_control_scheduler_key_2686",
        "ck_scheduler_control_scheduler_key_nonempty",
    ),
    (
        "scheduler_control",
        "ck_scheduler_control_ck_scheduler_control_slot_id_nonempty",
        "ck_scheduler_control_slot_id_nonempty",
    ),
    (
        "scheduler_control",
        "ck_scheduler_control_ck_scheduler_control_timezone_nonempty",
        "ck_scheduler_control_timezone_nonempty",
    ),
    (
        "missing_delivery_alert",
        "ck_missing_delivery_alert_ck_missing_delivery_alert_sta_8031",
        "ck_missing_delivery_alert_status_valid",
    ),
    (
        "ingestion_run",
        "ck_ingestion_run_ck_ingestion_run_backfill_coverage_end_c214",
        "ck_ingestion_run_backfill_coverage_end_matches_batch_date",
    ),
    (
        "ingestion_run",
        "ck_ingestion_run_ck_ingestion_run_coverage_date_order",
        "ck_ingestion_run_coverage_date_order",
    ),
    (
        "ingestion_run",
        "ck_ingestion_run_ck_ingestion_run_coverage_dates_paired",
        "ck_ingestion_run_coverage_dates_paired",
    ),
    (
        "ingestion_run",
        "ck_ingestion_run_ck_ingestion_run_minute_identity_coherent",
        "ck_ingestion_run_minute_identity_coherent",
    ),
    (
        "ingestion_run",
        "ck_ingestion_run_ck_ingestion_run_minute_sequence_count_ccb9",
        "ck_ingestion_run_minute_sequence_count_positive",
    ),
    (
        "ingestion_run",
        "ck_ingestion_run_ck_ingestion_run_minute_sequence_order",
        "ck_ingestion_run_minute_sequence_order",
    ),
    (
        "ingestion_run",
        "ck_ingestion_run_ck_ingestion_run_minute_sequence_positive",
        "ck_ingestion_run_minute_sequence_positive",
    ),
    (
        "ingestion_attempt",
        "ck_ingestion_attempt_ck_ingestion_attempt_ingestion_att_1bac",
        "ck_ingestion_attempt_ingestion_attempt_status_valid",
    ),
)


def _rename_check(table: str, old: str, new: str) -> None:
    # All identifiers are immutable literals above, never runtime input.
    op.execute(sa.text(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{old}" TO "{new}"'))


def upgrade() -> None:
    for table, old, new in CHECK_RENAMES:
        _rename_check(table, old, new)
    # Validate existing rows as well as future writes. Invalid market codes must
    # abort the transaction rather than silently rewriting operational data.
    op.create_check_constraint(
        op.f("ck_calendar_market_calendar_market_code_valid"),
        "calendar_market",
        "market ~ '^[A-Z][A-Z0-9_]{1,9}$'",
    )
    for column in ("created_at", "updated_at"):
        # timestamptz now() preserves the instant for every session timezone.
        op.alter_column("trading_calendar", column, server_default=sa.text("now()"))


def downgrade() -> None:
    for column in ("created_at", "updated_at"):
        op.alter_column(
            "trading_calendar", column, server_default=sa.text("timezone('utc', now())")
        )
    op.drop_constraint(
        op.f("ck_calendar_market_calendar_market_code_valid"), "calendar_market", type_="check"
    )
    for table, old, new in reversed(CHECK_RENAMES):
        _rename_check(table, new, old)
