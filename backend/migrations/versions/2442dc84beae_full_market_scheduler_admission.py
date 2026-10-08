"""Trusted full-market enrollment and immutable scheduler admission.

Revision ID: 2442dc84beae
Revises: 1331cb73adad
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "2442dc84beae"
down_revision = "1331cb73adad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "full_market_environment",
        sa.Column("environment", sa.String(20), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "full_market_enrollment",
        sa.Column("enrollment_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column(
            "source_client_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("source_client.client_id"),
            nullable=False,
        ),
        sa.Column("runtime_id", sa.String(200), nullable=False),
        sa.Column("declaration_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("declaration", pg.JSONB(), nullable=False),
        sa.Column("installation_sha256", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True)),
        sa.Column("reported_enabled", sa.Boolean(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_full_market_enrollment_identity",
        "full_market_enrollment",
        ["environment", "provider", "created_at"],
    )
    op.create_table(
        "full_market_admission",
        sa.Column("admission_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scheduler_key",
            sa.String(100),
            sa.ForeignKey("scheduler_control.scheduler_key"),
            nullable=False,
        ),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column(
            "enrollment_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("full_market_enrollment.enrollment_id"),
            nullable=False,
        ),
        sa.Column("capacity", pg.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "scheduler_key", "control_revision", name="uq_full_market_admission_revision"
        ),
    )
    op.create_table(
        "full_market_admission_feed",
        sa.Column(
            "admission_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("full_market_admission.admission_id"),
            primary_key=True,
        ),
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key"),
            primary_key=True,
        ),
        sa.Column(
            "baseline_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("universe_release.release_id"),
            nullable=False,
        ),
        sa.Column(
            "calendar_revision_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("calendar_year_revision.id"),
            nullable=False,
        ),
    )
    op.create_table(
        "full_market_dataset_state",
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key"),
            primary_key=True,
        ),
        sa.Column("first_start_date", sa.Date(), nullable=False),
    )
    # Preserve valid historic cutoffs. Invalid legacy dates remain visibly blocked.
    op.execute(
        sa.text("""INSERT INTO full_market_dataset_state(dataset_key,first_start_date)
        SELECT dataset_key, (config->'full_market'->>'activation_date')::date
        FROM dataset_registry WHERE config->'full_market'->>'activation_date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
        AND pg_input_is_valid(config->'full_market'->>'activation_date', 'date')""")
    )
    op.execute(
        sa.text(
            """UPDATE dataset_registry SET config=jsonb_set(config,
            '{full_market,_registry_active_before_2442dc84beae}',to_jsonb(is_active),true),
            is_active=true WHERE config->'full_market'->>'required'='true' AND NOT is_active"""
        )
    )


def downgrade() -> None:
    # Only changed policies carry the marker; already-active/shared feeds stay active.
    op.execute(
        sa.text("""UPDATE dataset_registry SET
        is_active=(config->'full_market'->>'_registry_active_before_2442dc84beae')::boolean,
        config=jsonb_set(config, '{full_market}', (config->'full_market') - '_registry_active_before_2442dc84beae')
        WHERE jsonb_typeof(config->'full_market'->'_registry_active_before_2442dc84beae')='boolean'""")
    )
    for table in (
        "full_market_dataset_state",
        "full_market_admission_feed",
        "full_market_admission",
        "full_market_enrollment",
        "full_market_environment",
    ):
        op.drop_table(table)
