"""add ingestion attempt audit

Revision ID: 2ad3e4f5a6b7
Revises: 19c0d1e2f3a4
Create Date: 2026-07-21 00:00:00.000000
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2ad3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "19c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DATASET_CONTRACTS = {
    "tw_equity_eod": {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
        "delivery_expectation": {
            "delivery_mode": "full_snapshot",
            "freshness_hours": 36,
            "minimum_record_count": 2100,
            "maximum_count_drop_ratio": 0.1,
        },
    },
    "tw_etf_eod": {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {"market": "TW", "asset_class": "etf", "currency": "TWD"},
        "delivery_expectation": {
            "delivery_mode": "full_snapshot",
            "freshness_hours": 36,
            "minimum_record_count": 190,
            "maximum_count_drop_ratio": 0.1,
        },
    },
    "futures_continuous_eod": {
        "schema_id": "futures_continuous_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {"market": "WTX", "asset_class": "future", "currency": "TWD"},
        "delivery_expectation": {
            "delivery_mode": "full_snapshot",
            "freshness_hours": 36,
            "minimum_record_count": 1,
            "maximum_count_drop_ratio": 0.5,
        },
    },
    "wtx_eod": {
        "schema_id": "futures_continuous_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "audit",
        "defaults": {"market": "WTX", "asset_class": "future", "currency": "TWD"},
        "delivery_expectation": {
            "delivery_mode": "full_snapshot",
            "freshness_hours": 36,
            "minimum_record_count": 1,
            "maximum_count_drop_ratio": 0.5,
        },
    },
}


def upgrade() -> None:
    op.add_column("ingestion_run", sa.Column("schema_id", sa.String(50), nullable=True))
    op.add_column("ingestion_run", sa.Column("schema_version", sa.Integer(), nullable=True))
    op.add_column(
        "market_payload",
        sa.Column("schema_id", sa.String(50), nullable=True),
        schema="raw",
    )
    op.add_column(
        "market_payload",
        sa.Column("schema_version", sa.Integer(), nullable=True),
        schema="raw",
    )

    op.create_table(
        "ingestion_attempt",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_client_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_key", sa.String(50), nullable=True),
        sa.Column("source", sa.String(50), nullable=True),
        sa.Column("schema_id", sa.String(50), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("request_key", sa.String(100), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=True),
        sa.Column("request_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('received', 'accepted', 'duplicate', 'rejected')",
            name="ck_ingestion_attempt_ingestion_attempt_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name="fk_ingestion_attempt_run_id_ingestion_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_client_id"],
            ["source_client.client_id"],
            name="fk_ingestion_attempt_source_client_id_source_client",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_ingestion_attempt"),
    )
    op.create_index(
        "idx_ingestion_attempt_status_created",
        "ingestion_attempt",
        ["status", "created_at"],
    )
    op.create_index(
        "idx_ingestion_attempt_dataset_created",
        "ingestion_attempt",
        ["dataset_key", "created_at"],
    )
    op.create_index(
        "idx_ingestion_attempt_client_created",
        "ingestion_attempt",
        ["source_client_id", "created_at"],
    )
    op.create_index(
        "idx_ingestion_attempt_idempotency",
        "ingestion_attempt",
        ["source_client_id", "dataset_key", "idempotency_key"],
    )

    connection = op.get_bind()
    for dataset_key, contract_config in DATASET_CONTRACTS.items():
        connection.execute(
            sa.text("""
                UPDATE dataset_registry
                SET config = COALESCE(config, '{}'::jsonb) || CAST(:contract_config AS jsonb),
                    updated_at = now()
                WHERE dataset_key = :dataset_key
                """),
            {
                "dataset_key": dataset_key,
                "contract_config": json.dumps(contract_config),
            },
        )


def downgrade() -> None:
    op.drop_index("idx_ingestion_attempt_idempotency", table_name="ingestion_attempt")
    op.drop_index("idx_ingestion_attempt_client_created", table_name="ingestion_attempt")
    op.drop_index("idx_ingestion_attempt_dataset_created", table_name="ingestion_attempt")
    op.drop_index("idx_ingestion_attempt_status_created", table_name="ingestion_attempt")
    op.drop_table("ingestion_attempt")
    op.drop_column("market_payload", "schema_version", schema="raw")
    op.drop_column("market_payload", "schema_id", schema="raw")
    op.drop_column("ingestion_run", "schema_version")
    op.drop_column("ingestion_run", "schema_id")
