"""Require a recent recoverable RDS backup before any schema mutation."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def validate(value: dict, account: str, identifier: str, now: datetime) -> str:
    rows = value.get("DBInstances", [])
    if len(rows) != 1:
        raise ValueError("rds_identity_invalid")
    row = rows[0]
    if (
        row.get("DBInstanceIdentifier") != identifier
        or row.get("DBInstanceArn") != f"arn:aws:rds:ap-southeast-1:{account}:db:{identifier}"
        or row.get("DBInstanceStatus") != "available"
        or row.get("BackupRetentionPeriod", 0) < 1
    ):
        raise ValueError("rds_backup_unavailable")
    restored = datetime.fromisoformat(row["LatestRestorableTime"].replace("Z", "+00:00"))
    if restored.tzinfo is None or not 0 <= (now - restored).total_seconds() <= 3600:
        raise ValueError("rds_recovery_point_stale")
    endpoint = row["Endpoint"]["Address"]
    if not isinstance(endpoint, str) or not endpoint.endswith(".rds.amazonaws.com"):
        raise ValueError("rds_endpoint_invalid")
    return endpoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--identifier", required=True)
    args = parser.parse_args()
    try:
        endpoint = validate(
            json.loads(args.input.read_text()),
            args.account,
            args.identifier,
            datetime.now(timezone.utc),
        )
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("rds_backup=failed") from None
    print(endpoint)


if __name__ == "__main__":
    main()
