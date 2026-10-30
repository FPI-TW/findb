"""Quiescent Owner preview/apply. Never instantiate runtime, SDK or SQLite writers."""

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx

from findb_fetcher.config import FetcherConfig, app_environment
from findb_fetcher.full_market_client import FullMarketSourceClient
from findb_fetcher.provider_work import host_lock


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read(path: Path):
    if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
        raise ValueError("existing regular calendar checkpoint required")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    return closing(db)


def _row(row):
    return {
        k: {"sha256": hashlib.sha256(v).hexdigest(), "bytes": len(v)} if isinstance(v, bytes) else v
        for k, v in dict(row).items()
    }


def checkpoint_facts(state: Path, account: Path, inventory: dict, *, now: float) -> dict:
    """The caller must retain the shared host lock through Owner response."""
    wid, pid, profile = inventory["work_id"], inventory["plan_id"], inventory["execution_profile"]
    facts = {
        "environment": app_environment("local"),
        "execution_profile": profile,
        "work_id": wid,
        "plan_id": pid,
        "checksums_valid": True,
    }
    with _read(state) as db:
        identity = db.execute("SELECT profile FROM execution_identity WHERE id=1").fetchone()
        if identity is None or identity[0] != profile:
            raise ValueError("calendar checkpoint profile differs")
        plans = list(db.execute("SELECT * FROM full_plan WHERE plan_id=?", (pid,)))
        members = list(db.execute("SELECT * FROM full_work WHERE plan_id=? ORDER BY key", (pid,)))
        work = inventory["snapshot"]["work"][0]
        if (
            len(plans) != 1
            or not members
            or plans[0]["provider"] != "shioaji"
            or plans[0]["dataset"] != work["dataset_key"]
            or plans[0]["trade_date"] != work["trade_date"]
        ):
            raise ValueError("calendar checkpoint plan missing or differs")
        rows = [_row(r) for r in members]
        facts["member_keys"] = [r["member_key"] for r in members]
        facts["prepared_count"] = sum(r["body"] is not None for r in members)
        facts["active_leases"] = sum(r["lease_until"] > now for r in members)
        receipts = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM full_work_receipt WHERE key IN (SELECT key FROM full_work WHERE plan_id=?) ORDER BY key,idempotency_key",
                (pid,),
            )
        ]
        facts["receipt_count"] = len(receipts) + sum(r["receipt"] is not None for r in members)
        facts["local"] = {
            "plans": [_row(r) for r in plans],
            "members": rows,
            "receipts": receipts,
            "repairs": [
                _row(r)
                for r in db.execute(
                    "SELECT * FROM full_member_repair WHERE work_key IN (SELECT key FROM full_work WHERE plan_id=?) ORDER BY repair_id",
                    (pid,),
                )
            ],
            "empty_outcomes": [
                _row(r)
                for r in db.execute(
                    "SELECT * FROM full_empty_outcome WHERE work_key IN (SELECT key FROM full_work WHERE plan_id=?) ORDER BY work_key",
                    (pid,),
                )
            ],
        }
    with _read(account.parent / "provider-work.sqlite3") as db:
        manifest = db.execute("SELECT body FROM work_manifest WHERE work_key=?", (wid,)).fetchone()
        if manifest is None:
            raise ValueError("original journal manifest required")
        facts["manifest_sha256"] = digest(json.loads(manifest[0]))
        journal = {}
        for table in [
            "work_manifest",
            "work_delivery",
            "work_job",
            "work_budget",
            "work_response",
            "work_response_observation",
            "work_shioaji_grant",
            "work_shioaji_probe",
        ]:
            raw = list(db.execute(f"SELECT * FROM {table} WHERE work_key=? ORDER BY rowid", (wid,)))
            journal[table] = [_row(r) for r in raw]
            if table in {"work_response", "work_response_observation"}:
                for r in raw:
                    if hashlib.sha256(bytes(r["body"])).hexdigest() != r["sha256"]:
                        raise ValueError("calendar response checksum differs")
        facts["journal_deliveries"] = len(journal["work_delivery"])
        facts["journal_jobs"] = len(journal["work_job"])
        facts["unknown_probes"] = sum(
            r["outcome"] == "unknown" for r in journal["work_shioaji_probe"]
        )
        facts["journal"] = journal
    with _read(account) as db:
        intents = [
            _row(r)
            for r in db.execute(
                "SELECT * FROM acquisition_intent WHERE provider='shioaji' ORDER BY operation_id"
            )
        ]
        facts["active_intents"] = sum(
            r["status"] not in {"finalized", "cancelled"} for r in intents
        )
        facts["governor_intents_sha256"] = digest(intents)
    return facts


def run(args, account: Path) -> dict:
    if args.provider != "shioaji" or not args.work_id or not args.closure_proposal:
        raise ValueError("calendar closure requires Shioaji --work-id and --closure-proposal")
    if args.closure_proposal.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("calendar closure proposal exceeds bound")
    proposal = json.loads(args.closure_proposal.read_bytes())
    if args.apply and not args.expected_sha256:
        raise ValueError("calendar --apply requires --expected-sha256")
    env = app_environment("local")
    lock = account.parent / f"provider-work-{env}-shioaji.lock"
    if not lock.is_file() or lock.is_symlink() or lock.parent.is_symlink():
        raise ValueError("existing shared provider lock required; preview never initializes state")
    config = FetcherConfig.from_env()
    key = os.getenv("FINDB_ADMIN_API_KEY", "").strip()
    if not key:
        raise ValueError("Owner FINDB_ADMIN_API_KEY required")
    wid = str(UUID(args.work_id))
    with host_lock("shioaji", root=account.parent) as (_, inode):
        with FullMarketSourceClient(config, execution_profile=args.execution_profile) as source:
            current = source.request("GET", f"provider-work/{wid}/calendar-closure")
        if current["environment"] != env or current["execution_profile"] != args.execution_profile:
            raise ValueError("calendar closure runtime scope differs")
        if args.apply:
            body = {**proposal["request"], "expected_sha256": args.expected_sha256}
            # After ACK loss, the retained Backend record is the receipt. Never
            # mint a new approval because unrelated governor history advanced.
            replay = any(
                row.get("work_id") == wid
                and row.get("request_id") == body["request_id"]
                and row.get("request_sha256") == digest(body)
                for row in current["snapshot"].get("closure", [])
            )
            if not replay:
                facts = checkpoint_facts(args.state_path, account, current, now=time.time())
                if (
                    body["checkpoint"]["sha256"] != digest(facts)
                    or body["checkpoint"]["host_lock_identity"] != inode
                ):
                    raise ValueError("calendar checkpoint preview is stale")
        else:
            facts = checkpoint_facts(args.state_path, account, current, now=time.time())
            if set(proposal) != {"request_id", "reason"}:
                raise ValueError("calendar preview proposal requires request_id and reason only")
            body = {
                **proposal,
                "environment": env,
                "execution_profile": args.execution_profile,
                "plan_id": current["plan_id"],
                "checkpoint": {
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "host_lock_identity": inode,
                    "inventory_sha256": current["sha256"],
                    "facts": facts,
                    "sha256": digest(facts),
                },
            }
        suffix = "" if args.apply else "/preview"
        with httpx.Client(timeout=config.request_timeout_seconds) as client:
            response = client.post(
                config.source_api_url
                + f"/api/v1/admin/provider-work/{wid}/calendar-closure"
                + suffix,
                json=body,
                headers={"X-API-Key": key},
            )
            if response.status_code != 200:
                raise ValueError(
                    f"Owner calendar closure API refused request: HTTP {response.status_code}"
                )
            return response.json()
