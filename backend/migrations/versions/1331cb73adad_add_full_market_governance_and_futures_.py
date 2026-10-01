"""add full market governance and futures quotes

Revision ID: 1331cb73adad
Revises: b9c0d1e2f3a4
Create Date: 2026-10-01 09:49:38.464803
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1331cb73adad"
down_revision: Union[str, Sequence[str], None] = "b9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Frozen declarations: migrations never import mutable runtime seed modules.
_NEW_DATASETS = [
    {
        "dataset_key": "hk_equity_eod",
        "name": "港股日K",
        "description": "Staged twelve_data market_eod.v1 full-market daily feed",
        "asset_class": "equity",
        "market": "HK",
        "frequency": "daily",
        "is_active": False,
        "config": {
            "schema_id": "market_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "enforce",
            "allowed_sources": ["twelve_data"],
            "defaults": {"market": "HK", "asset_class": "equity", "currency": None},
            "delivery_expectation": {
                "delivery_mode": "full_snapshot",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 0,
                    "maximum_count_drop_ratio": 0.1,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "HK",
                    "timezone": "Asia/Hong_Kong",
                    "market_close_time": "16:10:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
                "missing_delivery": {"action": "disabled", "expected_sources": []},
                "schedule": {
                    "enabled": False,
                    "slot_id": "taiwan_market_window",
                    "local_time": "14:30:00",
                    "timezone": "Asia/Taipei",
                    "target_date_lag_days": 0,
                    "expected_sources": ["twelve_data"],
                },
            },
            "full_market": {
                "enabled": False,
                "required": True,
                "readiness_approved": False,
                "activation_date": None,
                "calendar_market": "HK",
            },
            "source_precedence": ["twelve_data"],
        },
    },
    {
        "dataset_key": "tw_etf_eod",
        "name": "台灣 ETF 日K",
        "description": "Staged finlab market_eod.v1 full-market daily feed",
        "asset_class": "etf",
        "market": "TW",
        "frequency": "daily",
        "is_active": False,
        "config": {
            "schema_id": "market_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "enforce",
            "allowed_sources": ["finlab"],
            "defaults": {"market": "TW", "asset_class": "etf", "currency": "TWD"},
            "delivery_expectation": {
                "delivery_mode": "full_snapshot",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 0,
                    "maximum_count_drop_ratio": 0.1,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "TW",
                    "timezone": "Asia/Taipei",
                    "market_close_time": "13:30:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
                "missing_delivery": {"action": "disabled", "expected_sources": []},
                "schedule": {
                    "enabled": False,
                    "slot_id": "taiwan_market_window",
                    "local_time": "14:30:00",
                    "timezone": "Asia/Taipei",
                    "target_date_lag_days": 0,
                    "expected_sources": ["finlab"],
                },
            },
            "full_market": {
                "enabled": False,
                "required": True,
                "readiness_approved": False,
                "activation_date": None,
                "calendar_market": "TW",
            },
            "source_precedence": ["finlab"],
        },
    },
    {
        "dataset_key": "tw_futures_eod",
        "name": "台灣期貨契約日K",
        "description": "Staged taifex futures_eod.v1 full-market daily feed",
        "asset_class": "future",
        "market": "TW",
        "frequency": "daily",
        "is_active": False,
        "config": {
            "schema_id": "futures_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "enforce",
            "allowed_sources": ["taifex"],
            "defaults": {"market": "TW", "asset_class": "future", "currency": "TWD"},
            "delivery_expectation": {
                "delivery_mode": "full_snapshot",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 0,
                    "maximum_count_drop_ratio": 0.1,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "TAIFEX",
                    "timezone": "Asia/Taipei",
                    "market_close_time": "13:45:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
                "missing_delivery": {"action": "disabled", "expected_sources": []},
                "schedule": {
                    "enabled": False,
                    "slot_id": "taiwan_market_window",
                    "local_time": "14:30:00",
                    "timezone": "Asia/Taipei",
                    "target_date_lag_days": 0,
                    "expected_sources": ["taifex"],
                },
            },
            "full_market": {
                "enabled": False,
                "required": True,
                "readiness_approved": False,
                "activation_date": None,
                "calendar_market": "TAIFEX",
            },
            "source_precedence": ["taifex"],
        },
    },
]


def _provision_controls() -> None:
    for provider, datasets, slot, clock in (
        ("twelve_data", ["us_equity_eod", "hk_equity_eod"], "western_markets_window", "08:00:00"),
        ("finlab", ["tw_equity_eod", "tw_etf_eod"], "taiwan_market_window", "18:00:00"),
        ("shioaji", ["tw_equity_minute", "tw_etf_minute"], "taiwan_market_window", "18:00:00"),
        ("taifex", ["tw_futures_eod"], "taiwan_market_window", "18:00:00"),
    ):
        key = f"full_market_{provider}_v1"
        op.execute(
            sa.text("""INSERT INTO scheduler_control
            (scheduler_key, provider, slot_id, scheduled_local_time, timezone, desired_state, observed_state, revision, created_at, updated_at)
            VALUES (:key, :provider, :slot, CAST(:clock AS time), 'Asia/Taipei', 'stopped', 'stopped', 1, now(), now())
            ON CONFLICT (scheduler_key) DO NOTHING
        """).bindparams(key=key, provider=provider, slot=slot, clock=clock)
        )
        for dataset in datasets:
            op.execute(
                sa.text("""INSERT INTO scheduler_dataset (scheduler_key, dataset_key)
                VALUES (:key, :dataset) ON CONFLICT DO NOTHING
            """).bindparams(key=key, dataset=dataset)
            )


def upgrade() -> None:
    for dataset in _NEW_DATASETS:
        op.execute(
            sa.text("""INSERT INTO dataset_registry
            (dataset_key, name, description, asset_class, market, frequency, is_active, config, created_at, updated_at)
            VALUES (:key, :name, :description, :asset, :market, :frequency, false, CAST(:config AS jsonb), now(), now())
            ON CONFLICT (dataset_key) DO UPDATE SET config =
                CASE WHEN dataset_registry.config ? 'full_market' THEN dataset_registry.config
                ELSE dataset_registry.config || jsonb_build_object('full_market', EXCLUDED.config->'full_market') END
        """).bindparams(
                key=dataset["dataset_key"],
                name=dataset["name"],
                description=dataset["description"],
                asset=dataset["asset_class"],
                market=dataset["market"],
                frequency=dataset["frequency"],
                config=json.dumps(dataset["config"]),
            )
        )
    op.execute(
        sa.text("""UPDATE dataset_registry SET config = config ||
        CAST(:governance AS jsonb)
        WHERE dataset_key IN ('us_equity_eod', 'tw_equity_eod', 'tw_equity_minute', 'tw_etf_minute')
        AND NOT config ? 'full_market'
    """).bindparams(
            governance=json.dumps(
                {
                    "full_market": {
                        "enabled": False,
                        "required": False,
                        "readiness_approved": False,
                        "activation_date": None,
                    }
                }
            )
        )
    )
    _provision_controls()
    op.create_table(
        "universe_release",
        sa.Column("release_id", sa.UUID(), nullable=False),
        sa.Column("dataset_key", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_timezone", sa.String(length=64), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("member_sha256", sa.String(length=64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("change_count", sa.Integer(), nullable=False),
        sa.Column("change_ratio", sa.NUMERIC(precision=12, scale=8), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(length=100), nullable=True),
        sa.Column("approval_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('candidate', 'published')",
            name=op.f("ck_universe_release_universe_release_status_valid"),
        ),
        sa.CheckConstraint(
            "member_count >= 1 AND member_count <= 100000",
            name=op.f("ck_universe_release_universe_release_count_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_key"],
            ["dataset_registry.dataset_key"],
            name=op.f("fk_universe_release_dataset_key_dataset_registry"),
        ),
        sa.PrimaryKeyConstraint("release_id", name=op.f("pk_universe_release")),
        sa.UniqueConstraint(
            "dataset_key",
            "provider",
            "effective_date",
            "member_sha256",
            name="uq_universe_release_submission",
        ),
    )
    op.create_index(
        "idx_universe_release_scope",
        "universe_release",
        ["dataset_key", "provider", "status", "effective_date"],
        unique=False,
    )
    op.create_table(
        "daily_delivery_plan",
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("dataset_key", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("release_id", sa.UUID(), nullable=False),
        sa.Column("activation_cutoff", sa.Date(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("member_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_key"],
            ["dataset_registry.dataset_key"],
            name=op.f("fk_daily_delivery_plan_dataset_key_dataset_registry"),
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["universe_release.release_id"],
            name=op.f("fk_daily_delivery_plan_release_id_universe_release"),
        ),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_daily_delivery_plan")),
        sa.UniqueConstraint(
            "dataset_key", "provider", "trade_date", name="uq_daily_delivery_plan_scope_date"
        ),
    )
    op.create_index(
        "idx_daily_delivery_plan_deadline",
        "daily_delivery_plan",
        ["deadline_at", "dataset_key"],
        unique=False,
    )
    op.create_table(
        "universe_member",
        sa.Column("member_id", sa.UUID(), nullable=False),
        sa.Column("release_id", sa.UUID(), nullable=False),
        sa.Column("member_key", sa.String(length=150), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("provider_symbol", sa.String(length=100), nullable=False),
        sa.Column("exchange", sa.String(length=30), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False),
        sa.Column("asset_class", sa.String(length=20), nullable=False),
        sa.Column("classification", sa.String(length=100), nullable=True),
        sa.Column("contract_code", sa.String(length=50), nullable=True),
        sa.Column("product_code", sa.String(length=50), nullable=True),
        sa.Column("contract_month", sa.String(length=10), nullable=True),
        sa.Column("session", sa.String(length=20), nullable=True),
        sa.Column("mapping_status", sa.String(length=10), nullable=False),
        sa.Column("raw_evidence_ref", sa.String(length=2048), nullable=True),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["universe_release.release_id"],
            name=op.f("fk_universe_member_release_id_universe_release"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("member_id", name=op.f("pk_universe_member")),
        sa.UniqueConstraint("release_id", "member_key", name="uq_universe_member_key"),
    )
    op.create_index("idx_universe_member_symbol", "universe_member", ["symbol"], unique=False)
    op.create_table(
        "daily_delivery_part",
        sa.Column("part_id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.String(length=100), nullable=False),
        sa.Column("member_sha256", sa.String(length=64), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["daily_delivery_plan.plan_id"],
            name=op.f("fk_daily_delivery_part_plan_id_daily_delivery_plan"),
        ),
        sa.PrimaryKeyConstraint("part_id", name=op.f("pk_daily_delivery_part")),
        sa.UniqueConstraint("plan_id", "part_number", name="uq_daily_delivery_part_number"),
        sa.UniqueConstraint("work_item_id", name="uq_daily_delivery_part_work_item"),
    )
    op.create_table(
        "daily_delivery_member",
        sa.Column("member_id", sa.UUID(), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("part_id", sa.UUID(), nullable=False),
        sa.Column("member_key", sa.String(length=150), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("provider_symbol", sa.String(length=100), nullable=False),
        sa.Column("product_code", sa.String(length=50), nullable=True),
        sa.Column("contract_code", sa.String(length=50), nullable=True),
        sa.Column("session", sa.String(length=20), nullable=True),
        sa.Column("mapping_status", sa.String(length=10), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=True),
        sa.Column("outcome_reason", sa.String(length=30), nullable=True),
        sa.Column("outcome_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outcome_history", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('no_data', 'blocked')",
            name=op.f("ck_daily_delivery_member_daily_delivery_member_outcome_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["part_id"],
            ["daily_delivery_part.part_id"],
            name=op.f("fk_daily_delivery_member_part_id_daily_delivery_part"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["daily_delivery_plan.plan_id"],
            name=op.f("fk_daily_delivery_member_plan_id_daily_delivery_plan"),
        ),
        sa.PrimaryKeyConstraint("member_id", name=op.f("pk_daily_delivery_member")),
        sa.UniqueConstraint("plan_id", "member_key", name="uq_daily_delivery_member_key"),
    )
    op.create_index(
        "idx_daily_delivery_member_part", "daily_delivery_member", ["part_id"], unique=False
    )
    op.create_table(
        "futures_contract_eod",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("instrument_id", sa.UUID(), nullable=False),
        sa.Column("contract_id", sa.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("session", sa.String(length=20), nullable=False),
        sa.Column("open", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("high", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("low", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("close", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("settlement_price", sa.NUMERIC(precision=20, scale=8), nullable=True),
        sa.Column("open_interest", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("source_priority", sa.Integer(), nullable=False),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asof_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "session IN ('regular', 'after_hours')",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_session_valid"),
        ),
        sa.CheckConstraint(
            "close IS NULL OR close >= 0",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_close_nonnegative"),
        ),
        sa.CheckConstraint(
            "high IS NULL OR high >= 0",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_high_nonnegative"),
        ),
        sa.CheckConstraint(
            "high IS NULL OR low IS NULL OR high >= low",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_bounds_valid"),
        ),
        sa.CheckConstraint(
            "low IS NULL OR low >= 0",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_low_nonnegative"),
        ),
        sa.CheckConstraint(
            "open IS NULL OR open >= 0",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_open_nonnegative"),
        ),
        sa.CheckConstraint(
            "open_interest IS NULL OR open_interest >= 0",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_oi_nonnegative"),
        ),
        sa.CheckConstraint(
            "settlement_price IS NULL OR settlement_price >= 0",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_settlement_nonnegative"),
        ),
        sa.CheckConstraint(
            "volume IS NULL OR volume >= 0",
            name=op.f("ck_futures_contract_eod_futures_contract_eod_volume_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["contract_id"],
            ["futures_contract.contract_id"],
            name=op.f("fk_futures_contract_eod_contract_id_futures_contract"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_futures_contract_eod_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_run.run_id"],
            name=op.f("fk_futures_contract_eod_run_id_ingestion_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_futures_contract_eod")),
        sa.UniqueConstraint(
            "contract_id", "trade_date", "session", name="uq_futures_contract_eod_slot"
        ),
    )
    op.create_index(
        "idx_futures_contract_eod_date",
        "futures_contract_eod",
        ["trade_date", "session"],
        unique=False,
    )
    op.create_index(
        "idx_futures_contract_eod_instrument",
        "futures_contract_eod",
        ["instrument_id", "trade_date"],
        unique=False,
    )
    op.add_column("ingestion_run", sa.Column("delivery_part_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_ingestion_run_delivery_part_id_daily_delivery_part",
        "ingestion_run",
        "daily_delivery_part",
        ["delivery_part_id"],
        ["part_id"],
    )
    op.add_column("instrument_stats", sa.Column("futures_first_date", sa.Date(), nullable=True))
    op.add_column("instrument_stats", sa.Column("futures_latest_date", sa.Date(), nullable=True))


def downgrade() -> None:
    # Retain registry/history on rollback; old runtimes see inactive feeds.
    op.execute(
        sa.text("""DELETE FROM scheduler_control WHERE scheduler_key IN
        ('full_market_twelve_data_v1','full_market_finlab_v1','full_market_shioaji_v1','full_market_taifex_v1')
    """)
    )
    op.execute(
        sa.text("""UPDATE dataset_registry SET is_active = false,
        config = config - 'full_market' WHERE dataset_key IN ('hk_equity_eod','tw_etf_eod','tw_futures_eod')
    """)
    )
    op.execute(
        sa.text("""UPDATE dataset_registry SET config = config - 'full_market'
        WHERE dataset_key IN ('us_equity_eod','tw_equity_eod','tw_equity_minute','tw_etf_minute')
    """)
    )
    op.drop_column("instrument_stats", "futures_latest_date")
    op.drop_column("instrument_stats", "futures_first_date")
    op.drop_constraint(
        "fk_ingestion_run_delivery_part_id_daily_delivery_part", "ingestion_run", type_="foreignkey"
    )
    op.drop_column("ingestion_run", "delivery_part_id")
    op.drop_index("idx_futures_contract_eod_instrument", table_name="futures_contract_eod")
    op.drop_index("idx_futures_contract_eod_date", table_name="futures_contract_eod")
    op.drop_table("futures_contract_eod")
    op.drop_index("idx_daily_delivery_member_part", table_name="daily_delivery_member")
    op.drop_table("daily_delivery_member")
    op.drop_table("daily_delivery_part")
    op.drop_index("idx_universe_member_symbol", table_name="universe_member")
    op.drop_table("universe_member")
    op.drop_index("idx_daily_delivery_plan_deadline", table_name="daily_delivery_plan")
    op.drop_table("daily_delivery_plan")
    op.drop_index("idx_universe_release_scope", table_name="universe_release")
    op.drop_table("universe_release")
