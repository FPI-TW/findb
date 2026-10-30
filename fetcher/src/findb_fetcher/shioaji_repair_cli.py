"""Read-only local diagnostics and explicit Owner API approvals; never SDK I/O."""

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

import httpx

from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import FullMarketSourceClient


def read_checkpoint(state_path: Path, journal_path: Path, inventory: dict) -> dict:
    def connect(path):
        if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
            raise ValueError("existing regular checkpoint required")
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return closing(db)

    with connect(state_path) as db:
        identity = db.execute("SELECT profile FROM execution_identity WHERE id=1").fetchone()
        if identity is None or identity[0] != inventory["execution_profile"]:
            raise ValueError("checkpoint execution profile differs")
        local = {
            row["member_key"]: dict(row)
            for row in db.execute(
                "SELECT key,member_key,status,attempts,reason,next_at,body,body_sha256,receipt FROM full_work WHERE plan_id=?",
                (inventory["plan_id"],),
            )
        }
        for row in local.values():
            body = row.pop("body")
            if body is not None and hashlib.sha256(bytes(body)).hexdigest() != row["body_sha256"]:
                raise ValueError("prepared body checksum differs")
        receipts: dict[str, list[dict]] = {row["key"]: [] for row in local.values()}
        for key in receipts:
            receipts[key] = [
                dict(row)
                for row in db.execute(
                    "SELECT idempotency_key,run_id FROM full_work_receipt WHERE key=?", (key,)
                )
            ]
    with connect(journal_path) as db:
        responses = []
        for table, origin in (
            ("work_response", "cache"),
            ("work_response_observation", "observation"),
        ):
            for row in db.execute(
                f"SELECT response_key,body,sha256 FROM {table} WHERE work_key=?",
                (inventory["work_id"],),
            ):
                if hashlib.sha256(bytes(row["body"])).hexdigest() != row["sha256"]:
                    raise ValueError("response checksum differs")
                entry = {
                    "response_key": row["response_key"],
                    "sha256": row["sha256"],
                    "origin": origin,
                }
                snapshot = json.loads(row["body"])
                if snapshot.get("type") == "ShioajiKbarsSnapshot":
                    entry["vector_lengths"] = {
                        key: len(value) for key, value in snapshot["data"]["kbars"].items()
                    }
                responses.append(entry)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        probes = (
            [
                dict(row)
                for row in db.execute(
                    "SELECT probe_id,operation_key,grant_id,reserved_at,outcome,diagnostic FROM work_shioaji_probe WHERE work_key=? ORDER BY probe_id",
                    (inventory["work_id"],),
                )
            ]
            if "work_shioaji_probe" in tables
            else []
        )
    members = [
        {
            **member,
            "local": local.get(member["member_key"]),
            "sequence_receipts": receipts.get(
                local.get(member["member_key"], {}).get("key", ""), []
            ),
        }
        for member in inventory["members"]
    ]
    for member in members:
        identity = json.dumps(
            {"symbol": member["provider_symbol"], "target_date": inventory["trade_date"]},
            sort_keys=True,
            separators=(",", ":"),
        )
        key = "shioaji_kbars:" + hashlib.sha256(identity.encode()).hexdigest()
        member["responses"] = [row for row in responses if row["response_key"].startswith(key)]
        member["proposed_action"] = (
            "reconcile_original_delivery"
            if member["has_prepared"]
            else "collect_mapping_evidence"
            if member["mapping_status"] == "gap"
            else "review_observation_grant"
            if member["reason"] in {"empty_response", "source_error"}
            else "none"
        )
    report = {
        "backend": inventory,
        "members": members,
        "responses": responses,
        "observations": probes,
        "cause": "unknown",
    }
    report["sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return report


def run(args, account_path: Path) -> dict:
    if args.provider != "shioaji" or not args.work_id:
        raise ValueError("Shioaji repair modes require --provider shioaji and --work-id")
    work_id = str(UUID(args.work_id))
    config = FetcherConfig.from_env()
    with FullMarketSourceClient(config, execution_profile=args.execution_profile) as source:
        inventory = source.request("GET", f"provider-work/{work_id}/shioaji-diagnostics")
    if inventory["execution_profile"] != args.execution_profile:
        raise ValueError("repair execution profile differs")
    report = read_checkpoint(
        args.state_path, account_path.parent / "provider-work.sqlite3", inventory
    )
    if args.diagnose_shioaji:
        return report
    if not args.repair_proposal or args.repair_proposal.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("bounded --repair-proposal JSON file required")
    body = json.loads(args.repair_proposal.read_bytes())
    # Bind approval to the reviewed local report as well as Backend state.
    if body.pop("checkpoint_sha256", None) != report["sha256"] and not (
        args.apply and body.get("request_id") in inventory.get("approved_request_ids", [])
    ):
        raise ValueError("repair local checkpoint preview is stale")
    admin_key = os.environ.get("FINDB_ADMIN_API_KEY", "").strip()
    if not admin_key:
        raise ValueError("Owner FINDB_ADMIN_API_KEY is required")
    if args.apply:
        if not args.expected_sha256:
            raise ValueError("--apply requires --expected-sha256 from preview")
        body["expected_sha256"] = args.expected_sha256
    suffix = "" if args.apply else "/preview"
    url = config.source_api_url + f"/api/v1/admin/provider-work/{work_id}/shioaji-repairs" + suffix
    with httpx.Client(timeout=config.request_timeout_seconds) as client:
        response = client.post(url, json=body, headers={"X-API-Key": admin_key})
        if response.status_code != 200:
            # No server response text or credential-bearing exception is printed.
            raise ValueError(f"Owner repair API refused request: HTTP {response.status_code}")
        return {"checkpoint_sha256": report["sha256"], **response.json()}
