"""Freeze daily execution decisions and make calendars optional.

Revision ID: 91a4c7e2d630
Revises: 6b93ef20a147
"""

import hashlib
import json
from datetime import time

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "91a4c7e2d630"
down_revision = "6b93ef20a147"
branch_labels = None
depends_on = None

_MARKETS = {
    "TW": ("Asia/Taipei", time(9), time(13, 30)),
    "US": ("America/New_York", time(9, 30), time(16)),
    "HK": ("Asia/Hong_Kong", time(9, 30), time(16, 10)),
    "CN": ("Asia/Shanghai", time(9, 30), time(15)),
    "TAIFEX": ("Asia/Taipei", time(8, 45), time(13, 45)),
}


def _snapshot(connection, market, day):
    zone, normal_open, normal_close = _MARKETS[market]
    row = (
        connection.execute(
            sa.text("""
        SELECT d.*, r.revision, r.id AS revision_id
        FROM calendar_revision_day d JOIN calendar_year_revision r
          ON d.calendar_revision_id=r.id
        WHERE r.market=:market AND r.status='published' AND d.trade_date=:day
        ORDER BY r.revision DESC LIMIT 1
    """),
            {"market": market, "day": day},
        )
        .mappings()
        .first()
    )
    is_open = row["is_open"] if row else day.weekday() < 5
    close = (row["session_close"] if row and is_open else None) or normal_close
    opening = (row["session_open"] if row and is_open else None) or normal_open
    policy = {"version": 1, "skip_non_trading_days": False}
    policy["sha256"] = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "market": market,
        "trade_date": day.isoformat(),
        "timezone": zone,
        "is_open": is_open,
        "day_status": row["day_status"] if row else "open" if is_open else "closed",
        "session_open": opening.isoformat() if is_open else None,
        "session_close": close.isoformat() if is_open else None,
        "regular_close": close.isoformat(),
        "after_hours_close": "05:00:00" if market == "TAIFEX" else None,
        "holiday_name": row["holiday_name"] if row else None,
        "description": row["description"] if row else None,
        "calendar_source": "published" if row else "weekday_default",
        "calendar_revision": row["revision"] if row else None,
        "calendar_revision_id": str(row["revision_id"]) if row else None,
        "snapshot_origin": "legacy_migration_snapshot",
        "policy": policy,
    }


def upgrade():
    op.alter_column("full_market_admission_feed", "calendar_revision_id", nullable=True)
    table = op.create_table(
        "daily_execution_decision",
        sa.Column(
            "dataset_key",
            sa.String(50),
            sa.ForeignKey("dataset_registry.dataset_key"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(50), primary_key=True),
        sa.Column("trade_date", sa.Date(), primary_key=True),
        sa.Column("execution_profile", sa.String(20), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("daily_delivery_plan.plan_id"),
            nullable=True,
            unique=True,
        ),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("execution_profile IN ('pilot','full_market')", name="decision_profile"),
        sa.CheckConstraint(
            "(kind='planned' AND plan_id IS NOT NULL) OR (kind='skipped_calendar' AND plan_id IS NULL)",
            name="decision_kind_plan",
        ),
    )
    op.add_column(
        "historical_backfill_item",
        sa.Column("calendar_snapshot", postgresql.JSONB(), nullable=True),
    )
    op.drop_constraint(
        op.f("ck_historical_backfill_item_historical_backfill_item_status_valid"),
        "historical_backfill_item",
        type_="check",
    )
    op.create_check_constraint(
        "historical_backfill_item_status_valid",
        "historical_backfill_item",
        "status IN ('pending','running','completed','failed','cancelled','skipped_calendar')",
    )
    connection = op.get_bind()
    plans = (
        connection.execute(
            sa.text("""
        SELECT p.*, CASE WHEN p.dataset_key='tw_futures_eod' THEN 'TAIFEX' ELSE d.market END AS market
        FROM daily_delivery_plan p JOIN dataset_registry d USING (dataset_key)
        ORDER BY p.plan_id
    """)
        )
        .mappings()
        .all()
    )
    for plan in plans:
        connection.execute(
            table.insert().values(
                **{
                    key: plan[key]
                    for key in (
                        "dataset_key",
                        "provider",
                        "trade_date",
                        "execution_profile",
                        "plan_id",
                        "created_at",
                    )
                },
                kind="planned",
                snapshot=_snapshot(connection, plan["market"], plan["trade_date"]),
            )
        )
    items = (
        connection.execute(
            sa.text("""
        SELECT i.item_id,i.trade_date,r.market FROM historical_backfill_item i
        JOIN historical_backfill_request r USING(request_id) ORDER BY i.item_id
    """)
        )
        .mappings()
        .all()
    )
    update = sa.text(
        "UPDATE historical_backfill_item SET calendar_snapshot=:snapshot WHERE item_id=:id"
    ).bindparams(sa.bindparam("snapshot", type_=postgresql.JSONB()))
    for item in items:
        connection.execute(
            update,
            {
                "id": item["item_id"],
                "snapshot": _snapshot(connection, item["market"], item["trade_date"]),
            },
        )


def downgrade():
    # Old code would reinterpret persisted skips and frozen timing. Restore the
    # coordinated pre-cutover backup instead of opening upgraded state to it.
    raise RuntimeError(
        "Effective calendar decisions set a new rollback floor; coordinated backup restoration is required"
    )
