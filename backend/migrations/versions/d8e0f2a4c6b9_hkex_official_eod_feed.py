"""Install the HKEX daily quotation feed: its dataset, stopped control, and schedule inspection.

Revision ID: d8e0f2a4c6b9
Revises: b7d9f1a3c5e8

The provider catalog now lists ``hkex`` with one feed, ``hk_equity_official_eod``
(``exchange_daily_quote`` v1, 21:05 Hong Kong time). Registry provisioning requires
the dataset row, and starting a scheduler requires its control and datasets, so
both are created here, the control stopped. Nothing is acquired until an
operator publishes the first universe baseline and starts the control.

Frozen migration inputs: never import the live catalog or seed. This revision
replaces b3d5f7a9c1e2 as the schedule inspection used by the deploy preflight
and seed: its targets are that revision's plus the HKEX feed and control, so an
active HKEX control is no longer an unknown difference. Every other row is
judged by b3d5f7a9c1e2's own frozen rules.

Downgrade removes what this revision added and refuses once the dataset has
been used, because its universes, plans, runs and quotes cannot be removed by a
schema rollback.
"""

import functools
import importlib.util
import json
from copy import deepcopy
from datetime import time
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "d8e0f2a4c6b9"
down_revision = "b7d9f1a3c5e8"
branch_labels = None
depends_on = None

DATASET = "hk_equity_official_eod"
PROVIDER = "hkex"
SCHEDULER_KEY = "full_market_hkex_v1"
SLOT = "asia_pacific_markets_window"
ZONE = "Asia/Hong_Kong"
CLOCK = "21:05:00"
# dataset: provider, timezone, target clock, known old states, monitor clock, day offset
NEW_FEEDS = {DATASET: (PROVIDER, ZONE, CLOCK, (), None, 0)}
# provider: timezone, target clock, known old (timezone, clock) states.
NEW_CONTROLS = {PROVIDER: (ZONE, CLOCK, ())}
# Tables that hold rows of the dataset once it has been used.
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
CONFIG = {
    "schema_id": "exchange_daily_quote",
    "accepted_schema_versions": [1],
    "current_schema_version": 1,
    "schema_enforcement": "enforce",
    "allowed_sources": [PROVIDER],
    # The rows correct FactSet's HK equity rows: same market and asset class.
    "defaults": {"market": "HK", "asset_class": "equity", "currency": None},
    "delivery_expectation": {
        "delivery_mode": "incremental",
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
            "timezone": ZONE,
            "market_close_time": "16:10:00",
            "availability_grace_minutes": 120,
            "action": "warn",
        },
        "missing_delivery": {"action": "disabled", "expected_sources": []},
        "schedule": {
            "enabled": False,
            "slot_id": SLOT,
            "local_time": CLOCK,
            "timezone": ZONE,
            "target_date_lag_days": 0,
            "expected_sources": [PROVIDER],
        },
    },
    "full_market": {
        "enabled": False,
        "required": True,
        "activation_date": None,
        "calendar_market": "HK",
    },
    "source_precedence": [PROVIDER],
}
DESCRIPTION = (
    "Staged hkex exchange_daily_quote.v1 exchange daily quotation; "
    "corrects provider EOD close, high, low, volume and turnover"
)


