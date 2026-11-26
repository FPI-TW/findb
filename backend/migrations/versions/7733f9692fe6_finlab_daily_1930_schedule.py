"""Move the FinLab daily start to 19:30 Taipei, and its intraday feeds to 19:35.

Revision ID: 7733f9692fe6
Revises: ebb187773e30

The daily price tables are taken only once FinLab publishes the day's final copy.
Since 2026-11-16 that copy has appeared at 18:48-18:54 every trading day, so an
18:00 start spends three 15-minute "not yet published" retries before it. The
daily feeds now start at 19:30 (deadline 07:30). Until the intraday feeds run in
their own lane, the provider keeps one queue in scheduled order, so they move
from 19:15 to 19:35 to stay behind the daily.

Frozen migration inputs: never import the live catalog or seed. This revision
replaces 64da893e5e78 as the schedule inspection used by the deploy preflight
and seed: its targets are that revision's, with the two FinLab daily feeds and
the FinLab control at 19:30; their old 18:00 start is a known old state. Every
other row is judged by 64da893e5e78's own frozen rules. The intraday feeds are
not in the inspection; their stored (disabled) schedule only follows what seed
declares, as in d7f9b1c3e5a8. Unknown differences abort before any update.
Downgrade restores the old clocks only where they are still this revision's.
"""

import functools
import importlib.util
import json
from copy import deepcopy
from datetime import time
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "7733f9692fe6"
down_revision = "ebb187773e30"
branch_labels = None
depends_on = None

PROVIDER = "finlab"
SCHEDULER_KEY = "full_market_finlab_v1"
ZONE = "Asia/Taipei"
OLD_DAILY = "18:00:00"
NEW_DAILY = "19:30:00"
DAILY = ("tw_equity_eod", "tw_etf_eod")
INTRADAY = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")
OLD_INTRADAY = "19:15:00"
NEW_INTRADAY = "19:35:00"


@functools.cache
def _previous_revision():
    """The frozen 64da893e5e78 rules, loaded by path like the deploy preflight."""
    path = Path(__file__).with_name("64da893e5e78_fmp_dollar_index_feed.py")
    spec = importlib.util.spec_from_file_location("catalog_schedule_fmp_dollar", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _moved(entry: tuple) -> tuple:
    provider, zone, clock, olds, deadline, offset = entry
    assert (provider, zone, clock, olds) == (PROVIDER, ZONE, OLD_DAILY, ())
    return provider, zone, NEW_DAILY, ((ZONE, OLD_DAILY, 0, PROVIDER),), deadline, offset


# The complete targets this revision governs, for callers that read them.
FEEDS = {
    **_previous_revision().FEEDS,
    **{key: _moved(_previous_revision().FEEDS[key]) for key in DAILY},
}
CONTROLS = {
    **_previous_revision().CONTROLS,
    PROVIDER: (ZONE, NEW_DAILY, ((ZONE, OLD_DAILY),)),
}


def _with_clock(config: dict, clock: str) -> dict:
    result = deepcopy(config)
    result["delivery_expectation"]["schedule"]["local_time"] = clock
    return result


def _project_daily(key: str, config: dict) -> tuple[str, dict]:
    """The previous rules judge everything but the start, which moves to 19:30."""
    try:
        current = config["delivery_expectation"]["schedule"]["local_time"]
        # The previous rules know only the 18:00 start; judge the rest under it.
        probe = _with_clock(config, OLD_DAILY) if current == NEW_DAILY else config
    except (KeyError, TypeError, AttributeError):
        return "unknown_difference", config
    status, projected = _previous_revision().project_config(key, probe)
    if status == "unknown_difference":
        return status, config
    result = _with_clock(projected, NEW_DAILY)
    return ("already_compliant" if result == config else "known_old_default"), result


def project_config(key: str, config: dict, *, reverse: bool = False) -> tuple[str, dict]:
    if key in DAILY and not reverse:
        return _project_daily(key, config)
    return _previous_revision().project_config(key, config, reverse=reverse)


def inspect(
    connection, *, lock: bool = False, reverse: bool = False
) -> tuple[list[dict], list[dict], list[dict]]:
    """64da893e5e78's report, with the FinLab daily feeds and control judged here."""
    report, dataset_updates, control_updates = _previous_revision().inspect(
        connection, lock=lock, reverse=reverse
    )
    if reverse:
        return report, dataset_updates, control_updates
    datasets = {
        row["dataset_key"]: row["config"]
        for row in connection.execute(
            sa.text(
                "SELECT dataset_key, config FROM dataset_registry WHERE dataset_key IN :keys"
            ).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": list(DAILY)},
        ).mappings()
    }
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
    dataset_updates = [row for row in dataset_updates if row["key"] not in datasets]
    control_updates = [row for row in control_updates if row["key"] not in controls]
    for row in report:
        if row["kind"] == "dataset" and row["key"] in datasets:
            status, config = _project_daily(row["key"], datasets[row["key"]])
            row["status"] = status
            if status == "known_old_default":
                dataset_updates.append({"key": row["key"], "config": json.dumps(config)})
        elif row["kind"] == "control" and row["key"] in controls:
            control = controls[row["key"]]
            zone, clock, olds = CONTROLS[PROVIDER]
            current = (control["timezone"], str(control["scheduled_local_time"]))
            row["status"] = "unknown_difference"
            if control["scheduler_key"] != SCHEDULER_KEY:
                continue
            if current == (zone, clock):
                row["status"] = "already_compliant"
            elif current in olds:
                row["status"] = "known_old_default"
                control_updates.append(
                    {"key": row["key"], "zone": zone, "clock": time.fromisoformat(clock)}
                )
    return report, dataset_updates, control_updates


def _move_intraday(source: str, target: str) -> list[dict]:
    """Stored intraday starts that move; any start other than ``source`` aborts."""
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT dataset_key, config FROM dataset_registry "
                "WHERE dataset_key IN :keys ORDER BY dataset_key FOR UPDATE"
            ).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": list(INTRADAY)},
        )
        .mappings()
    )
    updates, unknown = [], []
    for row in rows:
        config = row["config"]
        try:
            current = config["delivery_expectation"]["schedule"]["local_time"]
        except (KeyError, TypeError):
            unknown.append(row["dataset_key"])
            continue
        if current == target:
            continue
        if current != source:
            unknown.append(row["dataset_key"])
            continue
        updates.append(
            {"key": row["dataset_key"], "config": json.dumps(_with_clock(config, target))}
        )
    if unknown:
        raise RuntimeError("FinLab intraday schedule unknown differences: " + ", ".join(unknown))
    return updates


