"""Project catalog v1 slots onto delivery expectations and scheduler controls.

Revision ID: c3f6a9d1e2b7
Revises: b5d8e2f4a6c1

A slot is only the Dashboard regional grouping of a feed, derived from the
catalog; contract enums, clocks, frozen plans and work are unchanged. Frozen
inputs: never import the live catalog. Retired controls keep their slot.
Downgrade requires a forward fix.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "c3f6a9d1e2b7"
down_revision = "b5d8e2f4a6c1"
branch_labels = None
depends_on = None

DATASET_SLOTS = {
    "us_equity_eod": "western_markets_window",
    "hk_equity_eod": "asia_pacific_markets_window",
    "tw_equity_eod": "taiwan_market_window",
    "tw_etf_eod": "taiwan_market_window",
    "tw_futures_eod": "taiwan_market_window",
}
CONTROL_SLOTS = {
    "full_market_twelve_data_v1": "western_markets_window",
    "full_market_factset_v1": "asia_pacific_markets_window",
    "full_market_finlab_v1": "taiwan_market_window",
    "full_market_taifex_v1": "taiwan_market_window",
}


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT dataset_key, config FROM dataset_registry WHERE dataset_key = ANY(:keys)"),
        {"keys": list(DATASET_SLOTS)},
    ).all()
    for dataset_key, config in rows:
        config = config if isinstance(config, dict) else json.loads(config or "{}")
        schedule = (config.get("delivery_expectation") or {}).get("schedule")
        if not isinstance(schedule, dict) or schedule.get("slot_id") in (
            None,
            DATASET_SLOTS[dataset_key],
        ):
            continue
        schedule["slot_id"] = DATASET_SLOTS[dataset_key]
        bind.execute(
            sa.text(
                "UPDATE dataset_registry SET config = CAST(:config AS JSONB) WHERE dataset_key = :key"
            ),
            {"config": json.dumps(config), "key": dataset_key},
        )
    for scheduler_key, slot_id in CONTROL_SLOTS.items():
        bind.execute(
            sa.text(
                "UPDATE scheduler_control SET slot_id = :slot "
                "WHERE scheduler_key = :key AND retired_at IS NULL AND slot_id <> :slot"
            ),
            {"slot": slot_id, "key": scheduler_key},
        )


def downgrade() -> None:
    raise RuntimeError("Catalog slot projection requires a forward fix")