@functools.cache
def _previous_revision():
    """The frozen b3d5f7a9c1e2 rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("b3d5f7a9c1e2_factset_hk_2000_schedule.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_previous", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The complete targets this revision governs, for callers that read them.
FEEDS = {**_previous_revision().FEEDS, **NEW_FEEDS}
CONTROLS = {**_previous_revision().CONTROLS, **NEW_CONTROLS}


def _project_new(key: str, config: dict) -> tuple[str, dict]:
    """The HKEX feed has no older shape: only its own target is compliant."""
    provider, zone, clock, _, _, _ = NEW_FEEDS[key]
    try:
        expectation = config["delivery_expectation"]
        schedule = expectation["schedule"]
        fields = ("timezone", "local_time", "target_date_lag_days", "expected_sources")
        current = tuple(schedule.get(field) for field in fields)
        if type(schedule.get("target_date_lag_days")) is not int or current != (
            zone,
            clock,
            0,
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
    """b3d5f7a9c1e2's report, with the HKEX dataset and control judged here."""
    report, dataset_updates, control_updates = _previous_revision().inspect(
        connection, lock=lock, reverse=reverse
    )
    controls = {
        row["scheduler_key"]: row
        for row in connection.execute(
            sa.text(
                "SELECT scheduler_key, provider, execution_profile, scheduled_local_time, timezone "
                "FROM scheduler_control WHERE retired_at IS NULL AND provider = :provider"
            ),
            {"provider": PROVIDER},
        ).mappings()
    }
    datasets = {
        row["dataset_key"]: row["config"]
        for row in connection.execute(
            sa.text("SELECT dataset_key, config FROM dataset_registry WHERE dataset_key = :key"),
            {"key": DATASET},
        ).mappings()
    }
    for row in report:
        if row["kind"] == "control" and row["key"] in controls:
            control = controls[row["key"]]
            zone, clock, _ = NEW_CONTROLS[PROVIDER]
            current = (control["timezone"], str(control["scheduled_local_time"]))
            row["status"] = (
                "already_compliant"
                if control["scheduler_key"] == SCHEDULER_KEY and current == (zone, clock)
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
    bind.execute(
        sa.text(
            "INSERT INTO dataset_registry (dataset_key, name, description, asset_class, "
            "market, frequency, is_active, config, created_at, updated_at) "
            "VALUES (:key, :name, :description, 'equity', 'HK', 'daily', true, "
            "CAST(:config AS jsonb), now(), now()) ON CONFLICT (dataset_key) DO NOTHING"
        ),
        {
            "key": DATASET,
            "name": "港股交易所每日行情",
            "description": DESCRIPTION,
            "config": json.dumps(CONFIG),
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO scheduler_control (scheduler_key, provider, execution_profile, slot_id, "
            "scheduled_local_time, timezone, desired_state, observed_state, revision, "
            "created_at, updated_at) "
            "VALUES (:key, :provider, 'full_market', :slot, CAST(:clock AS time), :zone, "
            "'stopped', 'stopped', 1, now(), now()) ON CONFLICT (scheduler_key) DO NOTHING"
        ),
        {
            "key": SCHEDULER_KEY,
            "provider": PROVIDER,
            "slot": SLOT,
            "clock": time.fromisoformat(CLOCK),
            "zone": ZONE,
        },
    )
    bind.execute(
        sa.text(
            "INSERT INTO scheduler_dataset (scheduler_key, dataset_key) "
            "VALUES (:scheduler, :key) ON CONFLICT DO NOTHING"
        ),
        {"scheduler": SCHEDULER_KEY, "key": DATASET},
    )
    # A row this revision did not write (ON CONFLICT) must still meet the targets.
    _require_compliant(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table in USAGE_TABLES:
        used = bind.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE dataset_key = :key LIMIT 1"), {"key": DATASET}
        ).first()
        if used:
            raise RuntimeError(
                f"HKEX dataset has been used; downgrade is blocked (rows in {table})"
            )
    # A started control keeps its admission proofs.
    admitted = bind.execute(
        sa.text("SELECT 1 FROM full_market_admission WHERE scheduler_key = :scheduler LIMIT 1"),
        {"scheduler": SCHEDULER_KEY},
    ).first()
    if admitted:
        raise RuntimeError("HKEX control has been started; downgrade is blocked")
    for statement in (
        "DELETE FROM scheduler_dataset WHERE scheduler_key = :scheduler",
        "DELETE FROM scheduler_control WHERE scheduler_key = :scheduler",
        "DELETE FROM dataset_registry WHERE dataset_key = :key",
    ):
        bind.execute(sa.text(statement), {"scheduler": SCHEDULER_KEY, "key": DATASET})
