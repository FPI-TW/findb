"""Install the HSIL index reference feeds under the HKEX control, and schedule inspection.

Revision ID: 21e8f89c680b
Revises: 54a916bdb18f

The provider catalog now lists two more ``hkex`` feeds, read by the HKEX runtime
from Hang Seng Indexes' documents: ``hk_index_constituents``
(``index_constituents`` v1, members ``HSI`` and ``HSCI``) and ``hk_index_changes``
(``index_constituent_changes`` v1, one member per index), both 21:05 Hong Kong
time for the same date, like ``hk_equity_official_eod``.  Both datasets are
created here and bound to the existing control ``full_market_hkex_v1``, whose
clock stays 21:05.  The HKEX Fetcher's Source credential is widened to them only
while it still holds exactly the one quotation dataset; any other scope is an
operator's and is kept.  Nothing is acquired until an operator publishes each
first universe baseline and the control admits the feeds.

Frozen migration inputs: never import the live catalog or seed. This revision
replaces 54a916bdb18f as the schedule inspection used by the deploy preflight
and seed: its targets are that revision's plus the two feeds. Every other row is
judged by 54a916bdb18f's own frozen rules.

Downgrade removes what this revision added and refuses once a dataset has been
used, because its universes, plans, runs and documents cannot be removed by a
schema rollback.
"""

import functools
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "21e8f89c680b"
down_revision = "54a916bdb18f"
branch_labels = None
depends_on = None

PROVIDER = "hkex"
SCHEDULER_KEY = "full_market_hkex_v1"
ZONE = "Asia/Hong_Kong"
CLOCK = "21:05:00"
# dataset: provider, timezone, target clock, known old states, monitor clock, day offset
NEW_FEEDS = {
    "hk_index_constituents": (PROVIDER, ZONE, CLOCK, (), None, 0),
    "hk_index_changes": (PROVIDER, ZONE, CLOCK, (), None, 0),
}
TARGET_DATE_LAGS = {key: 0 for key in NEW_FEEDS}
SCHEMAS = {
    "hk_index_constituents": "index_constituents",
    "hk_index_changes": "index_constituent_changes",
}
NAMES = {"hk_index_constituents": "恒指成分股名單", "hk_index_changes": "恒指成分股變動"}
DESCRIPTIONS = {
    "hk_index_constituents": (
        "Staged hkex index_constituents.v1 HSIL constituent reports (HSI, HSCI): lists, weights, HSICS"
    ),
    "hk_index_changes": (
        "Staged hkex index_constituent_changes.v1 HSIL change workbooks: effective changes per index"
    ),
}
# The HKEX Fetcher credential's scope before and after, in the catalog's order.
PREVIOUS_SCOPE = ["hk_equity_official_eod"]
STAGED_SCOPE = ["hk_equity_official_eod", "hk_index_changes", "hk_index_constituents"]
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


def _config(schema_id: str) -> dict:
    """The seed's declaration at this revision, frozen."""
    return {
        "schema_id": schema_id,
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": [PROVIDER],
        "defaults": {"market": "HK", "asset_class": "index", "currency": None},
        "delivery_expectation": {
            "delivery_mode": "full_snapshot",
            "baseline": {
                "strategy": "rolling_median",
                "scope": "dataset_source_schema",
                "window_size": 7,
                "minimum_history": 3,
                "enabled": False,
            },
            "record_count": {
                "minimum_record_count": 0,
                "maximum_count_drop_ratio": 0.1,
                "action": "disabled",
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
                "slot_id": "asia_pacific_markets_window",
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


CONFIGS = {key: _config(schema_id) for key, schema_id in SCHEMAS.items()}


@functools.cache
def _previous_revision():
    """The frozen 54a916bdb18f rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("54a916bdb18f_twse_tpex_institutional_feeds.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_tw_institutional", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The complete targets this revision governs, for callers that read them.
FEEDS = {**_previous_revision().FEEDS, **NEW_FEEDS}
CONTROLS = _previous_revision().CONTROLS


def _project_new(key: str, config: dict) -> tuple[str, dict]:
    """The reference feeds have no older shape: only their own targets are compliant."""
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
    """54a916bdb18f's report, with the two reference datasets judged here."""
    report, dataset_updates, control_updates = _previous_revision().inspect(
        connection, lock=lock, reverse=reverse
    )
    rows = connection.execute(
        sa.text(
            "SELECT dataset_key, config FROM dataset_registry "
            "WHERE dataset_key IN :keys ORDER BY dataset_key"
        ).bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": sorted(NEW_FEEDS)},
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
    """Move the HKEX Source credential between scopes, only from exactly ``current``."""
    bind.execute(
        sa.text(
            "UPDATE source_client SET allowed_datasets = CAST(:target AS jsonb), updated_at = now() "
            "WHERE lower(trim(source_name)) = 'hkex' AND revoked_at IS NULL "
            "AND jsonb_typeof(allowed_datasets) = 'array' "
            "AND allowed_datasets @> CAST(:current AS jsonb) "
            "AND allowed_datasets <@ CAST(:current AS jsonb)"
        ),
        {"target": json.dumps(target), "current": json.dumps(current)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for key, config in CONFIGS.items():
        bind.execute(
            sa.text(
                "INSERT INTO dataset_registry (dataset_key, name, description, asset_class, "
                "market, frequency, is_active, config, created_at, updated_at) "
                "VALUES (:key, :name, :description, 'index', 'HK', 'daily', true, "
                "CAST(:config AS jsonb), now(), now()) ON CONFLICT (dataset_key) DO NOTHING"
            ),
            {
                "key": key,
                "name": NAMES[key],
                "description": DESCRIPTIONS[key],
                "config": json.dumps(config),
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO scheduler_dataset (scheduler_key, dataset_key) "
                "VALUES (:scheduler, :key) ON CONFLICT DO NOTHING"
            ),
            {"scheduler": SCHEDULER_KEY, "key": key},
        )
    _set_scope(bind, PREVIOUS_SCOPE, STAGED_SCOPE)
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
                    f"HSIL dataset {key} has been used; downgrade is blocked (rows in {table})"
                )
    _set_scope(bind, STAGED_SCOPE, PREVIOUS_SCOPE)
    for statement in (
        "DELETE FROM scheduler_dataset WHERE dataset_key IN :keys",
        "DELETE FROM dataset_registry WHERE dataset_key IN :keys",
    ):
        bind.execute(
            sa.text(statement).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": sorted(NEW_FEEDS)},
        )
