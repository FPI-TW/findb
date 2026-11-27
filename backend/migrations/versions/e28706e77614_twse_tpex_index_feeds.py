"""Install the TWSE and TPEx index feeds: two datasets, two stopped controls, and schedule inspection.

Revision ID: e28706e77614
Revises: e0649188f071

The provider catalog now lists two official Taiwan providers, each with one
``market_eod`` v2 index feed: ``twse`` with ``tw_twse_index_eod`` (``^TWII``) and
``tpex`` with ``tw_tpex_index_eod`` (``^TWOII``), both 14:15 Taipei time for the
same date. One dataset cannot have two providers (the seed's one-Source-feed
rule, schedule projection, per-dataset first start date), so each exchange has
its own; Serve already lets several datasets share the ``TW`` ``index`` scope.
Registry provisioning requires the dataset rows, and starting a scheduler
requires its control and datasets, so they are created here, the controls
stopped. Nothing is acquired until an operator publishes each first universe
baseline and starts the control.

Frozen migration inputs: never import the live catalog or seed. This revision
replaces 7733f9692fe6 as the schedule inspection used by the deploy preflight
and seed: its targets are that revision's plus the two feeds and the two
controls, whose clock is their only feed's (14:15 Taipei). Every other row is
judged by 7733f9692fe6's own frozen rules.

Downgrade removes what this revision added and refuses once a dataset has been
used or a control started, because their universes, plans, runs, rows and
admission proofs cannot be removed by a schema rollback.
"""

import functools
import importlib.util
import json
from copy import deepcopy
from datetime import time
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "e28706e77614"
down_revision = "e0649188f071"
branch_labels = None
depends_on = None

ZONE = "Asia/Taipei"
CLOCK = "14:15:00"
# dataset: provider, timezone, target clock, known old states, monitor clock, day offset
NEW_FEEDS = {
    "tw_twse_index_eod": ("twse", ZONE, CLOCK, (), None, 0),
    "tw_tpex_index_eod": ("tpex", ZONE, CLOCK, (), None, 0),
}
TARGET_DATE_LAGS = {"tw_twse_index_eod": 0, "tw_tpex_index_eod": 0}
# provider: timezone, target clock, known old (timezone, clock) states.
NEW_CONTROLS = {"twse": (ZONE, CLOCK, ()), "tpex": (ZONE, CLOCK, ())}
SCHEDULER_KEYS = {provider: f"full_market_{provider}_v1" for provider in NEW_CONTROLS}
CONTROL_SLOT = "taiwan_market_window"
NAMES = {"tw_twse_index_eod": "加權指數日K", "tw_tpex_index_eod": "櫃買指數日K"}
# Tables that hold rows of a dataset once it has been used.
USAGE_TABLES = (
    "universe_release",
    "daily_delivery_plan",
    "daily_execution_decision",
    "full_market_admission_feed",
    "full_market_dataset_state",
    "ingestion_run",
    "provider_work",
    "historical_backfill_request",
)


def _config(provider: str) -> dict:
    """The seed's declaration at this revision, frozen."""
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [2],
        "current_schema_version": 2,
        "schema_enforcement": "enforce",
        "allowed_sources": [provider],
        "defaults": {"market": "TW", "asset_class": "index", "currency": "TWD"},
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
                "timezone": ZONE,
                "market_close_time": "13:30:00",
                "availability_grace_minutes": 120,
                "action": "warn",
            },
            "missing_delivery": {"action": "disabled", "expected_sources": []},
            "schedule": {
                "enabled": False,
                "slot_id": CONTROL_SLOT,
                "local_time": CLOCK,
                "timezone": ZONE,
                "target_date_lag_days": 0,
                "expected_sources": [provider],
            },
        },
        "full_market": {
            "enabled": False,
            "required": True,
            "activation_date": None,
            "calendar_market": "TW",
        },
        "source_precedence": [provider],
    }


CONFIGS = {key: _config(feed[0]) for key, feed in NEW_FEEDS.items()}
DESCRIPTIONS = {
    key: f"Staged {feed[0]} market_eod.v2 full-market daily feed" for key, feed in NEW_FEEDS.items()
}


