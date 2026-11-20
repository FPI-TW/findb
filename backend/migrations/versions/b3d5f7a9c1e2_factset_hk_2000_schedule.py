"""Move the FactSet Hong Kong daily start to 20:00, the HKEX daily report slot.

Revision ID: b3d5f7a9c1e2
Revises: a9c1e3f5b7d0

Frozen migration inputs: never import the live catalog. This revision replaces
e9f4a1c6b3d5 as the schedule inspection used by the deploy preflight and seed;
its targets are that revision's targets except the Hong Kong clock. Known old
defaults are that revision's old defaults plus the Hong Kong 18:30 start.
Unknown differences abort before any update. Downgrade restores 18:30 only when every governed value is still
this revision's target.
"""

import functools
import importlib.util
import json
from copy import deepcopy
from datetime import time
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "b3d5f7a9c1e2"
down_revision = "a9c1e3f5b7d0"
branch_labels = None
depends_on = None

# dataset: provider, timezone, target clock, known old (timezone, clock, lag, source)
# states beyond e9f4a1c6b3d5, bounded monitor clock and day offset. Older shapes
# (pre-e9f4a1c6b3d5 Taipei clocks, the TW bootstrap policy) are projected by that
# revision's own frozen rules first. Slot, enabled, actions are not projected.
FEEDS = {
    "us_equity_eod": ("twelve_data", "America/New_York", "17:00:00", (), "09:15:00", 1),
    "hk_equity_eod": (
        "factset",
        "Asia/Hong_Kong",
        "20:00:00",
        (("Asia/Hong_Kong", "18:30:00", 0, "factset"),),
        None,
        0,
    ),
    "tw_equity_eod": ("finlab", "Asia/Taipei", "18:00:00", (), "17:00:00", 0),
    "tw_etf_eod": ("finlab", "Asia/Taipei", "18:00:00", (), None, 0),
    "tw_futures_eod": ("taifex", "Asia/Taipei", "17:00:00", (), None, 0),
}
# provider: timezone, target clock, known old (timezone, clock) states.
CONTROLS = {
    "twelve_data": ("America/New_York", "17:00:00", (("Asia/Taipei", "08:00:00"),)),
    "finlab": ("Asia/Taipei", "18:00:00", ()),
    "taifex": ("Asia/Taipei", "17:00:00", (("Asia/Taipei", "18:00:00"),)),
    "factset": (
        "Asia/Hong_Kong",
        "20:00:00",
        (("Asia/Taipei", "18:30:00"), ("Asia/Hong_Kong", "18:30:00")),
    ),
}
# The clock this revision moves; downgrade restores it from the target.
_PREVIOUS = {"hk_equity_eod": "18:30:00", "factset": "18:30:00"}


@functools.cache
def _previous_revision():
    """The frozen e9f4a1c6b3d5 rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("e9f4a1c6b3d5_catalog_schedule_projection.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_projection", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def project_config(key: str, config: dict, *, reverse: bool = False) -> tuple[str, dict]:
    provider, zone, clock, olds, deadline, offset = FEEDS[key]
    known_old = [(z, c, lag, [source]) for z, c, lag, source in olds]
    original = config
    if reverse:
        known_old = [(zone, clock, 0, [provider])] if key in _PREVIOUS else []
        clock = _PREVIOUS.get(key, clock)
    else:
        status, projected = _previous_revision().project_config(key, config)
        if status == "known_old_default":
            config = projected
    result = deepcopy(config)
    try:
        expectation = result["delivery_expectation"]
        schedule = expectation["schedule"]
        fields = ("timezone", "local_time", "target_date_lag_days", "expected_sources")
        current = tuple(schedule.get(field) for field in fields)
        if type(schedule.get("target_date_lag_days")) is not int:
            return "unknown_difference", original
        target = (zone, clock, 0, [provider])
        if current not in [target, *known_old]:
            return "unknown_difference", original
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
            return "unknown_difference", original
        target_deadline = (deadline, "Asia/Taipei", offset) if deadline else (None, None, None)
        if current_deadline not in ((deadline, None, None), target_deadline):
            return "unknown_difference", original
        if deadline:
            monitor.update(
                deadline_local_time=deadline,
                deadline_timezone="Asia/Taipei",
                deadline_day_offset=offset,
            )
    except (KeyError, TypeError, AttributeError):
        return "unknown_difference", original
    return ("already_compliant" if result == original else "known_old_default"), result


def inspect(
    connection, *, lock: bool = False, reverse: bool = False
) -> tuple[list[dict], list[dict], list[dict]]:
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
        status, config = project_config(key, row["config"], reverse=reverse)
        report.append({"kind": "dataset", "key": key, "status": status})
        if status == "known_old_default":
            dataset_updates.append({"key": key, "config": json.dumps(config)})
    for row in controls:
        provider = row["provider"]
        definition = CONTROLS.get(provider)
        status = "unknown_difference"
        if definition and row["scheduler_key"] == f"{row['execution_profile']}_{provider}_v1":
            zone, clock, olds = definition
            if reverse:
                olds = ((zone, clock),) if provider in _PREVIOUS else ()
                clock = _PREVIOUS.get(provider, clock)
            current = (row["timezone"], str(row["scheduled_local_time"]))
            if current == (zone, clock):
                status = "already_compliant"
            elif current in olds:
                status = "known_old_default"
                control_updates.append(
                    {"key": row["scheduler_key"], "zone": zone, "clock": time.fromisoformat(clock)}
                )
        report.append({"kind": "control", "key": row["scheduler_key"], "status": status})
    return report, dataset_updates, control_updates


def _apply(*, reverse: bool) -> None:
    connection = op.get_bind()
    report, datasets, controls = inspect(connection, lock=True, reverse=reverse)
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


def upgrade() -> None:
    _apply(reverse=False)


def downgrade() -> None:
    _apply(reverse=True)
