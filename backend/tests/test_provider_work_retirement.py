"""All providers reject the retired protocol without an alternate write path."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import provider_work


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
@pytest.mark.parametrize("value", [False, 0, "false", "False", "0", "no", "off"])
def test_legacy_configuration_is_not_a_runtime_mode(environment, value):
    with pytest.raises(
        ValidationError, match="cannot be false; provider work protocol is required"
    ):
        Settings(_env_file=None, APP_ENVIRONMENT=environment, PROVIDER_WORK_REQUIRED=value)


@pytest.mark.parametrize(
    "value", [True, 1, "true", "True", "TRUE", "1", "yes", "YES", "on", "t", "y"]
)
def test_required_protocol_accepts_standard_true_boolean_values(value):
    assert Settings(_env_file=None, PROVIDER_WORK_REQUIRED=value).PROVIDER_WORK_REQUIRED is True


@pytest.mark.parametrize("value", ["", "invalid", " true ", None, 2])
def test_invalid_protocol_value_is_reported_as_boolean_error(value):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, PROVIDER_WORK_REQUIRED=value)
    error = caught.value.errors()[0]
    assert error["loc"] == ("PROVIDER_WORK_REQUIRED",)
    assert error["type"] in {"bool_parsing", "bool_type"}
    assert "legacy provider execution" not in str(caught.value)


@pytest.mark.parametrize("value", ["True", "1", "yes"])
def test_required_protocol_environment_uses_normal_boolean_parsing(monkeypatch, value):
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", value)
    assert Settings(_env_file=None).PROVIDER_WORK_REQUIRED is True


def test_missing_configuration_defaults_to_required(monkeypatch):
    monkeypatch.delenv("PROVIDER_WORK_REQUIRED", raising=False)
    assert Settings(_env_file=None).PROVIDER_WORK_REQUIRED is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex", "factset"])
async def test_missing_owner_rejects_before_any_database_acceptance(provider):
    # There is deliberately no DB interface: missing ownership must not reach it.
    db = SimpleNamespace(info={})
    with pytest.raises(provider_work.ProviderWorkError, match="protocol is required"):
        await provider_work.acceptance(db, provider=provider, dataset_key="scope", trade_date=None)
