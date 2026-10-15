"""Staging and production accept exactly the same nonsecret deployment schema."""

import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "env/aws_configuration.py"
SPEC = importlib.util.spec_from_file_location("aws_configuration", PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def document(target, unit):
    route = module.bootstrap(target, unit)
    account = route["AWS_ACCOUNT_ID"]
    values = {
        "AWS_INSTANCE_PROFILE_NAME": f"{unit}-{target}-instance",
        "AWS_SSM_LOG_GROUP": f"/findb/{target}/{unit}/ssm",
        "AWS_DNS_CHECK_NAME": f"{target}.example.test",
        "ECR_REGISTRY": f"{account}.dkr.ecr.ap-southeast-1.amazonaws.com",
        "DEPLOY_BUNDLE_BUCKET": f"findb-{target}-bundle-{account}",
        "DEPLOY_BUNDLE_KMS_KEY_ARN": f"arn:aws:kms:ap-southeast-1:{account}:key/12345678-1234-1234-1234-123456789abc",
    }
    if unit == "findb":
        values["RDS_DB_INSTANCE_IDENTIFIER"] = f"findb-{target}"
    return {"schema_version": 1, "deployment_target": target, "unit": unit, "values": values}


@pytest.mark.parametrize("unit", ["findb", "fetcher"])
def test_environment_schema_parity_and_safe_defaults(unit):
    staging = module.validate(document("staging", unit), target="staging", unit=unit)
    production = module.validate(document("production", unit), target="production", unit=unit)
    assert set(staging) == set(production)
    assert staging["FULL_MARKET_ENABLED"] == production["FULL_MARKET_ENABLED"] == "false"
    assert staging["CONFIGURATION_PATH"] != production["CONFIGURATION_PATH"]


@pytest.mark.parametrize("target", ["staging", "production"])
@pytest.mark.parametrize("change", ["secret", "version", "target", "registry", "newline", "shell"])
def test_configuration_rejects_secret_fields_wrong_identity_and_injection(target, change):
    value = document(target, "findb")
    if change == "secret":
        value["values"]["DATABASE_URL"] = "secret-not-allowed"
    elif change == "version":
        value["schema_version"] = 2
    elif change == "target":
        value["deployment_target"] = "other"
    elif change == "registry":
        value["values"]["ECR_REGISTRY"] = "other.dkr.ecr.ap-southeast-1.amazonaws.com"
    elif change == "newline":
        value["values"]["AWS_DNS_CHECK_NAME"] = "safe\nINJECT=unsafe"
    else:
        value["values"]["AWS_PROMOTION_ROLE_ARN"] = "$(unsafe)"
    with pytest.raises(module.ConfigurationError):
        module.validate(value, target=target, unit="findb")


@pytest.mark.parametrize("target", ["staging", "production"])
@pytest.mark.parametrize("unit", ["findb", "fetcher"])
@pytest.mark.parametrize("flag", ["true", "false", None, "True", "0", ""])
def test_full_market_flag_is_explicit_or_defaults_stopped(target, unit, flag):
    value = document(target, unit)
    if flag is not None:
        value["values"]["FULL_MARKET_ENABLED"] = flag
    if flag in {"true", "false", None}:
        assert module.validate(value, target=target, unit=unit)["FULL_MARKET_ENABLED"] == (
            flag or "false"
        )
    else:
        with pytest.raises(module.ConfigurationError):
            module.validate(value, target=target, unit=unit)
