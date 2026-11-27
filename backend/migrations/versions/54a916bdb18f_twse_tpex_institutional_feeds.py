"""Install the TWSE and TPEx three institutional investors feeds under their controls.

Revision ID: 54a916bdb18f
Revises: e28706e77614

The provider catalog now lists four more official Taiwan feeds, one per
(exchange, table), each with one fixed member (the board) and one request per
day for the whole table:

- ``tw_twse_institutional_summary`` (BFI82U, TWD amounts), 15:30 Taipei;
- ``tw_twse_institutional_stock`` (T86, shares, warrants included), 16:45;
- ``tw_tpex_institutional_summary`` and ``tw_tpex_institutional_stock``, 17:30,
  after TPEx's daily revision at about 17:15.

They are bound to the existing stopped controls ``full_market_twse_v1`` and
``full_market_tpex_v1``, whose clock stays their earliest feed's (the index,
14:15 Taipei). Each Fetcher Source credential is widened to its provider's three
datasets only while it still holds exactly the index dataset; any other scope is
an operator's and is kept.

Frozen migration inputs: never import the live catalog or seed. This revision
replaces e28706e77614 as the schedule inspection used by the deploy preflight
and seed: its targets are that revision's plus these four feeds. Every other row
is judged by e28706e77614's own frozen rules.

Downgrade removes what this revision added and refuses once a dataset has been
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

revision = "54a916bdb18f"
down_revision = "e28706e77614"
branch_labels = None
depends_on = None

ZONE = "Asia/Taipei"
SLOT = "taiwan_market_window"
# dataset: provider, timezone, target clock, known old states, monitor clock, day offset
NEW_FEEDS = {
    "tw_twse_institutional_summary": ("twse", ZONE, "15:30:00", (), None, 0),
    "tw_twse_institutional_stock": ("twse", ZONE, "16:45:00", (), None, 0),
    "tw_tpex_institutional_summary": ("tpex", ZONE, "17:30:00", (), None, 0),
    "tw_tpex_institutional_stock": ("tpex", ZONE, "17:30:00", (), None, 0),
}
TARGET_DATE_LAGS = dict.fromkeys(NEW_FEEDS, 0)
SCHEMAS = {
    "tw_twse_institutional_summary": "institutional_flow_summary",
    "tw_twse_institutional_stock": "institutional_flow_stock",
    "tw_tpex_institutional_summary": "institutional_flow_summary",
    "tw_tpex_institutional_stock": "institutional_flow_stock",
}
NAMES = {
    "tw_twse_institutional_summary": "上市三大法人買賣金額",
    "tw_twse_institutional_stock": "上市三大法人個股買賣超",
    "tw_tpex_institutional_summary": "上櫃三大法人買賣金額",
    "tw_tpex_institutional_stock": "上櫃三大法人個股買賣超",
}
SCHEDULER_KEYS = {"twse": "full_market_twse_v1", "tpex": "full_market_tpex_v1"}
# Each Fetcher credential's scope before and after, in the catalog's order.
PREVIOUS_SCOPES = {"twse": ["tw_twse_index_eod"], "tpex": ["tw_tpex_index_eod"]}
STAGED_SCOPES = {
    provider: sorted([*scope, *(key for key, feed in NEW_FEEDS.items() if feed[0] == provider)])
    for provider, scope in PREVIOUS_SCOPES.items()
}
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


def _config(key: str) -> dict:
    """The seed's declaration at this revision, frozen."""
    provider, zone, clock, *_ = NEW_FEEDS[key]
    return {
        "schema_id": SCHEMAS[key],
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": [provider],
        "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
        "delivery_expectation": {
            "delivery_mode": "incremental",
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
                "calendar_market": "TW",
                "timezone": zone,
                "market_close_time": "13:30:00",
                "availability_grace_minutes": 120,
                "action": "warn",
            },
            "missing_delivery": {"action": "disabled", "expected_sources": []},
            "schedule": {
                "enabled": False,
                "slot_id": SLOT,
                "local_time": clock,
                "timezone": zone,
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


CONFIGS = {key: _config(key) for key in NEW_FEEDS}
DESCRIPTIONS = {
    key: (
        f"Staged {feed[0]} {SCHEMAS[key]}.v1 three institutional investors table; "
        "one fixed member per board"
    )
    for key, feed in NEW_FEEDS.items()
}


@functools.cache
def _previous_revision():
    """The frozen e28706e77614 rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("e28706e77614_twse_tpex_index_feeds.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_tw_index", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The complete targets this revision governs, for callers that read them.
FEEDS = {**_previous_revision().FEEDS, **NEW_FEEDS}
CONTROLS = _previous_revision().CONTROLS


def _project_new(key: str, config: dict) -> tuple[str, dict]:
    """The institutional feeds have no older shape: only their own targets are compliant."""
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
    """e28706e77614's report, with the four institutional datasets judged here."""
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


def _set_scope(bind, provider: str, current: list[str], target: list[str]) -> None:
    """Move a Fetcher Source credential between scopes, only from exactly ``current``."""
    bind.execute(
        sa.text(
            "UPDATE source_client SET allowed_datasets = CAST(:target AS jsonb), updated_at = now() "
            "WHERE lower(trim(source_name)) = :provider AND revoked_at IS NULL "
            "AND jsonb_typeof(allowed_datasets) = 'array' "
            "AND allowed_datasets @> CAST(:current AS jsonb) "
            "AND allowed_datasets <@ CAST(:current AS jsonb)"
        ),
        {"provider": provider, "target": json.dumps(target), "current": json.dumps(current)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    for key, config in CONFIGS.items():
        bind.execute(
            sa.text(
                "INSERT INTO dataset_registry (dataset_key, name, description, asset_class, "
                "market, frequency, is_active, config, created_at, updated_at) "
                "VALUES (:key, :name, :description, 'equity', 'TW', 'daily', true, "
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
            {"scheduler": SCHEDULER_KEYS[NEW_FEEDS[key][0]], "key": key},
        )
    for provider, current in PREVIOUS_SCOPES.items():
        _set_scope(bind, provider, current, STAGED_SCOPES[provider])
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
                    f"Institutional dataset {key} has been used; downgrade is blocked "
                    f"(rows in {table})"
                )
    for provider, current in PREVIOUS_SCOPES.items():
        _set_scope(bind, provider, STAGED_SCOPES[provider], current)
    for statement in (
        "DELETE FROM scheduler_dataset WHERE dataset_key IN :keys",
        "DELETE FROM dataset_registry WHERE dataset_key IN :keys",
    ):
        bind.execute(
            sa.text(statement).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": sorted(NEW_FEEDS)},
        )