@functools.cache
def _previous_revision():
    """The frozen 7733f9692fe6 rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("7733f9692fe6_finlab_daily_1930_schedule.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_finlab_1930", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The complete targets this revision governs, for callers that read them.
FEEDS = {**_previous_revision().FEEDS, **NEW_FEEDS}
CONTROLS = {**_previous_revision().CONTROLS, **NEW_CONTROLS}


def _project_new(key: str, config: dict) -> tuple[str, dict]:
    """The index feeds have no older shape: only their own targets are compliant."""
    provider, zone, clock, _, _, _ = NEW_FEEDS[key]
    try:
        expectation = config["delivery_expectation"]
        schedule = expectation["schedule"]
        fields = ("timezone", "local_time", "target_date_lag_days", "expected_sources")
        current = tuple(schedule.get(field) for field in fields)
        if type(schedule.get("target_date_lag_days")) is not int or current != (
            zone,
            clock,
            TARGET_DATE_LAGS[key],
            [provider],
        ):
            return "unknown_difference", config
        monitor = expectation["missing_delivery"]
        if any(
            monitor.get(field) is not None
            for field in ("deadline_local_time", "deadline_timezone", "deadline_day_offset")
        ):
            return "unknown_difference", config
    except (KeyError, TypeError, AttributeError):
        return "unknown_difference", config
    return "already_compliant", deepcopy(config)


def project_config(key: str, config: dict, *, reverse: bool = False) -> tuple[str, dict]:
    if key in NEW_FEEDS:
        return _project_new(key, config)
    return _previous_revision().project_config(key, config, reverse=reverse)


def inspect(
    connection, *, lock: bool = False, reverse: bool = False
) -> tuple[list[dict], list[dict], list[dict]]:
    """7733f9692fe6's report, with the two index datasets and controls judged here."""
    report, dataset_updates, control_updates = _previous_revision().inspect(
        connection, lock=lock, reverse=reverse
    )
    controls = {
        row["scheduler_key"]: row
        for row in connection.execute(
            sa.text(
                "SELECT scheduler_key, provider, execution_profile, scheduled_local_time, timezone "
                "FROM scheduler_control WHERE retired_at IS NULL AND provider IN :providers"
            ).bindparams(sa.bindparam("providers", expanding=True)),
            {"providers": sorted(NEW_CONTROLS)},
        ).mappings()
    }
    datasets = {
        row["dataset_key"]: row["config"]
        for row in connection.execute(
            sa.text(
                "SELECT dataset_key, config FROM dataset_registry "
                "WHERE dataset_key IN :keys ORDER BY dataset_key"
            ).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": sorted(NEW_FEEDS)},
        ).mappings()
    }
    for row in report:
        if row["kind"] == "control" and row["key"] in controls:
            control = controls[row["key"]]
            zone, clock, _ = NEW_CONTROLS[control["provider"]]
            current = (control["timezone"], str(control["scheduled_local_time"]))
            row["status"] = (
                "already_compliant"
                if control["scheduler_key"] == SCHEDULER_KEYS[control["provider"]]
                and control["execution_profile"] == "full_market"
                and current == (zone, clock)
                else "unknown_difference"
            )
    for key, config in datasets.items():
        status, _ = _project_new(key, config)
        report.append({"kind": "dataset", "key": key, "status": status})
    return report, dataset_updates, control_updates


def _require_compliant(connection) -> None:
    report, _, _ = inspect(connection, lock=True)
    unknown = [row["key"] for row in report if row["status"] == "unknown_difference"]
    if unknown:
        raise RuntimeError("Catalog schedule preflight unknown differences: " + ", ".join(unknown))


def upgrade() -> None:
    bind = op.get_bind()
    for key, config in CONFIGS.items():
        bind.execute(
            sa.text(
                "INSERT INTO dataset_registry (dataset_key, name, description, asset_class, "
                "market, frequency, is_active, config, created_at, updated_at) "
                "VALUES (:key, :name, :description, 'index', 'TW', 'daily', true, "
                "CAST(:config AS jsonb), now(), now()) ON CONFLICT (dataset_key) DO NOTHING"
            ),
            {
                "key": key,
                "name": NAMES[key],
                "description": DESCRIPTIONS[key],
                "config": json.dumps(config),
            },
        )
    for provider, (zone, clock, _) in NEW_CONTROLS.items():
        bind.execute(
            sa.text(
                "INSERT INTO scheduler_control (scheduler_key, provider, execution_profile, "
                "slot_id, scheduled_local_time, timezone, desired_state, observed_state, "
                "revision, created_at, updated_at) "
                "VALUES (:key, :provider, 'full_market', :slot, CAST(:clock AS time), :zone, "
                "'stopped', 'stopped', 1, now(), now()) ON CONFLICT (scheduler_key) DO NOTHING"
            ),
            {
                "key": SCHEDULER_KEYS[provider],
                "provider": provider,
                "slot": CONTROL_SLOT,
                "clock": time.fromisoformat(clock),
                "zone": zone,
            },
        )
    for key, feed in NEW_FEEDS.items():
        bind.execute(
            sa.text(
                "INSERT INTO scheduler_dataset (scheduler_key, dataset_key) "
                "VALUES (:scheduler, :key) ON CONFLICT DO NOTHING"
            ),
            {"scheduler": SCHEDULER_KEYS[feed[0]], "key": key},
        )
    # A row this revision did not write (ON CONFLICT) must still meet the targets.
    _require_compliant(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for key in sorted(NEW_FEEDS):
        for table in USAGE_TABLES:
            used = bind.execute(
                sa.text(f"SELECT 1 FROM {table} WHERE dataset_key = :key LIMIT 1"), {"key": key}
            ).first()
            if used:
                raise RuntimeError(
                    f"TW index dataset {key} has been used; downgrade is blocked (rows in {table})"
                )
    schedulers = sorted(SCHEDULER_KEYS.values())
    # A started control keeps its admission proofs.
    admitted = bind.execute(
        sa.text(
            "SELECT 1 FROM full_market_admission WHERE scheduler_key IN :schedulers LIMIT 1"
        ).bindparams(sa.bindparam("schedulers", expanding=True)),
        {"schedulers": schedulers},
    ).first()
    if admitted:
        raise RuntimeError("TWSE or TPEx control has been started; downgrade is blocked")
    for statement in (
        "DELETE FROM scheduler_dataset WHERE scheduler_key IN :keys",
        "DELETE FROM scheduler_control WHERE scheduler_key IN :keys",
    ):
        bind.execute(
            sa.text(statement).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": schedulers},
        )
    bind.execute(
        sa.text("DELETE FROM dataset_registry WHERE dataset_key IN :keys").bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": sorted(NEW_FEEDS)},
    )
