"""Staging and production accept exactly the same nonsecret deployment schema."""

import importlib.util
import json
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
    assert "FULL_MARKET_ENABLED" not in staging
    assert "FULL_MARKET_ENABLED" not in production
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
@pytest.mark.parametrize("flag", ["true", "false", "True", "0", ""])
def test_retired_full_market_flag_is_tolerated_with_warning_and_never_exported(
    target, unit, flag, capsys
):
    value = document(target, unit)
    value["values"]["FULL_MARKET_ENABLED"] = flag
    result = module.validate(value, target=target, unit=unit)
    assert "FULL_MARKET_ENABLED" not in result
    assert value["values"]["FULL_MARKET_ENABLED"] == flag  # Input is never mutated.
    warning = capsys.readouterr().err
    assert warning == (
        "aws_configuration=warning reason=retired_field_ignored field=FULL_MARKET_ENABLED\n"
    )
    assert module.validate(document(target, unit), target=target, unit=unit) == result
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", ["load", "sync", "validate"])
def test_cli_ignores_retired_ssm_flag_once_and_sync_removes_it(
    tmp_path, monkeypatch, capsys, command
):
    value = document("staging", "fetcher")
    value["values"]["FULL_MARKET_ENABLED"] = "true"
    calls = []
    route = module.bootstrap("staging", "fetcher")

    def aws(arguments, region):
        calls.append(arguments)
        if arguments[:2] == ["sts", "get-caller-identity"]:
            return {"Account": route["AWS_ACCOUNT_ID"]}
        if arguments[:2] == ["ssm", "get-parameter"]:
            return {"Parameter": {"Type": "String", "Value": json.dumps(value)}}
        return {}

    source = tmp_path / "deployment.json"
    source.write_text(json.dumps(value))
    exported = tmp_path / "github.env"
    monkeypatch.setattr(module, "aws", aws)
    arguments = ["aws_configuration.py", command, "--target", "staging", "--unit", "fetcher"]
    if command != "load":
        arguments += ["--input", str(source)]
    monkeypatch.setattr(module.sys, "argv", [*arguments, "--github-env", str(exported)])
    assert module.main() == 0
    output = capsys.readouterr()
    assert output.err.count("reason=retired_field_ignored field=FULL_MARKET_ENABLED") == 1
    assert "FULL_MARKET_ENABLED" not in exported.read_text()
    assert "FULL_MARKET_ENABLED" not in output.out
    written = [call for call in calls if call[:2] == ["ssm", "put-parameter"]]
    if command == "sync":
        [put] = written
        stored = json.loads(put[put.index("--value") + 1])
        assert "FULL_MARKET_ENABLED" not in stored["values"]
        assert stored["values"] == {
            name: item for name, item in value["values"].items() if name != "FULL_MARKET_ENABLED"
        }
    else:
        assert written == []


def test_templates_and_schema_no_longer_name_the_retired_flag():
    root = PATH.parent
    for path in [root / "deployment.schema.json", *root.glob("*/*/deployment.json.example")]:
        assert "FULL_MARKET_ENABLED" not in path.read_text(), path


@pytest.mark.parametrize("target", ["staging", "production"])
@pytest.mark.parametrize("unit", ["findb", "fetcher"])
@pytest.mark.parametrize("flag", [None, "true", "false", "0", ""])
def test_provider_work_protocol_cannot_be_disabled(target, unit, flag):
    value = document(target, unit)
    if flag is not None:
        value["values"]["PROVIDER_WORK_REQUIRED"] = flag
    if flag in {None, "true"}:
        assert module.validate(value, target=target, unit=unit)["PROVIDER_WORK_REQUIRED"] == "true"
    else:
        with pytest.raises(module.ConfigurationError):
            module.validate(value, target=target, unit=unit)
