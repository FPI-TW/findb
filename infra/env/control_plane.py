#!/usr/bin/env python3
"""Validate and export the committed non-secret deployment control plane.

GitHub Environments hold zero variables and zero secrets: each one is only the
OIDC identity and the protection rules of a target/unit pair.  Every value the
workflow runner needs before it can reach AWS lives in these JSON files with an
identical key set for staging and production; only the values differ.
Application configuration is loaded on the host from Secrets Manager
``runtime/configuration`` and never passes through the runner.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Final

ENV_ROOT: Final = Path(__file__).resolve().parent
SCHEMA_VERSION: Final = 1
AWS_REGION: Final = "ap-southeast-1"
TARGETS: Final = ("staging", "production")
UNITS: Final = ("findb", "fetcher")
COMMON_KEYS: Final = (
    "AWS_REGION",
    "AWS_ACCOUNT_ID",
    "AWS_DEPLOY_ROLE_ARN",
    "AWS_PROMOTION_ROLE_ARN",
    "AWS_PUBLISHER_ROLE_ARN",
    "AWS_INSTANCE_PROFILE_NAME",
    "AWS_SSM_LOG_GROUP",
    "ECR_REGISTRY",
    "DEPLOY_BUNDLE_BUCKET",
    "DEPLOY_BUNDLE_KMS_KEY_ARN",
    "FINDB_PUBLIC_HOST",
    "FULL_MARKET_ENABLED",
    "STAGING_AWS_ACCOUNT_ID",
    "STAGING_PROMOTION_READ_ROLE_ARN",
    "STAGING_ECR_REGISTRY",
    "STAGING_DEPLOY_BUNDLE_BUCKET",
)
UNIT_KEYS: Final = {
    "findb": ("RDS_DB_INSTANCE_IDENTIFIER",),
    "fetcher": ("FETCHER_FULL_MARKET_STATE_DIR",),
}
# Values that both units of one target must agree on.
SHARED_KEYS: Final = (
    "AWS_REGION",
    "AWS_ACCOUNT_ID",
    "ECR_REGISTRY",
    "DEPLOY_BUNDLE_BUCKET",
    "DEPLOY_BUNDLE_KMS_KEY_ARN",
    "FINDB_PUBLIC_HOST",
    "FULL_MARKET_ENABLED",
    "STAGING_AWS_ACCOUNT_ID",
    "STAGING_ECR_REGISTRY",
    "STAGING_DEPLOY_BUNDLE_BUCKET",
)
# Promotion source values exist only where a promotion happens (production);
# image publication happens only where images are built (staging).
PROMOTION_KEYS: Final = (
    "AWS_PROMOTION_ROLE_ARN",
    "STAGING_AWS_ACCOUNT_ID",
    "STAGING_PROMOTION_READ_ROLE_ARN",
    "STAGING_ECR_REGISTRY",
    "STAGING_DEPLOY_BUNDLE_BUCKET",
)
PUBLISHER_KEYS: Final = ("AWS_PUBLISHER_ROLE_ARN",)
ACCOUNT_RE: Final = re.compile(r"^[0-9]{12}$")
HOSTNAME_RE: Final = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)
RDS_IDENTIFIER_RE: Final = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
FULL_MARKET_STATE_DIR_RE: Final = re.compile(r"^/var/lib/findb-full-market(/[a-z0-9-]+)?$")
EXPORT_KEY_RE: Final = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ControlPlaneError(ValueError):
    """The committed control plane does not satisfy its closed contract."""


def keys_for(unit: str) -> tuple[str, ...]:
    if unit not in UNITS:
        raise ControlPlaneError(f"unit invalid: {unit}")
    return (*COMMON_KEYS, *UNIT_KEYS[unit])


def path_for(target: str, unit: str, root: Path = ENV_ROOT) -> Path:
    if target not in TARGETS:
        raise ControlPlaneError(f"target invalid: {target}")
    if unit not in UNITS:
        raise ControlPlaneError(f"unit invalid: {unit}")
    return root / target / f"{unit}.json"


def _role_arn(account: str, name: str) -> str:
    return f"arn:aws:iam::{account}:role/{name}"


def _kms_key_pattern(account: str) -> re.Pattern[str]:
    return re.compile(
        rf"^arn:aws:kms:{re.escape(AWS_REGION)}:{account}:key/"
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )


def _registry(account: str) -> str:
    return f"{account}.dkr.ecr.{AWS_REGION}.amazonaws.com"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlPlaneError(message)


def _validate_values(target: str, unit: str, values: dict[str, str]) -> None:
    expected_keys = keys_for(unit)
    _require(
        tuple(sorted(values)) == tuple(sorted(expected_keys)),
        f"{target}/{unit}: variables must be exactly {sorted(expected_keys)}",
    )
    for key, value in values.items():
        _require(isinstance(value, str), f"{target}/{unit}: {key} must be a string")
        _require(
            value == value.strip() and not any(ch in value for ch in "\r\n\x00"),
            f"{target}/{unit}: {key} contains whitespace padding or control characters",
        )
    account = values["AWS_ACCOUNT_ID"]
    _require(ACCOUNT_RE.fullmatch(account) is not None, f"{target}/{unit}: AWS_ACCOUNT_ID invalid")
    checks = {
        "AWS_REGION": AWS_REGION,
        "AWS_DEPLOY_ROLE_ARN": _role_arn(account, f"{unit}-{target}-deploy"),
        "AWS_INSTANCE_PROFILE_NAME": f"{unit}-{target}-instance",
        "AWS_SSM_LOG_GROUP": f"/findb/{target}/{unit}/ssm",
        "ECR_REGISTRY": _registry(account),
        "DEPLOY_BUNDLE_BUCKET": f"findb-{target}-deploy-bundle-{account}",
    }
    for key, expected in checks.items():
        _require(values[key] == expected, f"{target}/{unit}: {key} must be {expected}")
    _require(
        _kms_key_pattern(account).fullmatch(values["DEPLOY_BUNDLE_KMS_KEY_ARN"]) is not None,
        f"{target}/{unit}: DEPLOY_BUNDLE_KMS_KEY_ARN must be a key in account {account}",
    )
    _require(
        HOSTNAME_RE.fullmatch(values["FINDB_PUBLIC_HOST"]) is not None,
        f"{target}/{unit}: FINDB_PUBLIC_HOST must be a lowercase DNS name",
    )
    _require(
        values["FULL_MARKET_ENABLED"] in {"true", "false"},
        f"{target}/{unit}: FULL_MARKET_ENABLED must be exactly true or false",
    )
    if target == "staging":
        for key in PROMOTION_KEYS:
            _require(values[key] == "", f"{target}/{unit}: {key} must be empty on staging")
        _require(
            values["AWS_PUBLISHER_ROLE_ARN"] == _role_arn(account, f"{unit}-staging-ecr-publisher"),
            f"{target}/{unit}: AWS_PUBLISHER_ROLE_ARN must be the staging ECR publisher role",
        )
    else:
        for key in PUBLISHER_KEYS:
            _require(values[key] == "", f"{target}/{unit}: {key} must be empty on production")
        _require(
            values["AWS_PROMOTION_ROLE_ARN"] == _role_arn(account, f"{unit}-production-promotion"),
            f"{target}/{unit}: AWS_PROMOTION_ROLE_ARN must be the production promotion role",
        )
        staging_account = values["STAGING_AWS_ACCOUNT_ID"]
        _require(
            ACCOUNT_RE.fullmatch(staging_account) is not None and staging_account != account,
            f"{target}/{unit}: STAGING_AWS_ACCOUNT_ID must be a different 12-digit account",
        )
        promotion_checks = {
            "STAGING_PROMOTION_READ_ROLE_ARN": _role_arn(
                staging_account, f"{unit}-staging-promotion-reader"
            ),
            "STAGING_ECR_REGISTRY": _registry(staging_account),
            "STAGING_DEPLOY_BUNDLE_BUCKET": f"findb-staging-deploy-bundle-{staging_account}",
        }
        for key, expected in promotion_checks.items():
            _require(values[key] == expected, f"{target}/{unit}: {key} must be {expected}")
    if unit == "findb":
        _require(
            RDS_IDENTIFIER_RE.fullmatch(values["RDS_DB_INSTANCE_IDENTIFIER"]) is not None,
            f"{target}/{unit}: RDS_DB_INSTANCE_IDENTIFIER invalid",
        )
    else:
        _require(
            FULL_MARKET_STATE_DIR_RE.fullmatch(values["FETCHER_FULL_MARKET_STATE_DIR"]) is not None,
            f"{target}/{unit}: FETCHER_FULL_MARKET_STATE_DIR must be below /var/lib/findb-full-market",
        )


def load(target: str, unit: str, root: Path = ENV_ROOT) -> dict[str, str]:
    """Load and validate one committed control-plane file."""
    path = path_for(target, unit, root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlPlaneError(f"{path}: unreadable or invalid JSON") from exc
    _require(
        isinstance(document, dict)
        and set(document) == {"schema_version", "deployment_target", "unit", "variables"},
        f"{path}: document keys must be schema_version, deployment_target, unit, variables",
    )
    _require(document["schema_version"] == SCHEMA_VERSION, f"{path}: schema_version invalid")
    _require(document["deployment_target"] == target, f"{path}: deployment_target mismatch")
    _require(document["unit"] == unit, f"{path}: unit mismatch")
    variables = document["variables"]
    _require(isinstance(variables, dict), f"{path}: variables must be an object")
    _validate_values(target, unit, variables)
    return dict(variables)


def validate_all(root: Path = ENV_ROOT) -> dict[tuple[str, str], dict[str, str]]:
    """Validate every file and the cross-file identity rules."""
    loaded = {(target, unit): load(target, unit, root) for target in TARGETS for unit in UNITS}
    for target in TARGETS:
        findb, fetcher = loaded[(target, "findb")], loaded[(target, "fetcher")]
        for key in SHARED_KEYS:
            _require(
                findb[key] == fetcher[key], f"{target}: {key} differs between findb and fetcher"
            )
    staging, production = loaded[("staging", "findb")], loaded[("production", "findb")]
    _require(
        staging["AWS_ACCOUNT_ID"] != production["AWS_ACCOUNT_ID"],
        "staging and production must use different AWS accounts",
    )
    cross = {
        "STAGING_AWS_ACCOUNT_ID": "AWS_ACCOUNT_ID",
        "STAGING_ECR_REGISTRY": "ECR_REGISTRY",
        "STAGING_DEPLOY_BUNDLE_BUCKET": "DEPLOY_BUNDLE_BUCKET",
    }
    for production_key, staging_key in cross.items():
        _require(
            production[production_key] == staging[staging_key],
            f"production {production_key} must equal staging {staging_key}",
        )
    for unit in UNITS:
        _require(
            sorted(loaded[("staging", unit)]) == sorted(loaded[("production", unit)]),
            f"{unit}: staging and production must declare the same variable names",
        )
    return loaded


def export_lines(values: dict[str, str]) -> list[str]:
    """Render ``KEY=VALUE`` lines suitable for ``$GITHUB_ENV``."""
    lines = []
    for key in sorted(values):
        _require(EXPORT_KEY_RE.fullmatch(key) is not None, f"export key invalid: {key}")
        lines.append(f"{key}={values[key]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ENV_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--target", choices=TARGETS, required=True)
    export_parser.add_argument("--unit", choices=UNITS, required=True)
    args = parser.parse_args(argv)
    try:
        loaded = validate_all(args.root)
        if args.command == "export":
            sys.stdout.write("\n".join(export_lines(loaded[(args.target, args.unit)])) + "\n")
        else:
            print(f"control_plane=valid files={len(loaded)}")
    except ControlPlaneError as exc:
        print(f"control_plane=failed reason={exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
