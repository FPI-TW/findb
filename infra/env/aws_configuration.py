"""Validate/synchronize AWS deployment configuration without application secret access."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


class ConfigurationError(ValueError):
    pass


def bootstrap(target: str, unit: str) -> dict[str, str]:
    document = json.loads((ROOT / "bootstrap.json").read_text())
    if document["schema_version"] != 1 or target not in document["targets"]:
        raise ConfigurationError("bootstrap_target_invalid")
    values = document["targets"][target]
    if unit not in values["units"]:
        raise ConfigurationError("bootstrap_unit_invalid")
    return {
        "AWS_ACCOUNT_ID": values["account"],
        "AWS_REGION": values["region"],
        "AWS_DEPLOY_ROLE_ARN": values["units"][unit]["role"],
        "CONFIGURATION_PATH": values["units"][unit]["parameter"],
    }


def validate(document: Any, *, target: str, unit: str) -> dict[str, str]:
    schema = json.loads((ROOT / "deployment.schema.json").read_text())
    route = bootstrap(target, unit)
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "deployment_target",
        "unit",
        "values",
    }:
        raise ConfigurationError("configuration_shape_invalid")
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != schema["schema_version"]
        or document["deployment_target"] != target
        or document["unit"] != unit
    ):
        raise ConfigurationError("configuration_identity_invalid")
    fields = schema["units"][unit]
    values = document["values"]
    if (
        not isinstance(values, dict)
        or set(values) - set(fields["required"]) - set(fields["defaults"])
        or set(fields["required"]) - set(values)
    ):
        raise ConfigurationError("configuration_fields_invalid")
    result = {**fields["defaults"], **values}
    for name, value in result.items():
        if (
            not isinstance(value, str)
            or len(value) > 2048
            or re.fullmatch(r"[A-Za-z0-9_:/@.=-]*", value) is None
            or (name in fields["required"] and not value)
        ):
            raise ConfigurationError("configuration_value_invalid")
    if result["FULL_MARKET_ENABLED"] not in {"true", "false"}:
        raise ConfigurationError("configuration_flag_invalid")
    if (
        result["ECR_REGISTRY"]
        != f"{route['AWS_ACCOUNT_ID']}.dkr.ecr.{route['AWS_REGION']}.amazonaws.com"
    ):
        raise ConfigurationError("configuration_registry_invalid")
    kms_prefix = f"arn:aws:kms:{route['AWS_REGION']}:{route['AWS_ACCOUNT_ID']}:key/"
    if (
        re.fullmatch(
            re.escape(kms_prefix) + r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
            result["DEPLOY_BUNDLE_KMS_KEY_ARN"],
        )
        is None
    ):
        raise ConfigurationError("configuration_kms_account_invalid")
    for name in ("AWS_INSTANCE_PROFILE_NAME", "DEPLOY_BUNDLE_BUCKET", "RDS_DB_INSTANCE_IDENTIFIER"):
        if name in result and re.fullmatch(r"[A-Za-z0-9._-]+", result[name]) is None:
            raise ConfigurationError("configuration_resource_name_invalid")
    if re.fullmatch(r"[a-zA-Z0-9.-]+", result["AWS_DNS_CHECK_NAME"]) is None:
        raise ConfigurationError("configuration_dns_invalid")
    staging = bootstrap("staging", unit)
    expected = {
        "AWS_PROMOTION_ROLE_ARN": f"arn:aws:iam::{route['AWS_ACCOUNT_ID']}:role/{unit}-{target}-promotion",
        "STAGING_AWS_ACCOUNT_ID": staging["AWS_ACCOUNT_ID"],
        "STAGING_ECR_REGISTRY": f"{staging['AWS_ACCOUNT_ID']}.dkr.ecr.{staging['AWS_REGION']}.amazonaws.com",
        "STAGING_PROMOTION_READ_ROLE_ARN": f"arn:aws:iam::{staging['AWS_ACCOUNT_ID']}:role/{unit}-staging-promotion-reader",
    }
    for name, expected_value in expected.items():
        if result[name] and result[name] != expected_value:
            raise ConfigurationError("configuration_promotion_identity_invalid")
    return {**route, **result}


def aws(arguments: list[str], region: str) -> dict:
    result = subprocess.run(
        ["aws", *arguments, "--region", region, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise ConfigurationError("aws_configuration_request_failed")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "validate", "load", "sync"))
    parser.add_argument("--target", choices=("staging", "production"), required=True)
    parser.add_argument("--unit", choices=("findb", "fetcher"), required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()
    try:
        route = bootstrap(args.target, args.unit)
        if args.command == "bootstrap":
            values = route
        else:
            if args.command != "validate":
                identity = aws(["sts", "get-caller-identity"], route["AWS_REGION"])
                if identity["Account"] != route["AWS_ACCOUNT_ID"]:
                    raise ConfigurationError("aws_account_mismatch")
            if args.command == "load":
                parameter = aws(
                    ["ssm", "get-parameter", "--name", route["CONFIGURATION_PATH"]],
                    route["AWS_REGION"],
                )["Parameter"]
                if parameter["Type"] != "String":
                    raise ConfigurationError("deployment_parameter_must_be_nonsecret_string")
                document = json.loads(parameter["Value"])
            else:
                if args.input is None:
                    raise ConfigurationError("input_required")
                document = json.loads(args.input.read_text())
            values = validate(document, target=args.target, unit=args.unit)
            if args.command == "sync":
                # File input is already allowlisted; stdout/stderr never contain its values.
                aws(
                    [
                        "ssm",
                        "put-parameter",
                        "--name",
                        route["CONFIGURATION_PATH"],
                        "--type",
                        "String",
                        "--value",
                        json.dumps(document, separators=(",", ":")),
                        "--overwrite",
                    ],
                    route["AWS_REGION"],
                )
        if args.github_env:
            with args.github_env.open("a", encoding="utf-8") as output:
                for name, value in sorted(values.items()):
                    output.write(f"{name}={value}\n")
        print(
            json.dumps(
                {
                    "status": "valid",
                    "schema_version": 1,
                    "unit": args.unit,
                    "deployment_target": args.target,
                    "fields": sorted(values),
                },
                separators=(",", ":"),
            )
        )
        return 0
    except (ConfigurationError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        print("aws_configuration=failed", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
