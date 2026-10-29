"""All providers reject the retired protocol without an alternate write path."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import provider_work


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
@pytest.mark.parametrize("value", [False, "false", "0", "", "invalid"])
def test_legacy_configuration_is_not_a_runtime_mode(environment, value):
    with pytest.raises(ValidationError, match="legacy provider execution is retired"):
        Settings(_env_file=None, APP_ENVIRONMENT=environment, PROVIDER_WORK_REQUIRED=value)


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