def _write(datasets: list[dict], controls: list[dict]) -> None:
    connection = op.get_bind()
    for row in datasets:
        connection.execute(
            sa.text(
                "UPDATE dataset_registry SET config = CAST(:config AS jsonb), updated_at = now() "
                "WHERE dataset_key = :key"
            ),
            row,
        )
    for row in controls:
        connection.execute(
            sa.text(
                "UPDATE scheduler_control SET timezone = :zone, "
                "scheduled_local_time = CAST(:clock AS time) WHERE scheduler_key = :key"
            ),
            row,
        )


def upgrade() -> None:
    connection = op.get_bind()
    report, datasets, controls = inspect(connection, lock=True)
    unknown = [row["key"] for row in report if row["status"] == "unknown_difference"]
    if unknown:
        raise RuntimeError("Catalog schedule preflight unknown differences: " + ", ".join(unknown))
    # Only this revision's rows move; the others keep what their own revisions wrote.
    mine = [row for row in datasets if row["key"] in DAILY]
    mine_controls = [row for row in controls if row["key"] == SCHEDULER_KEY]
    intraday = _move_intraday(OLD_INTRADAY, NEW_INTRADAY)
    _write(mine + intraday, mine_controls)


def downgrade() -> None:
    connection = op.get_bind()
    datasets, unknown = [], []
    for row in connection.execute(
        sa.text(
            "SELECT dataset_key, config FROM dataset_registry "
            "WHERE dataset_key IN :keys ORDER BY dataset_key FOR UPDATE"
        ).bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": list(DAILY)},
    ).mappings():
        try:
            current = row["config"]["delivery_expectation"]["schedule"]["local_time"]
        except (KeyError, TypeError):
            unknown.append(row["dataset_key"])
            continue
        if current == NEW_DAILY:
            datasets.append(
                {
                    "key": row["dataset_key"],
                    "config": json.dumps(_with_clock(row["config"], OLD_DAILY)),
                }
            )
        elif current != OLD_DAILY:
            unknown.append(row["dataset_key"])
    controls = []
    for row in connection.execute(
        sa.text(
            "SELECT scheduler_key, scheduled_local_time, timezone FROM scheduler_control "
            "WHERE scheduler_key = :key AND retired_at IS NULL FOR UPDATE"
        ),
        {"key": SCHEDULER_KEY},
    ).mappings():
        current = (row["timezone"], str(row["scheduled_local_time"]))
        if current == (ZONE, NEW_DAILY):
            controls.append(
                {"key": SCHEDULER_KEY, "zone": ZONE, "clock": time.fromisoformat(OLD_DAILY)}
            )
        elif current != (ZONE, OLD_DAILY):
            unknown.append(SCHEDULER_KEY)
    if unknown:
        raise RuntimeError("FinLab daily schedule unknown differences: " + ", ".join(unknown))
    intraday = _move_intraday(NEW_INTRADAY, OLD_INTRADAY)
    _write(datasets + intraday, controls)
