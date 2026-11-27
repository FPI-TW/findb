"""Move FinLab's intraday feeds to their own lane (#691 step 3b).

Revision ID: e5979e281ba9
Revises: 36bfabaae072

The catalog now declares ``lane: intraday`` on FinLab's four intraday feeds, so a
4-6 hour intraday round no longer holds FinLab's daily EOD. Their unfinished work
moves from ``default`` to ``intraday``; finished work moves too, so every work of
a dataset sits in the lane the catalog gives it. Only work no consumer holds may
move: a running or recovering work, or one the default owner's attempt points at,
belongs to a live default-lane round, so the upgrade refuses before any change.
Unfinished work that already ran a round is refused too: its member progress
lives in the default consumer's Full checkpoint (``state-finlab.sqlite3``), which
the lane consumer never reads. The lane consumer would rebuild the plan from its
own empty checkpoint, deliver the finished members again and report their
``no_data`` outcomes anew, which Source rejects as immutable evidence; staging
saw both 11/19 intraday works fail this way. Such work must finish (or reach a
terminal state) under the default consumer before the upgrade.

``scheduler_control_lane`` keeps each non-default lane consumer's poll report, so
a lane consumer no longer overwrites the default consumer's heartbeat on the
shared control row.

Downgrade moves the work back only while the intraday owner was never used and
none of that work is running or recovering; otherwise folding two FIFOs back into
one cannot be done safely by a schema rollback.
"""

import sqlalchemy as sa
from alembic import op

revision = "e5979e281ba9"
down_revision = "36bfabaae072"
branch_labels = None
depends_on = None

PROVIDER = "finlab"
LANE = "intraday"
DATASETS = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")
LANE_CHECK = "lane ~ '^[a-z][a-z0-9_]{0,29}$' AND lane <> 'default'"
OBSERVED_CHECK = "observed_state IN ('running', 'stopped')"

_DATASETS = sa.bindparam("datasets", value=list(DATASETS), expanding=True)


def _held_work(bind: sa.engine.Connection, lane: str) -> bool:
    """Whether a work of the four datasets in ``lane`` is held by a consumer."""
    params = {"provider": PROVIDER, "lane": lane}
    running = bind.execute(
        sa.text(
            "SELECT 1 FROM provider_work WHERE provider = :provider AND lane = :lane "
            "AND dataset_key IN :datasets AND state IN ('running', 'recovery_blocked') LIMIT 1"
        ).bindparams(_DATASETS),
        params,
    ).first()
    owned = bind.execute(
        sa.text(
            "SELECT 1 FROM provider_work_owner o "
            "JOIN provider_work_attempt a ON a.attempt_id = o.attempt_id "
            "JOIN provider_work w ON w.work_id = a.work_id "
            "WHERE o.provider = :provider AND o.lane = :lane "
            "AND w.lane = :lane AND w.dataset_key IN :datasets LIMIT 1"
        ).bindparams(_DATASETS),
        params,
    ).first()
    return running is not None or owned is not None


def _started_work(bind: sa.engine.Connection) -> bool:
    """Whether unfinished default-lane work of the four datasets already ran a round."""
    return (
        bind.execute(
            sa.text(
                "SELECT 1 FROM provider_work w WHERE w.provider = :provider "
                "AND w.lane = 'default' AND w.dataset_key IN :datasets "
                "AND w.state NOT IN ('completed', 'partial', 'failed', 'cancelled') AND ("
                "EXISTS (SELECT 1 FROM provider_work_attempt a "
                "WHERE a.work_id = w.work_id AND a.state <> 'queued') "
                "OR EXISTS (SELECT 1 FROM provider_work_prepared p WHERE p.work_id = w.work_id) "
                "OR EXISTS (SELECT 1 FROM provider_work_receipt r WHERE r.work_id = w.work_id)"
                ") LIMIT 1"
            ).bindparams(_DATASETS),
            {"provider": PROVIDER},
        ).first()
        is not None
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _held_work(bind, "default"):
        raise RuntimeError(
            "FinLab intraday work is running or owned in the default lane; "
            "stop the FinLab control and let the round settle before this upgrade"
        )
    if _started_work(bind):
        raise RuntimeError(
            "unfinished FinLab intraday work already ran a round in the default lane; "
            "its progress is in the default consumer's checkpoint, so let it finish "
            "before this upgrade"
        )
    bind.execute(
        sa.text(
            "UPDATE provider_work SET lane = :lane WHERE provider = :provider "
            "AND lane = 'default' AND dataset_key IN :datasets"
        ).bindparams(_DATASETS),
        {"provider": PROVIDER, "lane": LANE},
    )
    op.create_table(
        "scheduler_control_lane",
        sa.Column(
            "scheduler_key",
            sa.String(100),
            sa.ForeignKey(
                "scheduler_control.scheduler_key",
                ondelete="CASCADE",
                name=op.f("fk_scheduler_control_lane_scheduler_key_scheduler_control"),
            ),
            nullable=False,
        ),
        sa.Column("lane", sa.String(30), nullable=False),
        sa.Column("observed_state", sa.String(20), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_cycle_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scheduler_key", "lane", name=op.f("pk_scheduler_control_lane")),
        sa.CheckConstraint(LANE_CHECK, name=op.f("ck_scheduler_control_lane_lane_valid")),
        sa.CheckConstraint(
            OBSERVED_CHECK, name=op.f("ck_scheduler_control_lane_observed_state_valid")
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    used = bind.execute(
        sa.text(
            "SELECT 1 FROM provider_work_owner WHERE provider = :provider "
            "AND lane = :lane AND (generation > 0 OR attempt_id IS NOT NULL) LIMIT 1"
        ),
        {"provider": PROVIDER, "lane": LANE},
    ).first()
    if used is not None or _held_work(bind, LANE):
        raise RuntimeError("the FinLab intraday lane was used; downgrade is blocked")
    op.drop_table("scheduler_control_lane")
    bind.execute(
        sa.text(
            "UPDATE provider_work SET lane = 'default' WHERE provider = :provider "
            "AND lane = :lane AND dataset_key IN :datasets"
        ).bindparams(_DATASETS),
        {"provider": PROVIDER, "lane": LANE},
    )
    bind.execute(
        sa.text("DELETE FROM provider_work_owner WHERE provider = :provider AND lane = :lane"),
        {"provider": PROVIDER, "lane": LANE},
    )
