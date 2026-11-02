"""Permanently retire the Twelve Data HK scope; other HK sources are unaffected.

Revision ID: c7d2e9a4f1b3
Revises: 9c1e4f7a2b6d

The Twelve Data plan has no HK entitlement. This data-only revision removes the
pair from scheduler, active credential and registry scope, and closes its open
missing-delivery alerts: the monitor iterates registry scope, so it would never
reach them again. Unsettled work stops the migration instead of being settled here.

Raw, runs, plans, work, universe releases, canonical rows and revoked credentials
stay untouched as evidence. The downgrade is intentionally a no-op: reopening the
scope would permit a pair that can never acquire and revive alerts that cannot close.
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d2e9a4f1b3"
down_revision = "9c1e4f7a2b6d"
branch_labels = None
depends_on = None

_PAIR_WORK = (
    "FROM provider_work w WHERE w.provider = 'twelve_data' AND w.dataset_key = 'hk_equity_eod'"
)
_UNSETTLED = {
    "work": f"SELECT count(*) {_PAIR_WORK} "
    "AND w.state NOT IN ('completed', 'partial', 'failed', 'cancelled')",
    "attempts": "SELECT count(*) FROM provider_work_attempt a "
    f"WHERE a.state <> 'settled' AND a.work_id IN (SELECT w.work_id {_PAIR_WORK})",
    "prepared": "SELECT count(*) FROM provider_work_prepared p "
    f"WHERE p.work_id IN (SELECT w.work_id {_PAIR_WORK}) AND NOT EXISTS ("
    "SELECT 1 FROM provider_work_receipt r "
    "WHERE r.work_id = p.work_id AND r.member_key = p.delivery_key)",
    "historical": "SELECT count(*) FROM historical_backfill_request "
    "WHERE provider = 'twelve_data' AND dataset_key = 'hk_equity_eod' "
    "AND status IN ('queued', 'running')",
    "runs": "SELECT count(*) FROM ingestion_run "
    "WHERE source = 'twelve_data' AND dataset_key = 'hk_equity_eod' "
    "AND status NOT IN ('completed', 'completed_with_errors', 'failed', 'cancelled')",
}


def upgrade() -> None:
    bind = op.get_bind()
    unsettled = {name: bind.scalar(sa.text(query)) for name, query in _UNSETTLED.items()}
    blocking = {name: count for name, count in unsettled.items() if count}
    if blocking:
        raise RuntimeError(f"Twelve Data HK has unsettled evidence: {blocking}")
    op.execute(
        sa.text(
            """
            DELETE FROM scheduler_dataset
            WHERE dataset_key = 'hk_equity_eod'
              AND scheduler_key IN (
                SELECT scheduler_key FROM scheduler_control WHERE provider = 'twelve_data'
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE source_client
            SET allowed_datasets = allowed_datasets - 'hk_equity_eod'
            WHERE source_name = 'twelve_data'
              AND revoked_at IS NULL
              AND allowed_datasets @> '["hk_equity_eod"]'::jsonb
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE dataset_registry
            SET config = jsonb_set(
                config, '{allowed_sources}', (config -> 'allowed_sources') - 'twelve_data'
            )
            WHERE dataset_key = 'hk_equity_eod'
              AND config -> 'allowed_sources' @> '["twelve_data"]'::jsonb
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE missing_delivery_alert
            SET status = 'resolved',
                resolved_at = now(),
                details = coalesce(details, '{}'::jsonb) || jsonb_build_object(
                    'resolution',
                    jsonb_build_object(
                        'code', 'FEED_RETIRED', 'execution_profile', execution_profile
                    )
                )
            WHERE source = 'twelve_data'
              AND dataset_key = 'hk_equity_eod'
              AND status = 'open'
            """
        )
    )


def downgrade() -> None:
    """Keep the retirement; see the module docstring."""
