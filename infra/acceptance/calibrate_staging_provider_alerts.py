#!/usr/bin/env python3
"""Calibrate staging provider anomaly alarms from a retained real delivery.

Run only in the Twelve Data staging scheduler container. The script clones the
latest durable prepared request, gives each clone a new idempotency identity,
and submits two controlled mutations: a missing required ``close`` field and
an empty snapshot. It never calls the provider or alters scheduler state.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


def _load_retained_request() -> dict[str, Any]:
    injected = os.getenv("FINDB_CALIBRATION_REQUEST_B64")
    if injected:
        value = json.loads(base64.b64decode(injected, validate=True))
        if not isinstance(value, dict) or not value.get("payload", {}).get("data"):
            raise RuntimeError("injected delivery is not a non-empty request object")
        return value
    path = Path(os.getenv("FETCHER_STATE_PATH", "/var/lib/findb-fetcher/state.sqlite3"))
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        row = db.execute(
            """
            SELECT prepared_request
            FROM scheduled_job
            WHERE provider = 'twelve_data'
              AND dataset_key = 'us_equity_eod'
              AND status = 'completed'
              AND prepared_request IS NOT NULL
            ORDER BY target_data_date DESC, completed_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise RuntimeError("no retained completed Twelve Data delivery is available")
    value = json.loads(row[0])
    if not isinstance(value, dict) or not value.get("payload", {}).get("data"):
        raise RuntimeError("retained delivery is not a non-empty request object")
    return value


def _identity(request: dict[str, Any], kind: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-staging-cal-{kind}-{stamp}"
    request["request_key"] = f"{str(request['request_key'])[: 100 - len(suffix)]}{suffix}"
    request["idempotency_key"] = f"{str(request['idempotency_key'])[: 100 - len(suffix)]}{suffix}"
    request["fetched_at"] = datetime.now(timezone.utc).isoformat()


def _post(request: dict[str, Any]) -> dict[str, Any]:
    url = os.environ["SOURCE_API_URL"].rstrip("/") + "/api/v1/source/ingest"
    response = httpx.post(
        url,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": os.environ["SOURCE_CLIENT_KEY"],
            "Idempotency-Key": request["idempotency_key"],
        },
        content=json.dumps(request, separators=(",", ":"), sort_keys=True),
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Source returned a non-JSON calibration response") from exc
    return {
        "http_status": response.status_code,
        "attempt_id": body.get("attempt_id"),
        "run_id": body.get("run_id"),
        "error_code": body.get("error", {}).get("code"),
    }


def main() -> int:
    retained = _load_retained_request()

    missing_field = deepcopy(retained)
    _identity(missing_field, "missing-close")
    del missing_field["payload"]["data"][0]["close"]
    missing_result = _post(missing_field)
    if (
        missing_result["http_status"] != 422
        or missing_result["error_code"] != "INGRESS_SCHEMA_INVALID"
    ):
        raise RuntimeError("missing-field calibration did not produce the expected rejection")

    empty_snapshot = deepcopy(retained)
    _identity(empty_snapshot, "empty")
    empty_snapshot["payload"]["data"] = []
    empty_snapshot["payload"]["batch"]["declared_record_count"] = 0
    empty_result = _post(empty_snapshot)
    if empty_result["http_status"] != 202 or empty_result["run_id"] is None:
        raise RuntimeError("empty-snapshot calibration was not accepted as a governed warning")

    print(
        json.dumps(
            {
                "source": "twelve_data",
                "dataset_key": "us_equity_eod",
                "fixture": "latest_retained_real_prepared_delivery",
                "missing_required_field": missing_result,
                "empty_snapshot": empty_result,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
