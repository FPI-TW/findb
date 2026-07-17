"""durable ingestion queue and source clients

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-07-15 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_client",
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("source_name", sa.String(length=50), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("allowed_datasets", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rate_limit_requests", sa.Integer(), server_default="100", nullable=False),
        sa.Column("rate_limit_window", sa.Integer(), server_default="60", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("client_id", name=op.f("pk_source_client")),
        sa.UniqueConstraint("key_hash", name="uq_source_client_key_hash"),
    )
    op.create_index("idx_source_client_revoked", "source_client", ["revoked_at"])

    op.add_column(
        "market_payload",
        sa.Column("raw_payload_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="raw",
    )
    op.add_column(
        "market_payload",
        sa.Column("source_client_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="raw",
    )
    op.add_column(
        "market_payload",
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        schema="raw",
    )
    op.execute("""
        UPDATE raw.market_payload
        SET raw_payload_id = md5(idempotency_key || ':' || run_id::text)::uuid
        WHERE raw_payload_id IS NULL
        """)
    op.alter_column("market_payload", "raw_payload_id", nullable=False, schema="raw")
    op.drop_constraint("pk_market_payload", "market_payload", schema="raw", type_="primary")
    op.create_primary_key("pk_market_payload", "market_payload", ["raw_payload_id"], schema="raw")
    op.create_foreign_key(
        "fk_market_payload_source_client_id_source_client",
        "market_payload",
        "source_client",
        ["source_client_id"],
        ["client_id"],
        source_schema="raw",
    )
    op.execute("""
        ALTER TABLE raw.market_payload
        ADD CONSTRAINT uq_raw_payload_idempotency_scope
        UNIQUE NULLS NOT DISTINCT (source_client_id, dataset_key, idempotency_key)
        """)

    op.add_column(
        "ingestion_run",
        sa.Column("source_client_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ingestion_run",
        sa.Column("raw_payload_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ingestion_run",
        sa.Column("failure_code", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "ingestion_run",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "ingestion_run",
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
    )
    op.add_column(
        "ingestion_run",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_ingestion_run_source_client_id_source_client",
        "ingestion_run",
        "source_client",
        ["source_client_id"],
        ["client_id"],
    )
    op.create_foreign_key(
        "fk_ingestion_run_raw_payload_id_market_payload",
        "ingestion_run",
        "market_payload",
        ["raw_payload_id"],
        ["raw_payload_id"],
        referent_schema="raw",
        ondelete="SET NULL",
    )
    op.execute("""
        UPDATE ingestion_run AS ir
        SET raw_payload_id = raw.raw_payload_id
        FROM raw.market_payload AS raw
        WHERE raw.run_id = ir.run_id
          AND ir.raw_payload_id IS NULL
        """)

    op.create_table(
        "normalization_job",
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_key", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["dataset_key"],
            ["dataset_registry.dataset_key"],
            name=op.f("fk_normalization_job_dataset_key_dataset_registry"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_normalization_job_run_id_ingestion_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_normalization_job")),
        sa.UniqueConstraint("run_id", name="uq_normalization_job_run"),
    )
    op.create_index(
        "idx_normalization_job_state_available",
        "normalization_job",
        ["status", "available_at"],
    )
    op.create_index("idx_normalization_job_dataset", "normalization_job", ["dataset_key"])

    op.create_table(
        "normalization_outbox",
        sa.Column("outbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["normalization_job.job_id"],
            name=op.f("fk_normalization_outbox_job_id_normalization_job"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_normalization_outbox_run_id_ingestion_run"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("outbox_id", name=op.f("pk_normalization_outbox")),
    )
    op.create_index(
        "idx_normalization_outbox_pending",
        "normalization_outbox",
        ["status", "available_at"],
    )
    op.create_index("idx_normalization_outbox_run", "normalization_outbox", ["run_id"])

    op.create_table(
        "normalization_worker_heartbeat",
        sa.Column("worker_id", sa.String(length=200), nullable=False),
        sa.Column("current_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_normalization_worker_heartbeat")),
    )

    op.execute("""
        UPDATE ingestion_run
        SET status = 'queued', failure_code = NULL, error_message = NULL,
            completed_at = NULL, next_retry_at = NULL
        WHERE status IN ('pending', 'processing')
          AND raw_payload_id IS NOT NULL
        """)
    op.execute("""
        INSERT INTO normalization_job (
            job_id, run_id, dataset_key, status, delivery_id,
            attempt_count, max_attempts, available_at, created_at, updated_at
        )
        SELECT
            md5(run_id::text || '-job')::uuid,
            run_id,
            dataset_key,
            'queued',
            md5(run_id::text || '-delivery-1')::uuid,
            0,
            5,
            now(),
            created_at,
            now()
        FROM ingestion_run
        WHERE status = 'queued' AND raw_payload_id IS NOT NULL
        ON CONFLICT (run_id) DO NOTHING
        """)
    op.execute("""
        INSERT INTO normalization_outbox (
            outbox_id, job_id, run_id, delivery_id, event_type, status,
            available_at, publish_attempts, created_at, updated_at
        )
        SELECT
            md5(job.run_id::text || '-outbox-1')::uuid,
            job.job_id,
            job.run_id,
            job.delivery_id,
            'normalize_run',
            'pending',
            now(),
            0,
            now(),
            now()
        FROM normalization_job AS job
        WHERE job.status = 'queued'
        """)
    op.execute("""
        UPDATE ingestion_run
        SET status = 'failed',
            failure_code = 'RAW_PAYLOAD_MISSING',
            error_message = 'Raw payload missing during durable queue migration',
            completed_at = now()
        WHERE status IN ('pending', 'processing') AND raw_payload_id IS NULL
        """)


def downgrade() -> None:
    op.drop_table("normalization_worker_heartbeat")
    op.drop_index("idx_normalization_outbox_run", table_name="normalization_outbox")
    op.drop_index("idx_normalization_outbox_pending", table_name="normalization_outbox")
    op.drop_table("normalization_outbox")
    op.drop_index("idx_normalization_job_dataset", table_name="normalization_job")
    op.drop_index("idx_normalization_job_state_available", table_name="normalization_job")
    op.drop_table("normalization_job")

    op.drop_constraint(
        "fk_ingestion_run_raw_payload_id_market_payload", "ingestion_run", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_ingestion_run_source_client_id_source_client",
        "ingestion_run",
        type_="foreignkey",
    )
    for column in (
        "next_retry_at",
        "max_attempts",
        "attempt_count",
        "failure_code",
        "raw_payload_id",
        "source_client_id",
    ):
        op.drop_column("ingestion_run", column)

    op.drop_constraint(
        "uq_raw_payload_idempotency_scope", "market_payload", schema="raw", type_="unique"
    )
    op.drop_constraint(
        "fk_market_payload_source_client_id_source_client",
        "market_payload",
        schema="raw",
        type_="foreignkey",
    )
    op.drop_constraint("pk_market_payload", "market_payload", schema="raw", type_="primary")
    op.create_primary_key("pk_market_payload", "market_payload", ["idempotency_key"], schema="raw")
    op.drop_column("market_payload", "payload_sha256", schema="raw")
    op.drop_column("market_payload", "source_client_id", schema="raw")
    op.drop_column("market_payload", "raw_payload_id", schema="raw")

    op.drop_index("idx_source_client_revoked", table_name="source_client")
    op.drop_table("source_client")
