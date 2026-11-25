"""Install the FMP dollar index feed: one dataset under the FMP control, and schedule inspection.

Revision ID: 64da893e5e78
Revises: 2bbe07b98293

``DX-Y.NYB`` trades around the clock: about 20:00 New York time FMP replaces the
date's row with the next session's, so the Taipei 08:30 group fetched it too late.
The catalog now lists it alone as ``us_dollar_index_eod`` (17:15 New York time, the
same date, after the 17:00 settlement), bound to the existing stopped control
``full_market_fmp_v1``, whose clock stays its earliest feed's (08:30 Taipei). The
FMP Fetcher's Source credential is widened to it only while it still holds
exactly the two index datasets; any other scope is an operator's and is kept.

Frozen migration inputs: never import the live catalog or seed. This revision
replaces e5a7c9b1d3f2 as the schedule inspection used by the deploy preflight
and seed: its targets are that revision's plus this feed. Every other row is
judged by e5a7c9b1d3f2's own frozen rules.

Downgrade removes what this revision added and refuses once the dataset has been
used, because its universes, plans, runs and rows cannot be removed by a schema
rollback.
"""

import functools
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "64da893e5e78"
down_revision = "2bbe07b98293"
branch_labels = None
depends_on = None

PROVIDER = "fmp"
SCHEDULER_KEY = "full_market_fmp_v1"
DATASET = "us_dollar_index_eod"
# dataset: provider, timezone, target clock, known old states, monitor clock, day offset
NEW_FEEDS = {DATASET: (PROVIDER, "America/New_York", "17:15:00", (), None, 0)}
TARGET_DATE_LAGS = {DATASET: 0}
NAME = "美元指數日K"
# The FMP Fetcher credential's scope before and after, in the catalog's order.
PREVIOUS_SCOPE = ["hk_index_eod", "us_index_eod"]
STAGED_SCOPE = ["hk_index_eod", "us_dollar_index_eod", "us_index_eod"]
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
# The seed's declaration at this revision, frozen.
CONFIG = {
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
            "local_time": "17:15:00",
            "timezone": "America/New_York",
            "target_date_lag_days": 0,
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
}
DESCRIPTION = "Staged fmp market_eod.v2 full-market daily feed"


@functools.cache
def _previous_revision():
    """The frozen e5a7c9b1d3f2 rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("e5a7c9b1d3f2_fmp_index_feeds.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_fmp", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The complete targets this revision governs, for callers that read them.
FEEDS = {**_previous_revision().FEEDS, **NEW_FEEDS}
CONTROLS = _previous_revision().CONTROLS


def _project_new(key: str, config: dict) -> tuple[str, dict]:
    """The dollar index feed has no older shape: only its own targets are compliant."""
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
    """e5a7c9b1d3f2's report, with the dollar index dataset judged here."""
    report, dataset_updates, control_updates = _previous_revision().inspect(
        connection, lock=lock, reverse=reverse
    )
    rows = connection.execute(
        sa.text("SELECT dataset_key, config FROM dataset_registry WHERE dataset_key = :key"),
        {"key": DATASET},
    ).mappings()
    for row in rows:
        status, _ = _project_new(row["dataset_key"], row["config"])
        report.append({"kind": "dataset", "key": row["dataset_key"], "status": status})
    return report, dataset_updates, control_updates


def _require_compliant(connection) -> None:
    report, _, _ = inspect(connection, lock=True)
    unknown = [row["key"] for row in report if row["status"] == "unknown_difference"]
    if unknown:
        raise RuntimeError("Catalog schedule preflight unknown differences: " + ", ".join(unknown))


def _set_scope(bind, current: list[str], target: list[str]) -> None:
    """Move the FMP Source credential between scopes, only from exactly ``current``."""
    bind.execute(
        sa.text(
            "UPDATE source_client SET allowed_datasets = CAST(:target AS jsonb), updated_at = now() "
            "WHERE lower(trim(source_name)) = 'fmp' AND revoked_at IS NULL "
            "AND jsonb_typeof(allowed_datasets) = 'array' "
            "AND allowed_datasets @> CAST(:current AS jsonb) "
            "AND allowed_datasets <@ CAST(:current AS jsonb)"
        ),
        {"target": json.dumps(target), "current": json.dumps(current)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO dataset_registry (dataset_key, name, description, asset_class, "
            "market, frequency, is_active, config, created_at, updated_at) "
            "VALUES (:key, :name, :description, 'index', 'US', 'daily', true, "
            "CAST(:config AS jsonb), now(), now()) ON CONFLICT (dataset_key) DO NOTHING"
        ),
        {"key": DATASET, "name": NAME, "description": DESCRIPTION, "config": json.dumps(CONFIG)},
    )
    bind.execute(
        sa.text(
            "INSERT INTO scheduler_dataset (scheduler_key, dataset_key) "
            "VALUES (:scheduler, :key) ON CONFLICT DO NOTHING"
        ),
        {"scheduler": SCHEDULER_KEY, "key": DATASET},
    )
    _set_scope(bind, PREVIOUS_SCOPE, STAGED_SCOPE)
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
                f"FMP dataset {DATASET} has been used; downgrade is blocked (rows in {table})"
            )
    _set_scope(bind, STAGED_SCOPE, PREVIOUS_SCOPE)
    bind.execute(
        sa.text("DELETE FROM scheduler_dataset WHERE dataset_key = :key"), {"key": DATASET}
    )
    bind.execute(sa.text("DELETE FROM dataset_registry WHERE dataset_key = :key"), {"key": DATASET})
