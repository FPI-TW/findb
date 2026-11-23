"""Install the FMP market index feeds: two datasets, one stopped control, and schedule inspection.

Revision ID: e5a7c9b1d3f2
Revises: d8e0f2a4c6b9

The provider catalog now lists ``fmp`` with two ``market_eod`` v2 feeds, one per
publication-time group: ``hk_index_eod`` (20:00 Hong Kong time, the same date)
and ``us_index_eod`` (08:30 Taipei time, the previous New York date). Registry
provisioning requires the dataset rows, and starting a scheduler requires its
control and datasets, so they are created here, the control stopped. Nothing is
acquired until an operator publishes each first universe baseline and starts
the control.

Frozen migration inputs: never import the live catalog or seed. This revision
replaces d8e0f2a4c6b9 as the schedule inspection used by the deploy preflight
and seed: its targets are that revision's plus the two FMP feeds and the FMP
control, whose clock is its earliest feed's (08:30 Taipei). Every other row is
judged by d8e0f2a4c6b9's own frozen rules.

Downgrade removes what this revision added and refuses once a dataset has been
used, because its universes, plans, runs and rows cannot be removed by a schema
rollback.
"""

import functools
import importlib.util
import json
from copy import deepcopy
from datetime import time
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "e5a7c9b1d3f2"
down_revision = "d8e0f2a4c6b9"
branch_labels = None
depends_on = None

PROVIDER = "fmp"
SCHEDULER_KEY = "full_market_fmp_v1"
# dataset: provider, timezone, target clock, known old states, monitor clock, day offset
NEW_FEEDS = {
    "hk_index_eod": (PROVIDER, "Asia/Hong_Kong", "20:00:00", (), None, 0),
    "us_index_eod": (PROVIDER, "Asia/Taipei", "08:30:00", (), None, 0),
}
# The New York group is fetched the next Taipei morning, for the previous date.
TARGET_DATE_LAGS = {"hk_index_eod": 0, "us_index_eod": 1}
# provider: timezone, target clock, known old (timezone, clock) states.
NEW_CONTROLS = {PROVIDER: ("Asia/Taipei", "08:30:00", ())}
CONTROL_SLOT = "western_markets_window"
NAMES = {"hk_index_eod": "港股指數日K", "us_index_eod": "美股指數日K"}
MARKETS = {"hk_index_eod": "HK", "us_index_eod": "US"}
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
# The seed's declarations at this revision, frozen.
CONFIGS = {
    "hk_index_eod": {
        "schema_id": "market_eod",
        "accepted_schema_versions": [2],
        "current_schema_version": 2,
        "schema_enforcement": "enforce",
        "allowed_sources": ["fmp"],
        "defaults": {"market": "HK", "asset_class": "index", "currency": "HKD"},
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
                "slot_id": "asia_pacific_markets_window",
                "local_time": "20:00:00",
                "timezone": "Asia/Hong_Kong",
                "target_date_lag_days": 0,
                "expected_sources": ["fmp"],
            },
        },
        "full_market": {
            "enabled": False,
            "required": True,
            "activation_date": None,
            "calendar_market": "HK",
        },
        "source_precedence": ["fmp"],
    },
    "us_index_eod": {
        "schema_id": "market_eod",
        "accepted_schema_versions": [2],
        "current_schema_version": 2,
        "schema_enforcement": "enforce",
        "allowed_sources": ["fmp"],
        "defaults": {"market": "US", "asset_class": "index", "currency": "USD"},
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
                "calendar_market": "US",
                "timezone": "America/New_York",
                "market_close_time": "16:00:00",
                "availability_grace_minutes": 120,
                "action": "warn",
            },
            "missing_delivery": {"action": "disabled", "expected_sources": []},
            "schedule": {
                "enabled": False,
                "slot_id": "western_markets_window",
                "local_time": "08:30:00",
                "timezone": "Asia/Taipei",
                "target_date_lag_days": 1,
                "expected_sources": ["fmp"],
            },
        },
        "full_market": {
            "enabled": False,
            "required": True,
            "activation_date": None,
            "calendar_market": "US",
        },
        "source_precedence": ["fmp"],
    },
}
DESCRIPTION = "Staged fmp market_eod.v1 full-market daily feed"


@functools.cache
def _previous_revision():
    """The frozen d8e0f2a4c6b9 rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("d8e0f2a4c6b9_hkex_official_eod_feed.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_hkex", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The complete targets this revision governs, for callers that read them.
FEEDS = {**_previous_revision().FEEDS, **NEW_FEEDS}
CONTROLS = {**_previous_revision().CONTROLS, **NEW_CONTROLS}


def _project_new(key: str, config: dict) -> tuple[str, dict]:
    """The FMP feeds have no older shape: only their own targets are compliant."""
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
    """d8e0f2a4c6b9's report, with the FMP datasets and control judged here."""
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
    for key, config in CONFIGS.items():
        bind.execute(
            sa.text(
                "INSERT INTO dataset_registry (dataset_key, name, description, asset_class, "
                "market, frequency, is_active, config, created_at, updated_at) "
                "VALUES (:key, :name, :description, 'index', :market, 'daily', true, "
                "CAST(:config AS jsonb), now(), now()) ON CONFLICT (dataset_key) DO NOTHING"
            ),
            {
                "key": key,
                "name": NAMES[key],
                "description": DESCRIPTION,
                "market": MARKETS[key],
                "config": json.dumps(config),
            },
        )
    zone, clock, _ = NEW_CONTROLS[PROVIDER]
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
            "slot": CONTROL_SLOT,
            "clock": time.fromisoformat(clock),
            "zone": zone,
        },
    )
    for key in sorted(NEW_FEEDS):
        bind.execute(
            sa.text(
                "INSERT INTO scheduler_dataset (scheduler_key, dataset_key) "
                "VALUES (:scheduler, :key) ON CONFLICT DO NOTHING"
            ),
            {"scheduler": SCHEDULER_KEY, "key": key},
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
                    f"FMP dataset {key} has been used; downgrade is blocked (rows in {table})"
                )
    # A started control keeps its admission proofs.
    admitted = bind.execute(
        sa.text("SELECT 1 FROM full_market_admission WHERE scheduler_key = :scheduler LIMIT 1"),
        {"scheduler": SCHEDULER_KEY},
    ).first()
    if admitted:
        raise RuntimeError("FMP control has been started; downgrade is blocked")
    bind.execute(
        sa.text("DELETE FROM scheduler_dataset WHERE scheduler_key = :scheduler"),
        {"scheduler": SCHEDULER_KEY},
    )
    bind.execute(
        sa.text("DELETE FROM scheduler_control WHERE scheduler_key = :scheduler"),
        {"scheduler": SCHEDULER_KEY},
    )
    bind.execute(
        sa.text("DELETE FROM dataset_registry WHERE dataset_key IN :keys").bindparams(
            sa.bindparam("keys", expanding=True)
        ),
        {"keys": sorted(NEW_FEEDS)},
    )
