#!/usr/bin/env python3
"""Export bounded, non-secret scheduler evidence from one Fetcher container.

This file is designed to be piped to ``python -`` inside a provider container.
It performs read-only access to the mounted config and SQLite state files and
prints exactly one compact JSON object.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _daily_provider(provider: str) -> dict[str, Any]:
    # Retired checkpoint reader only: no installed schedule or image fallback.
    # The caller must explicitly supply preserved historical evidence.
    schedule_path = Path(os.environ["FETCHER_SCHEDULE_FILE"])
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    feed = next(item for item in schedule["feeds"] if item["provider"] == provider)
    universe_path = schedule_path.parent / feed["universe_file"]
    universe = json.loads(universe_path.read_text(encoding="utf-8"))
    state_env = "FETCHER_STATE_PATH" if provider == "twelve_data" else "FETCHER_FINLAB_STATE_PATH"
    default_state = (
        "/var/lib/findb-fetcher/staging-pilot-v2/state.sqlite3"
        if provider == "twelve_data"
        else "/var/lib/findb-finlab-fetcher/state.sqlite3"
    )
    state_path = Path(os.getenv("FETCHER_STATE_PATH", os.getenv(state_env, default_state)))
    with _open_read_only(state_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """
            SELECT target_data_date, scheduled_date, status, last_outcome,
                   group_concat(run_id) AS run_ids, count(*) AS work_items,
                   sum(attempt_count) AS provider_attempts,
                   coalesce(sum(record_count), 0) AS record_count,
                   sum(CASE WHEN attempt_id IS NOT NULL AND run_id IS NOT NULL THEN 1 ELSE 0 END)
                       AS source_receipts,
                   sum(CASE WHEN checkpoint_before IS NOT NULL THEN 1 ELSE 0 END)
                       AS pre_checkpoints,
                   sum(CASE WHEN checkpoint_after IS NOT NULL THEN 1 ELSE 0 END)
                       AS post_checkpoints,
                   sum(CASE WHEN checkpoint_after IS NOT NULL
                                 AND checkpoint_after IS NOT checkpoint_before
                            THEN 1 ELSE 0 END) AS checkpoint_advances
            FROM scheduled_job
            WHERE provider = ? AND dataset_key = ?
            GROUP BY target_data_date, scheduled_date, status, last_outcome
            ORDER BY target_data_date DESC, status
            """,
            (provider, feed["dataset_key"]),
        ).fetchall()
    selected_dates = sorted({str(row["target_data_date"]) for row in rows}, reverse=True)[:2]
    history = [dict(row) for row in rows if str(row["target_data_date"]) in selected_dates]
    symbols = universe.get("symbols", [])
    if provider == "twelve_data":
        batch_size = int(universe.get("limits", {}).get("max_symbols_per_run", len(symbols)))
        estimated_credits: int | None = min(len(symbols), batch_size) * int(
            universe["credit_cost_per_symbol"]
        )
        universe_id = universe["universe_id"]
    else:
        estimated_credits = None
        universe_id = universe["manifest_id"]
    return {
        "provider": provider,
        "datasets": [feed["dataset_key"]],
        "config": {
            "schedule_sha256": _sha256(schedule_path),
            "universe_sha256": _sha256(universe_path),
            "schedule": {
                **{
                    key: feed[key]
                    for key in (
                        "slot_id",
                        "scheduled_time",
                        "target_date_policy",
                        "max_credits_per_run",
                        "max_records_per_run",
                        "enabled",
                    )
                },
                "timezone": schedule["timezone"],
            },
            "universe_id": universe_id,
            "symbols": [
                str(item.get("canonical_symbol") or item.get("symbol")) for item in symbols
            ],
        },
        "credits": {
            "kind": "provider_credit" if estimated_credits is not None else "not_applicable",
            "estimated_per_run": estimated_credits,
            "configured_cap": feed["max_credits_per_run"],
            "reason": None if estimated_credits is not None else "provider_not_credit_metered",
        },
        "recent_trade_dates": history,
    }


def _taifex() -> dict[str, Any]:
    config_path = Path("/app/configs/taifex_tw_staging_pilot.v1.json")
    state_path = Path(
        os.getenv(
            "FETCHER_STATE_PATH",
            "/var/lib/findb-taifex-fetcher/staging-pilot-v1/state.sqlite3",
        )
    )
    with _open_read_only(state_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT target_date,status,attempts,requests,run_id,attempt_id,record_count,prepared,last_outcome FROM pilot_day ORDER BY target_date DESC LIMIT 2"
        ).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        prepared = json.loads(item.pop("prepared")) if item.get("prepared") else None
        item["actual_contracts"] = (
            [
                {
                    "contract_code": record["contract_code"],
                    "contract_month": record["contract_month"],
                    "session": record["session"],
                    "trade_date": record["trade_date"],
                }
                for record in prepared["payload"]["data"]
            ]
            if prepared
            else []
        )
        history.append(item)
    return {
        "provider": "taifex",
        "datasets": ["tw_futures_eod"],
        "config": {
            "manifest_sha256": _sha256(config_path),
            "pilot": json.loads(config_path.read_bytes()),
        },
        "credits": {"kind": "not_applicable", "reason": "public_exchange_report"},
        "recent_trade_dates": history,
    }


def main() -> int:
    provider = os.environ.get("FINDB_EVIDENCE_PROVIDER")
    if provider in {"twelve_data", "finlab"}:
        result = _daily_provider(provider)
    elif provider == "taifex":
        result = _taifex()
    else:
        raise SystemExit("FINDB_EVIDENCE_PROVIDER must be twelve_data, finlab, or taifex")
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit("Legacy checkpoint CLI retired; use export_native_fetcher_evidence.py")
