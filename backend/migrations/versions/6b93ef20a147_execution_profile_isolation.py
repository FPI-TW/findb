"""Isolate execution profiles and retain canonical delivery evidence.

Revision ID: 6b93ef20a147
Revises: 3553ed95cfbf
"""

from datetime import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "6b93ef20a147"
down_revision = "3553ed95cfbf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, default in (
        ("full_market_dataset_state", "full_market"),
        ("daily_delivery_plan", "full_market"),
        ("scheduler_control", "pilot"),
        ("missing_delivery_alert", "pilot"),
    ):
        op.add_column(
            table,
            sa.Column("execution_profile", sa.String(20), nullable=False, server_default=default),
        )
    op.execute(
        sa.text(
            "UPDATE scheduler_control SET execution_profile = 'full_market' WHERE left(scheduler_key, 12) = 'full_market_'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE missing_delivery_alert SET execution_profile = 'full_market' WHERE details->>'code' = 'FULL_MARKET_INCOMPLETE'"
        )
    )
    op.add_column(
        "scheduler_control", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        sa.text("""
        UPDATE missing_delivery_alert
        SET status='resolved', resolved_at=CURRENT_TIMESTAMP,
            details=COALESCE(details,'{}'::jsonb) ||
                '{"resolution":"legacy_pilot_retired"}'::jsonb
        WHERE execution_profile='pilot' AND status='open'
    """)
    )
    op.execute(
        sa.text("""
        UPDATE scheduler_control SET retired_at=CURRENT_TIMESTAMP,
            desired_state='stopped', revision=revision+1, updated_at=CURRENT_TIMESTAMP
        WHERE scheduler_key NOT IN (
            'pilot_twelve_data_v1','pilot_finlab_v1','pilot_shioaji_v1','pilot_taifex_v1',
            'full_market_twelve_data_v1','full_market_finlab_v1',
            'full_market_shioaji_v1','full_market_taifex_v1')
    """)
    )
    # Migration owns first installation; repeated registry reconciliation never
    # changes desired_state or overwrites existing full-market controls.
    for provider, datasets, slot, clock in (
        ("twelve_data", ("us_equity_eod", "hk_equity_eod"), "western_markets_window", "08:00"),
        ("finlab", ("tw_equity_eod", "tw_etf_eod"), "taiwan_market_window", "18:00"),
        ("shioaji", ("tw_equity_minute", "tw_etf_minute"), "taiwan_market_window", "18:00"),
        ("taifex", ("tw_futures_eod",), "taiwan_market_window", "18:00"),
    ):
        for profile in ("pilot", "full_market"):
            key = f"{profile}_{provider}_v1"
            op.get_bind().execute(
                sa.text("""
                INSERT INTO scheduler_control (scheduler_key,provider,execution_profile,
                    slot_id,scheduled_local_time,timezone,desired_state,observed_state,
                    revision,created_at,updated_at)
                VALUES (:key,:provider,:profile,:slot,CAST(:clock AS time),'Asia/Taipei',
                    'stopped','stopped',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                ON CONFLICT (scheduler_key) DO NOTHING
            """),
                {
                    "key": key,
                    "provider": provider,
                    "profile": profile,
                    "slot": slot,
                    "clock": time.fromisoformat(clock),
                },
            )
            for dataset in datasets:
                op.get_bind().execute(
                    sa.text("""
                    INSERT INTO scheduler_dataset(scheduler_key,dataset_key)
                    SELECT :key,dataset_key FROM dataset_registry WHERE dataset_key=:dataset
                    ON CONFLICT DO NOTHING
                """),
                    {"key": key, "dataset": dataset},
                )
    op.drop_constraint("pk_full_market_dataset_state", "full_market_dataset_state", type_="primary")
    op.create_primary_key(
        "pk_full_market_dataset_state",
        "full_market_dataset_state",
        ["dataset_key", "execution_profile"],
    )
    op.create_check_constraint(
        "dataset_state_profile",
        "full_market_dataset_state",
        "execution_profile IN ('pilot', 'full_market')",
    )
    op.drop_constraint("uq_daily_delivery_plan_scope_date", "daily_delivery_plan", type_="unique")
    op.create_unique_constraint(
        "uq_daily_delivery_plan_scope_date",
        "daily_delivery_plan",
        ["dataset_key", "provider", "trade_date", "execution_profile"],
    )
    op.create_check_constraint(
        "delivery_plan_profile",
        "daily_delivery_plan",
        "execution_profile IN ('pilot', 'full_market')",
    )
    op.drop_constraint(
        "uq_missing_delivery_identity_date", "missing_delivery_alert", type_="unique"
    )
    op.create_unique_constraint(
        "uq_missing_delivery_identity_date",
        "missing_delivery_alert",
        [
            "dataset_key",
            "source",
            "schema_id",
            "schema_version",
            "expected_data_date",
            "execution_profile",
        ],
    )
    op.create_table(
        "delivery_member_evidence",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_run.run_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("member_key", sa.String(200), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Only actual canonical provenance can be recovered, never old raw counts.
    for table in ("market_data_eod", "market_data_minute"):
        op.execute(
            sa.text(f"""
            INSERT INTO delivery_member_evidence (run_id, member_key, created_at)
            SELECT DISTINCT r.run_id, i.symbol, CURRENT_TIMESTAMP
            FROM {table} c JOIN instruments i ON i.instrument_id=c.instrument_id
            JOIN ingestion_run r ON r.run_id=c.run_id
            WHERE r.delivery_part_id IS NOT NULL
              AND r.status IN ('completed', 'completed_with_errors')
            ON CONFLICT DO NOTHING
        """)
        )
    op.execute(
        sa.text("""
        INSERT INTO delivery_member_evidence (run_id, member_key, created_at)
        SELECT DISTINCT r.run_id, i.symbol || '|' || f.contract_code || '|' || c.session, CURRENT_TIMESTAMP
        FROM futures_contract_eod c JOIN futures_contract f ON f.contract_id=c.contract_id
        JOIN instruments i ON i.instrument_id=f.instrument_id
        JOIN ingestion_run r ON r.run_id=c.run_id
        WHERE r.delivery_part_id IS NOT NULL
          AND r.status IN ('completed', 'completed_with_errors')
        ON CONFLICT DO NOTHING
    """)
    )


def downgrade() -> None:
    raise RuntimeError(
        "Execution-profile cutover establishes a new rollback floor; restore a matching backup for pre-cutover recovery"
    )
