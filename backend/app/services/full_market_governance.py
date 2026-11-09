"""Shared registry governance validation for monitoring and scheduler control."""

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from app.models.registry import DatasetRegistry
from app.services.ingress_contracts import (
    parse_dataset_contract_declaration,
    validate_dataset_contract_scope,
)
from app.services.provider_metadata import acquisition_feed, acquisition_feeds
from app.services.source_clients import PROVIDER_DATASET_SCOPE


@dataclass(frozen=True)
class FullMarketFeedDescriptor:
    schema_id: str
    schema_version: int
    market: str
    asset_class: str
    frequency: str


# The isolated Fetcher builders emit these fixed contracts and feed identities.
# Registry declarations must accept their output, even for disabled siblings.
FULL_MARKET_FEEDS = MappingProxyType(
    {
        feed.dataset_key: FullMarketFeedDescriptor(
            feed.contract.schema_id,
            feed.contract.current_version,
            feed.market,
            feed.asset_class,
            feed.frequency,
        )
        for feed in acquisition_feeds()
    }
)


def full_market_configuration(
    dataset: DatasetRegistry, provider: str
) -> tuple[bool, bool, list[str], list[str]]:
    """Separate opted-out readiness prerequisites from invalid registry configuration."""
    config = dataset.config if isinstance(dataset.config, dict) else {}
    errors: list[str] = []
    blockers: list[str] = []
    expected = FULL_MARKET_FEEDS.get(dataset.dataset_key)
    if expected is None or dataset.dataset_key not in PROVIDER_DATASET_SCOPE.get(provider, ()):
        errors.append("runtime_feed_scope_mismatch")
    try:
        declaration = parse_dataset_contract_declaration(config)
        if declaration is None:
            errors.append("contract_declaration_missing")
    except ValueError:
        errors.append("contract_declaration_invalid")
    else:
        if declaration is not None:
            if provider not in declaration.provider_scope():
                errors.append("provider_scope_mismatch")
            try:
                validate_dataset_contract_scope(
                    declaration, market=dataset.market, asset_class=dataset.asset_class
                )
            except ValueError:
                errors.append("contract_scope_mismatch")
            if expected is not None:
                if (
                    declaration.schema_id != expected.schema_id
                    or expected.schema_version not in declaration.accepted_schema_versions
                ):
                    errors.append("runtime_contract_mismatch")
                if (
                    declaration.defaults.market != expected.market
                    or declaration.defaults.asset_class != expected.asset_class
                    or dataset.frequency != expected.frequency
                ):
                    errors.append("runtime_feed_scope_mismatch")
    governance = config.get("full_market")
    if not isinstance(governance, dict):
        return False, False, [*errors, "full_market_governance_invalid"], blockers
    # Registry scope remains mandatory; enrollment/admission supplies runtime authority.
    activation = governance.get("activation_date")
    previous = False
    if activation is not None:
        try:
            if not isinstance(activation, str):
                raise ValueError("activation date must be a date string")
            previous = date.fromisoformat(activation) is not None
        except ValueError:
            errors.append("full_market_activation_date_invalid")
    if not dataset.is_active:
        errors.append("dataset_inactive")
    return False, previous, errors, blockers


def feed_schedule(dataset_key: str) -> tuple[str, str]:
    """Compatibility accessor for existing Backend consumers."""
    from app.services.feed_schedule import describe_feed

    matches = [f for f in acquisition_feeds() if f.dataset_key == dataset_key]
    schedules = {
        (describe_feed(f).source_timezone, describe_feed(f).scheduled_time) for f in matches
    }
    if len(schedules) != 1:
        raise ValueError("Unknown or ambiguous acquisition feed")
    return next(iter(schedules))


def is_execution_feed(dataset_key: str, provider: str) -> bool:
    return acquisition_feed(provider, dataset_key) is not None
