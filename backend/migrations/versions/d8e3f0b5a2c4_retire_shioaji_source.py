"""Retire Shioaji acquisition without deleting historical or canonical evidence.

Revision ID: d8e3f0b5a2c4
Revises: c7d2e9a4f1b3

Deploy only after Fetcher bundle v3 is accepted and all Shioaji controls are
stopped. Closed-calendar queued attempts are retained inert; all other unfinished
work or unreceipted prepared bytes block retirement. Downgrade never resurrects
credentials or acquisition scopes.
"""

import json
from copy import deepcopy

import sqlalchemy as sa
from alembic import op

revision = "d8e3f0b5a2c4"
down_revision = "c7d2e9a4f1b3"
branch_labels = None
depends_on = None

_UNSETTLED = {
    "controls": "SELECT count(*) FROM scheduler_control WHERE provider='shioaji' AND retired_at IS NULL "
    "AND (desired_state <> 'stopped' OR observed_state <> 'stopped')",
    "owners": "SELECT count(*) FROM provider_work_owner WHERE provider='shioaji' "
    "AND attempt_id IS NOT NULL",
    "work": "SELECT count(*) FROM provider_work WHERE provider='shioaji' "
    "AND state NOT IN ('completed','partial','failed','cancelled','calendar_closed')",
    "attempts": "SELECT count(*) FROM provider_work_attempt a JOIN provider_work w USING(work_id) "
    "WHERE w.provider='shioaji' AND a.state <> 'settled' "
    "AND NOT (w.state='calendar_closed' AND a.state='queued')",
    "prepared": "SELECT count(*) FROM provider_work_prepared p JOIN provider_work w USING(work_id) "
    "WHERE w.provider='shioaji' AND NOT EXISTS (SELECT 1 FROM provider_work_receipt r "
    "WHERE r.work_id=p.work_id AND r.member_key=p.delivery_key)",
    "historical": "SELECT count(*) FROM historical_backfill_request WHERE provider='shioaji' "
    "AND status IN ('queued','running')",
    "runs": "SELECT count(*) FROM ingestion_run WHERE source='shioaji' "
    "AND status NOT IN ('completed','completed_with_errors','failed','cancelled')",
}


def _retire_scope(config: dict) -> dict:
    result = deepcopy(config)
    scopes = [(result, "allowed_sources", None)] + [
        (result.get("delivery_expectation", {}).get(name, {}), "expected_sources", name)
        for name in ("schedule", "missing_delivery")
    ]
    for scope, key, name in scopes:
        if key not in scope:
            continue
        if not isinstance(scope[key], list):
            raise RuntimeError("Shioaji retirement requires valid provider scope arrays")
        if "shioaji" not in scope[key]:
            continue
        scope[key] = [source for source in scope[key] if source != "shioaji"]
        if not scope[key] and name == "schedule":
            scope["enabled"] = False
        if not scope[key] and name == "missing_delivery":
            scope["action"] = "disabled"
    return result


def upgrade() -> None:
    bind = op.get_bind()
    blocking = {
        name: count for name, query in _UNSETTLED.items() if (count := bind.scalar(sa.text(query)))
    }
    if blocking:
        raise RuntimeError(f"Shioaji has unsettled evidence: {blocking}")
    # Keep registry activation, source precedence, snapshots, and first dates:
    # Serve and archive overlap resolution still need them.
    for row in bind.execute(sa.text("SELECT dataset_key, config FROM dataset_registry")).mappings():
        config = row["config"]
        if not isinstance(config, dict):
            continue
        retired = _retire_scope(config)
        if retired != config:
            bind.execute(
                sa.text(
                    "UPDATE dataset_registry SET config=CAST(:config AS jsonb) WHERE dataset_key=:key"
                ),
                {"config": json.dumps(retired), "key": row["dataset_key"]},
            )
    op.execute(
        sa.text(
            "UPDATE source_client SET revoked_at=now(), updated_at=now() WHERE source_name='shioaji' AND revoked_at IS NULL"
        )
    )
    # Preserve scheduler_dataset bindings for historical interpretation.
    op.execute(
        sa.text(
            "UPDATE scheduler_control SET retired_at=now(), updated_at=now(), revision=revision+1 WHERE provider='shioaji' AND retired_at IS NULL"
        )
    )
    op.execute(
        sa.text("""
        UPDATE missing_delivery_alert
        SET status='resolved', resolved_at=now(), details=coalesce(details,'{}'::jsonb) ||
            jsonb_build_object('resolution',jsonb_build_object('code','FEED_RETIRED','execution_profile',execution_profile))
        WHERE source='shioaji' AND status='open'
    """)
    )


def downgrade() -> None:
    """Retirement is irreversible; a replacement provider needs a new migration."""
