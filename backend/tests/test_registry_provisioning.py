"""Target-aware dataset registry provisioning checks."""

from copy import deepcopy
from pathlib import Path

import pytest

from scripts import provision_registry as provisioning

BACKEND_ROOT = Path(__file__).parents[1]
DEPLOY_HELPER_PATH = (
    BACKEND_ROOT.parent / "infra" / "deploy" / "runtime-secrets" / "deploy_findb_aws.sh"
)


def _config(*, finlab: dict | None = None, other: dict | None = None) -> dict:
    overrides = {}
    if finlab is not None:
        overrides["finlab"] = finlab
    if other is not None:
        overrides["other"] = other
    return {
        "operator_note": "preserve",
        "delivery_expectation": {
            "record_count": {"minimum_record_count": 2100},
            "source_overrides": overrides,
        },
    }


@pytest.mark.parametrize("target", ["staging", "production"])
def test_unified_registry_removes_exact_legacy_pilot_and_is_idempotent(target):
    original = _config(
        finlab=provisioning.FINLAB_PILOT_OVERRIDE,
        other={"record_count": {"minimum_record_count": 17}},
    )
    first, report = provisioning.provision_registry_config(original, deployment_target=target)
    assert first["delivery_expectation"]["record_count"] == {"minimum_record_count": 2100}
    assert first["delivery_expectation"]["source_overrides"] == {
        "other": {"record_count": {"minimum_record_count": 17}}
    }
    assert report.changed and report.action == "legacy_finlab_pilot_removed"
    second, second_report = provisioning.provision_registry_config(first, deployment_target=target)
    assert second == first and not second_report.changed


@pytest.mark.parametrize("target", ["staging", "production"])
def test_unified_registry_preserves_operator_overrides(target):
    original = _config(
        finlab={"baseline": {"enabled": True}, "record_count": {"minimum_record_count": 99}}
    )
    result, report = provisioning.provision_registry_config(original, deployment_target=target)
    assert result == original
    assert not report.changed
    assert report.action == "operator_policy_preserved"


def test_report_never_serializes_arbitrary_custom_policy() -> None:
    marker = "operator-secret-annotation-" + ("x" * 10_000)
    config = _config(
        finlab={
            "baseline": {"enabled": True, "annotation": marker},
            "record_count": {"minimum_record_count": 99},
        }
    )
    _, report = provisioning.provision_registry_config(config, deployment_target="production")
    encoded = str(report.as_dict())
    assert marker not in encoded
    assert report.previous_finlab_override == "custom"
    assert report.resulting_finlab_override == "custom"


@pytest.mark.parametrize("target", ("qa", "", "STAGING "))
def test_noncanonical_target_is_rejected(target: str) -> None:
    with pytest.raises(provisioning.RegistryProvisioningError):
        provisioning.provision_registry_config({}, deployment_target=target)


def test_malformed_policy_fails_closed() -> None:
    with pytest.raises(provisioning.RegistryProvisioningError, match="delivery_expectation"):
        provisioning.provision_registry_config(
            {"delivery_expectation": []}, deployment_target="staging"
        )
    with pytest.raises(provisioning.RegistryProvisioningError, match="source_overrides"):
        provisioning.provision_registry_config(
            {"delivery_expectation": {"source_overrides": []}},
            deployment_target="production",
        )


def test_supported_registry_reconciliation_restores_missing_us_policy() -> None:
    existing = {
        dataset["dataset_key"]: deepcopy(dataset["config"]) for dataset in provisioning.DATASETS
    }
    existing["us_equity_eod"]["delivery_expectation"] = {
        "missing_delivery": {
            "action": "warn",
            "expected_sources": ["twelve_data"],
            "deadline_local_time": "10:00:00",
        },
        "schedule": {
            "enabled": True,
            "slot_id": "western_markets_window",
            "local_time": "09:00:00",
            "timezone": "Asia/Taipei",
            "target_date_lag_days": 1,
            "expected_sources": ["twelve_data"],
        },
    }
    existing["us_equity_eod"]["operator_note"] = "preserve"

    reconciled, report = provisioning.reconcile_supported_dataset_configs(
        existing,
        deployment_target="production",
    )

    us_policy = reconciled["us_equity_eod"]["delivery_expectation"]
    assert us_policy["latest_date"]["calendar_market"] == "US"
    assert us_policy["baseline"]["strategy"] == "rolling_median"
    assert us_policy["record_count"]["minimum_record_count"] == 1
    assert us_policy["schedule"]["local_time"] == "09:00:00"
    assert reconciled["us_equity_eod"]["operator_note"] == "preserve"
    assert report.changed is True
    assert report.action == "production_registry_reconciled"
    assert report.reconciled_dataset_keys == provisioning.SUPPORTED_DATASETS
    assert report.changed_dataset_keys == ("us_equity_eod",)


def test_supported_registry_reconciliation_requires_all_four_rows() -> None:
    existing = {
        dataset["dataset_key"]: deepcopy(dataset["config"])
        for dataset in provisioning.DATASETS
        if dataset["dataset_key"] != "tw_etf_minute"
    }

    with pytest.raises(provisioning.RegistryProvisioningError, match="tw_etf_minute"):
        provisioning.reconcile_supported_dataset_configs(
            existing,
            deployment_target="production",
        )


class _FakeResult:
    def __init__(self, configs: dict[str, dict]):
        self.configs = configs

    def mappings(self):
        return self

    def all(self):
        return [
            {"dataset_key": dataset_key, "config": config}
            for dataset_key, config in self.configs.items()
        ]


class _FakeConnection:
    def __init__(self, config: dict, *, fail_update: bool = False):
        self.configs = {
            dataset["dataset_key"]: deepcopy(dataset["config"]) for dataset in provisioning.DATASETS
        }
        self.configs[provisioning.DATASET_KEY] = config
        self.fail_update = fail_update
        self.updated = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT dataset_key, config" in sql:
            return _FakeResult(self.configs)
        if "UPDATE dataset_registry" in sql:
            self.updated = True
            if self.fail_update:
                raise RuntimeError("simulated update failure")
        return _FakeResult(self.configs)


class _FakeTransaction:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection
        self.rolled_back = False

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, _exc, _tb):
        self.rolled_back = exc_type is not None
        return False


class _FakeEngine:
    def __init__(self, transaction: _FakeTransaction):
        self.transaction = transaction
        self.disposed = False

    def begin(self):
        return self.transaction

    async def dispose(self):
        self.disposed = True


@pytest.mark.asyncio
async def test_update_failure_rolls_back_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(_config(), fail_update=True)
    transaction = _FakeTransaction(connection)
    engine = _FakeEngine(transaction)
    monkeypatch.setattr(provisioning, "create_async_engine", lambda *args, **kwargs: engine)

    with pytest.raises(RuntimeError, match="simulated update failure"):
        await provisioning.provision_registry("postgresql://unused", deployment_target="staging")

    assert connection.updated is True
    assert transaction.rolled_back is True
    assert engine.disposed is True


def test_cd_passes_target_explicitly_after_migration() -> None:
    workflow = DEPLOY_HELPER_PATH.read_text(encoding="utf-8")
    migration_marker = "alembic upgrade head"
    provisioning_marker = '--deployment-target "$APP_ENVIRONMENT"'
    assert migration_marker in workflow
    assert provisioning_marker in workflow
    assert "python /app/scripts/provision_registry.py" in workflow
    provisioning_index = workflow.index("python /app/scripts/provision_registry.py")
    assert workflow.index(migration_marker) < provisioning_index
    assert workflow.index(provisioning_marker, provisioning_index) > provisioning_index
