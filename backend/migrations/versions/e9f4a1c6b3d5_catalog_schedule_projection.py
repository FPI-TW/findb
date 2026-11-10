"""Project catalog v1 clocks without changing operator policy or frozen work.

Revision ID: e9f4a1c6b3d5
Revises: d8e3f0b5a2c4

Frozen migration inputs: never import the live catalog. Unknown differences
abort before any update. Downgrade requires a forward fix, not clock restoration.
"""

import json
from copy import deepcopy
from datetime import time

import sqlalchemy as sa
from alembic import op

revision = "e9f4a1c6b3d5"
down_revision = "d8e3f0b5a2c4"
branch_labels = None
depends_on = None

# dataset: provider, target timezone, target clock, old source, old clock, old lag,
# bounded monitor clock and day offset. Slot, enabled, actions are not projected.
FEEDS = {
    "us_equity_eod": (
        "twelve_data",
        "America/New_York",
        "17:00:00",
        "twelve_data",
        "08:15:00",
        1,
        "09:15:00",
        1,
    ),
    "hk_equity_eod": (
        "factset",
        "Asia/Hong_Kong",
        "18:30:00",
        "twelve_data",
        "14:30:00",
        0,
        None,
        0,
    ),
    "tw_equity_eod": ("finlab", "Asia/Taipei", "18:00:00", "finlab", "14:30:00", 0, "17:00:00", 0),
    "tw_etf_eod": ("finlab", "Asia/Taipei", "18:00:00", "finlab", "14:30:00", 0, None, 0),
    "tw_futures_eod": ("taifex", "Asia/Taipei", "17:00:00", "taifex", "14:30:00", 0, None, 0),
}
CONTROL_OLD = {
    "twelve_data": "08:00:00",
    "finlab": "18:00:00",
    "taifex": "18:00:00",
    "factset": "18:30:00",
}


# Exact pre-seed TW policy emitted by the historical migration chain.
# A missing schedule in any other shape remains an unknown difference.
_TW_BOOTSTRAP_POLICY = {
    "baseline": {
        "minimum_history": 3,
        "scope": "dataset_source_schema",
        "strategy": "rolling_median",
        "window_size": 7,
    },
    "delivery_mode": "full_snapshot",
    "freshness": {"action": "warn", "allowed_clock_skew_minutes": 5, "maximum_fetch_age_hours": 36},
    "freshness_hours": 36,
    "latest_date": {
        "action": "warn",
        "availability_grace_minutes": 120,
        "calendar_market": "TW",
        "market_close_time": "13:30:00",
        "timezone": "Asia/Taipei",
    },
    "maximum_count_drop_ratio": 0.1,
    "minimum_record_count": 2100,
    "missing_delivery": {
        "action": "warn",
        "deadline_local_time": "17:00:00",
        "expected_sources": ["finlab"],
    },
    "record_count": {
        "action": "warn",
        "maximum_count_drop_ratio": 0.1,
        "minimum_record_count": 2100,
    },
}


def project_config(key: str, config: dict) -> tuple[str, dict]:
    provider, zone, clock, old_source, old_clock, old_lag, deadline, offset = FEEDS[key]
    result = deepcopy(config)
    try:
        expectation = result["delivery_expectation"]
        if key == "tw_equity_eod" and expectation == _TW_BOOTSTRAP_POLICY:
            expectation["schedule"] = {
                "enabled": True,
                "slot_id": "taiwan_market_window",
                "timezone": "Asia/Taipei",
                "local_time": "14:30:00",
                "target_date_lag_days": 0,
                "expected_sources": ["finlab"],
            }
        schedule = expectation["schedule"]
        fields = ("timezone", "local_time", "target_date_lag_days", "expected_sources")
        current = tuple(schedule.get(field) for field in fields)
        if type(schedule.get("target_date_lag_days")) is not int:
            return "unknown_difference", config
        target = (zone, clock, 0, [provider])
        old = ("Asia/Taipei", old_clock, old_lag, [old_source])
        if current not in (old, target):
            return "unknown_difference", config
        schedule.update(zip(fields, target, strict=True))
        monitor = expectation["missing_delivery"]
        current_deadline = tuple(
            monitor.get(field)
            for field in ("deadline_local_time", "deadline_timezone", "deadline_day_offset")
        )
        if (
            monitor.get("deadline_day_offset") is not None
            and type(monitor["deadline_day_offset"]) is not int
        ):
            return "unknown_difference", config
        target_deadline = (deadline, "Asia/Taipei", offset) if deadline else (None, None, None)
        if current_deadline not in ((deadline, None, None), target_deadline):
            return "unknown_difference", config
        if deadline:
            monitor.update(
                deadline_local_time=deadline,
                deadline_timezone="Asia/Taipei",
                deadline_day_offset=offset,
            )
    except (KeyError, TypeError, AttributeError):
        return "unknown_difference", config
    return ("already_compliant" if result == config else "known_old_default"), result


def inspect(connection, *, lock: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
    suffix = " FOR UPDATE" if lock else ""
    datasets = list(
        connection.execute(
            sa.text(
                "SELECT dataset_key, config FROM dataset_registry ORDER BY dataset_key" + suffix
            )
        ).mappings()
    )
    controls = list(
        connection.execute(
            sa.text(
                "SELECT scheduler_key, provider, execution_profile, scheduled_local_time, timezone FROM scheduler_control WHERE retired_at IS NULL ORDER BY scheduler_key"
                + suffix
            )
        ).mappings()
    )
    report, dataset_updates, control_updates = [], [], []
    for row in datasets:
        key = row["dataset_key"]
        if key not in FEEDS:
            continue
        status, config = project_config(key, row["config"])
        report.append({"kind": "dataset", "key": key, "status": status})
        if status == "known_old_default":
            dataset_updates.append({"key": key, "config": json.dumps(config)})
    for row in controls:
        provider = row["provider"]
        feed = next((f for f in FEEDS.values() if f[0] == provider), None)
        target = (feed[1], feed[2]) if feed else None
        current = (row["timezone"], str(row["scheduled_local_time"]))
        valid_key = (
            row["execution_profile"] in ("pilot", "full_market")
            and row["scheduler_key"] == f"{row['execution_profile']}_{provider}_v1"
        )
        status = "unknown_difference"
        if target and valid_key:
            if current == target:
                status = "already_compliant"
            elif current == ("Asia/Taipei", CONTROL_OLD[provider]):
                status = "known_old_default"
                control_updates.append(
                    {
                        "key": row["scheduler_key"],
                        "zone": target[0],
                        "clock": time.fromisoformat(target[1]),
                    }
                )
        report.append({"kind": "control", "key": row["scheduler_key"], "status": status})
    return report, dataset_updates, control_updates


def upgrade() -> None:
    connection = op.get_bind()
    report, datasets, controls = inspect(connection, lock=True)
    unknown = [row["key"] for row in report if row["status"] == "unknown_difference"]
    if unknown:
        raise RuntimeError("Catalog schedule preflight unknown differences: " + ", ".join(unknown))
    for row in datasets:
        connection.execute(
            sa.text(
                "UPDATE dataset_registry SET config=CAST(:config AS jsonb) WHERE dataset_key=:key"
            ),
            row,
        )
    for row in controls:
        connection.execute(
            sa.text(
                "UPDATE scheduler_control SET timezone=:zone, scheduled_local_time=CAST(:clock AS time) WHERE scheduler_key=:key"
            ),
            row,
        )


def downgrade() -> None:
    raise RuntimeError(
        "Catalog schedule projection requires a forward fix; independent deadline timezone is the rollback floor"
    )
