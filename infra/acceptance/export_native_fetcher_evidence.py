"""Read profile-bound checkpoints and verify selected native raw objects; never acquire."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig


def governor_evidence(provider: str) -> dict[str, Any]:
    """Observe shared account usage/liability without starting another SDK session."""
    path = Path(os.environ["FETCHER_ACCOUNT_STATE_PATH"])
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        return {
            "scope": "shared_environment_account",
            "liabilities": [
                dict(row)
                for row in db.execute(
                    "SELECT operation_kind,status,count(*) AS count FROM acquisition_intent WHERE provider=? AND status IN ('armed','unknown') GROUP BY operation_kind,status",
                    (provider,),
                )
            ],
            "recent_operations": [
                dict(row)
                for row in db.execute(
                    "SELECT operation_kind,status,outcome,usage_before_state,armed_at,actual_start,completed_at,cooldown_bound FROM acquisition_intent WHERE provider=? ORDER BY armed_at DESC LIMIT 20",
                    (provider,),
                )
            ],
            "official_usage": [
                dict(row)
                for row in db.execute(
                    "SELECT u.observed_at,u.bytes,u.limit_bytes,u.remaining_bytes,u.connections FROM acquisition_usage u JOIN acquisition_intent i USING(operation_id) WHERE i.provider=? ORDER BY u.observed_at DESC LIMIT 4",
                    (provider,),
                )
            ],
        }


def export(spec: dict[str, Any]) -> dict[str, Any]:
    environment, profile, provider = (
        spec["environment"],
        spec["execution_profile"],
        spec["provider"],
    )
    if (
        environment not in ("staging", "production")
        or profile not in ("pilot", "full_market")
        or provider not in ("twelve_data", "finlab", "shioaji", "taifex", "factset")
        or environment != os.environ["APP_ENVIRONMENT"]
        or profile != os.environ["FETCHER_CONSUMER_PROFILE"]
    ):
        raise ValueError("runtime identity mismatch")
    path = Path(os.environ["FETCHER_STATE_PATH"])
    config = RawStorageConfig.from_env()
    client = R2RawPayloadStore(config)._client
    result: dict[str, Any] = {
        "environment": environment,
        "execution_profile": profile,
        "provider": provider,
        "plans": [],
    }
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("SELECT profile FROM execution_identity WHERE id=1").fetchone()[0] != profile:
            raise ValueError("checkpoint identity mismatch")
        for target in spec["plans"]:
            plan = db.execute(
                "SELECT * FROM full_plan WHERE plan_id=? AND provider=?",
                (target["plan_id"], provider),
            ).fetchone()
            if plan is None:
                result["plans"].append(
                    {"plan_id": target["plan_id"], "reason": "checkpoint_missing"}
                )
                continue
            frozen = json.loads(plan["body"])
            proof = {
                key: frozen[key]
                for key in (
                    "plan_id",
                    "dataset_key",
                    "trade_date",
                    "release_id",
                    "execution_profile",
                )
            }
            proof["parts_sha256"] = hashlib.sha256(canonical_bytes(frozen["parts"])).hexdigest()
            if any(proof[key] != target[key] for key in proof):
                raise ValueError("frozen plan identity mismatch")
            proof["work"] = [
                dict(row)
                for row in db.execute(
                    "SELECT status, count(*) AS count FROM full_work WHERE plan_id=? GROUP BY status",
                    (target["plan_id"],),
                )
            ]
            proof["sample"] = None
            rows = db.execute(
                "SELECT key,member_key,status,body,body_sha256,receipt FROM full_work WHERE plan_id=? AND provider=? AND receipt=?",
                (target["plan_id"], provider, target["run_id"]),
            ).fetchall()
            if len(rows) == 1:
                row = rows[0]
                body = bytes(row["body"] or b"")
                if (
                    len(body) > 1024 * 1024
                    or hashlib.sha256(body).hexdigest() != row["body_sha256"]
                ):
                    raise ValueError("prepared body integrity mismatch")
                requests = json.loads(body)
                matching = [
                    r for r in requests if r["idempotency_key"] == target["idempotency_key"]
                ]
                if len(matching) != 1:
                    raise ValueError("prepared request identity mismatch")
                request = matching[0]
                if (
                    request["source"] != provider
                    or request["dataset_key"] != target["dataset_key"]
                    or row["member_key"] != target["member_key"]
                    or request["delivery"]["work_item_id"] != target["work_item_id"]
                    or row["key"] != target["work_item_id"] + ":" + target["member_key"]
                ):
                    raise ValueError("prepared member scope mismatch")
                if (
                    hashlib.sha256(canonical_bytes(request)).hexdigest()
                    != target["source_request_sha256"]
                ):
                    raise ValueError("Source request checksum mismatch")
                batch = request["payload"]["batch"]
                ref, digest = batch["source_raw_ref"], batch["source_raw_sha256"]
                if ref != target["source_raw_ref"] or digest != target["source_raw_sha256"]:
                    raise ValueError("Source raw reference mismatch")
                parsed = urlsplit(ref)
                bucket, separator, key = parsed.path.removeprefix("/").partition("/")
                if (
                    parsed.scheme != "r2"
                    or parsed.netloc != config.account_id
                    or bucket != config.bucket
                    or not separator
                    or parsed.query
                    or parsed.fragment
                    or not key.startswith(f"{provider}/{target['dataset_key']}/")
                    or key.rsplit("/", 1)[-1] != digest + ".json"
                ):
                    raise ValueError("raw object identity mismatch")
                response = client.get_object(Bucket=bucket, Key=key)
                stream = response["Body"]
                try:
                    length = response["ContentLength"]
                    if type(length) is not int or not 0 < length <= config.max_object_bytes:
                        raise ValueError("raw object size invalid")
                    raw = stream.read(length + 1)
                    if (
                        len(raw) != length
                        or hashlib.sha256(raw).hexdigest() != digest
                        or response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                    ):
                        raise ValueError("raw object integrity mismatch")
                finally:
                    stream.close()
                proof["sample"] = {
                    "work_key": row["key"],
                    "member_key": row["member_key"],
                    "status": row["status"],
                    "body_sha256": row["body_sha256"],
                    "run_id": row["receipt"],
                    "idempotency_key": request["idempotency_key"],
                    "source_request_sha256": hashlib.sha256(canonical_bytes(request)).hexdigest(),
                    "work_item_id": request["delivery"]["work_item_id"],
                    "source_raw_ref": ref,
                    "source_raw_sha256": digest,
                    "verified_raw_bytes": length,
                    "raw_verified": True,
                }
            result["plans"].append(proof)
    result["governor"] = governor_evidence(provider)
    return result


def main() -> None:
    try:
        spec = json.loads(os.environ["FINDB_NATIVE_EVIDENCE_SPEC"])
        print(json.dumps(export(spec), sort_keys=True, separators=(",", ":")))
    except Exception:
        raise SystemExit("native fetcher evidence export failed") from None


if __name__ == "__main__":
    main()
