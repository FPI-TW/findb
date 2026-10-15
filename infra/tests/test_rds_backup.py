"""Pre-migration backup evidence must bind the selected RDS identity."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "rds_backup", Path(__file__).resolve().parents[1] / "deploy/check_rds_backup.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


@pytest.mark.parametrize(
    "target,account", [("staging", "439622209937"), ("production", "289112218471")]
)
@pytest.mark.parametrize("fault", [None, "account", "disabled", "stale", "future", "missing"])
def test_backup_preflight(target, account, fault):
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    identifier = f"findb-{target}"
    row = {
        "DBInstanceIdentifier": identifier,
        "DBInstanceArn": f"arn:aws:rds:ap-southeast-1:{account}:db:{identifier}",
        "DBInstanceStatus": "available",
        "BackupRetentionPeriod": 7,
        "LatestRestorableTime": (now - timedelta(minutes=5)).isoformat(),
        "Endpoint": {"Address": "database.ap-southeast-1.rds.amazonaws.com"},
    }
    if fault == "account":
        row["DBInstanceArn"] = row["DBInstanceArn"].replace(account, "000000000000")
    elif fault == "disabled":
        row["BackupRetentionPeriod"] = 0
    elif fault == "stale":
        row["LatestRestorableTime"] = (now - timedelta(hours=2)).isoformat()
    elif fault == "future":
        row["LatestRestorableTime"] = (now + timedelta(minutes=1)).isoformat()
    elif fault == "missing":
        row.pop("LatestRestorableTime")
    if fault:
        with pytest.raises((ValueError, KeyError)):
            module.validate({"DBInstances": [row]}, account, identifier, now)
    else:
        assert (
            module.validate({"DBInstances": [row]}, account, identifier, now)
            == row["Endpoint"]["Address"]
        )
