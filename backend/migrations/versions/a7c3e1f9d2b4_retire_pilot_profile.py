"""Retire the Pilot execution profile and delete its workflow evidence.

Revision ID: a7c3e1f9d2b4
Revises: e9f4a1c6b3d5

Deploy only after every ``pilot_*`` control is stopped and its daily work is
settled. Pilot plans, work, admissions, controls and alerts are deleted;
ingestion runs, raw payloads and canonical rows written by Pilot are retained
and detached from the deleted plan parts. Downgrade cannot restore the deleted
rows and therefore refuses to run.
"""

import sqlalchemy as sa
from alembic import op

revision = "a7c3e1f9d2b4"
down_revision = "e9f4a1c6b3d5"
branch_labels = None
depends_on = None

_PILOT_CONTROLS = "scheduler_key LIKE 'pilot\\_%\\_v1' ESCAPE '\\'"
_PILOT_WORK = "SELECT work_id FROM provider_work WHERE execution_profile='pilot'"
_PILOT_PLANS = "SELECT plan_id FROM daily_delivery_plan WHERE execution_profile='pilot'"
_PILOT_PARTS = f"SELECT part_id FROM daily_delivery_part WHERE plan_id IN ({_PILOT_PLANS})"
_PILOT_RUNS = f"SELECT run_id FROM ingestion_run WHERE delivery_part_id IN ({_PILOT_PARTS})"
_PILOT_ADMISSIONS = f"SELECT admission_id FROM full_market_admission WHERE {_PILOT_CONTROLS}"

_UNSETTLED = {
    "controls": f"SELECT count(*) FROM scheduler_control WHERE {_PILOT_CONTROLS} "
    "AND (desired_state <> 'stopped' OR observed_state <> 'stopped')",
    "owners": "SELECT count(*) FROM provider_work_owner o JOIN provider_work_attempt a "
    "USING(attempt_id) JOIN provider_work w ON w.work_id=a.work_id "
    "WHERE w.execution_profile='pilot'",
    "work": "SELECT count(*) FROM provider_work WHERE execution_profile='pilot' "
    "AND state NOT IN ('completed','partial','failed','cancelled','calendar_closed')",
    "attempts": "SELECT count(*) FROM provider_work_attempt a JOIN provider_work w USING(work_id) "
    "WHERE w.execution_profile='pilot' AND a.state <> 'settled' "
    "AND NOT (w.state='calendar_closed' AND a.state='queued')",
    "prepared": "SELECT count(*) FROM provider_work_prepared p JOIN provider_work w USING(work_id) "
    "WHERE w.execution_profile='pilot' AND NOT EXISTS (SELECT 1 FROM provider_work_receipt r "
    "WHERE r.work_id=p.work_id AND r.member_key=p.delivery_key)",
    "runs": f"SELECT count(*) FROM ingestion_run WHERE run_id IN ({_PILOT_RUNS}) "
    "AND status NOT IN ('completed','completed_with_errors','failed','cancelled')",
}

# Children before parents; every foreign key on these tables is RESTRICT except
# daily_delivery_plan_summary and scheduler_dataset, which cascade.
_DELETES = (
    f"DELETE FROM delivery_member_evidence WHERE run_id IN ({_PILOT_RUNS})",
    f"UPDATE ingestion_run SET delivery_part_id=NULL WHERE delivery_part_id IN ({_PILOT_PARTS})",
    "DELETE FROM shioaji_repair_ack WHERE repair_id IN (SELECT repair_id FROM "
    f"shioaji_member_repair WHERE work_id IN ({_PILOT_WORK}) OR plan_id IN ({_PILOT_PLANS}))",
    f"DELETE FROM shioaji_member_repair WHERE work_id IN ({_PILOT_WORK}) "
    f"OR plan_id IN ({_PILOT_PLANS})",
    f"DELETE FROM provider_work_calendar_closure WHERE work_id IN ({_PILOT_WORK}) "
    f"OR plan_id IN ({_PILOT_PLANS})",
    f"DELETE FROM provider_work_receipt WHERE work_id IN ({_PILOT_WORK})",
    f"DELETE FROM provider_work_prepared WHERE work_id IN ({_PILOT_WORK})",
    f"DELETE FROM provider_work_attempt WHERE work_id IN ({_PILOT_WORK})",
    "DELETE FROM provider_work WHERE execution_profile='pilot'",
    f"DELETE FROM daily_delivery_member WHERE plan_id IN ({_PILOT_PLANS})",
    f"DELETE FROM daily_delivery_part WHERE plan_id IN ({_PILOT_PLANS})",
    "DELETE FROM daily_execution_decision WHERE execution_profile='pilot' "
    f"OR plan_id IN ({_PILOT_PLANS})",
    "DELETE FROM daily_delivery_plan WHERE execution_profile='pilot'",
    "DELETE FROM full_market_dataset_state WHERE execution_profile='pilot'",
    f"DELETE FROM full_market_admission_feed WHERE admission_id IN ({_PILOT_ADMISSIONS})",
    f"DELETE FROM full_market_admission WHERE {_PILOT_CONTROLS}",
    f"DELETE FROM scheduler_control WHERE {_PILOT_CONTROLS}",
    # Pilot plan alerts go with their plans.
    "DELETE FROM missing_delivery_alert WHERE execution_profile='pilot' "
    "AND (details->>'code'='PILOT_INCOMPLETE' OR details->>'execution_profile'='pilot')",
    # Legacy alerts were only labelled pilot by the old server default. Keep them
    # as full_market unless that identity already exists.
    """DELETE FROM missing_delivery_alert p WHERE p.execution_profile='pilot' AND EXISTS (
        SELECT 1 FROM missing_delivery_alert f WHERE f.execution_profile='full_market'
        AND f.dataset_key=p.dataset_key AND f.source=p.source AND f.schema_id=p.schema_id
        AND f.schema_version=p.schema_version AND f.expected_data_date=p.expected_data_date)""",
    "UPDATE missing_delivery_alert SET execution_profile='full_market' "
    "WHERE execution_profile='pilot'",
    # Long-retired legacy controls carried the old default without any meaning.
    "UPDATE scheduler_control SET execution_profile='full_market' WHERE execution_profile='pilot'",
)

_TIGHTENED = (
    ("full_market_dataset_state", "dataset_state_profile"),
    ("daily_delivery_plan", "delivery_plan_profile"),
    ("daily_execution_decision", "decision_profile"),
)
_ADDED = (
    ("scheduler_control", "scheduler_control_profile", "execution_profile = 'full_market'"),
    ("missing_delivery_alert", "missing_delivery_profile", "execution_profile = 'full_market'"),
    (
        "provider_work",
        "provider_work_profile",
        "execution_profile IS NULL OR execution_profile = 'full_market'",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    for name, statement in _UNSETTLED.items():
        if bind.scalar(sa.text(statement)):
            raise RuntimeError(f"Pilot retirement blocked by unsettled {name}")
    for statement in _DELETES:
        bind.execute(sa.text(statement))
    for table, name in _TIGHTENED:
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
        op.create_check_constraint(name, table, "execution_profile = 'full_market'")
    for table, name, condition in _ADDED:
        op.create_check_constraint(name, table, condition)
    for table in ("scheduler_control", "missing_delivery_alert"):
        op.alter_column(table, "execution_profile", server_default="full_market")


def downgrade() -> None:
    raise RuntimeError("Pilot retirement deleted its workflow rows and cannot be downgraded")
