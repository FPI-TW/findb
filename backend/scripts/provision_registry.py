"""Provision target-specific dataset registry policy for a deployment.

Alembic migrations and the normal seed path are deliberately environment
neutral.  CD invokes this explicit step after migrations and before writers
restart.  It applies the bounded FinLab pilot policy in staging and removes
only the exact historical pilot value in production.

The update is one transaction protected by a PostgreSQL transaction advisory
lock.  A bounded JSON report is printed for deployment audit evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.services.delivery_policy import DeliveryExpectation
from scripts.seed_data import DATASETS, SUPPORTED_DATASETS, _merge_operator_config

DEPLOYMENT_TARGETS = ("staging", "production")
DATASET_KEY = "tw_equity_eod"
SOURCE_KEY = "finlab"
REGISTRY_PROVISIONING_LOCK_KEY = 7_135_202_607_280_002

FINLAB_PILOT_OVERRIDE: dict[str, Any] = {
    "baseline": {"enabled": False},
    "record_count": {"minimum_record_count": 2},
}

logger = logging.getLogger(__name__)


class RegistryProvisioningError(RuntimeError):
    """Raised when registry provisioning cannot safely continue."""


@dataclass(frozen=True)
class ProvisioningReport:
    """Auditable summary of one provisioning transaction."""

    deployment_target: str
    dataset_key: str
    action: str
    changed: bool
    previous_finlab_override: str
    resulting_finlab_override: str
    global_minimum_record_count: int | None | str
    reconciled_dataset_keys: tuple[str, ...] = ()
    changed_dataset_keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "deployment_target": self.deployment_target,
            "dataset_key": self.dataset_key,
            "action": self.action,
            "changed": self.changed,
            "previous_finlab_override": self.previous_finlab_override,
            "resulting_finlab_override": self.resulting_finlab_override,
            "global_minimum_record_count": self.global_minimum_record_count,
            "reconciled_dataset_keys": list(self.reconciled_dataset_keys),
            "changed_dataset_keys": list(self.changed_dataset_keys),
        }


def _copy_json(value: Any) -> Any:
    return copy.deepcopy(value)


def _validate_target(deployment_target: str) -> str:
    target = deployment_target
    if target not in DEPLOYMENT_TARGETS:
        expected = ", ".join(DEPLOYMENT_TARGETS)
        raise RegistryProvisioningError(
            f"deployment target must be one of {expected}; got {deployment_target!r}"
        )
    return target


def _override_state(value: Any) -> str:
    """Return a bounded label without serializing operator-owned policy JSON."""
    if value is None:
        return "absent"
    if value == FINLAB_PILOT_OVERRIDE:
        return "exact_pilot"
    return "custom"


def _bounded_threshold(value: Any) -> int | None | str:
    """Keep the report's global threshold scalar and bounded."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000:
        return value
    return "invalid"


def provision_registry_config(
    config: Mapping[str, Any] | None,
    *,
    deployment_target: str,
) -> tuple[dict[str, Any], ProvisioningReport]:
    """Apply a deployment target policy to one registry config in memory.

    Existing non-object policy nodes are rejected rather than replaced.  This
    makes malformed or operator-managed JSON fail closed instead of being
    silently overwritten.
    """
    target = _validate_target(deployment_target)
    if config is None:
        working: dict[str, Any] = {}
    elif isinstance(config, Mapping):
        working = _copy_json(dict(config))
    else:
        raise RegistryProvisioningError("dataset registry config must be a JSON object or null")

    expectation_value = working.get("delivery_expectation")
    if expectation_value is None:
        expectation: dict[str, Any] = {}
        working["delivery_expectation"] = expectation
    elif isinstance(expectation_value, dict):
        expectation = expectation_value
    else:
        raise RegistryProvisioningError("delivery_expectation must be a JSON object")

    source_overrides_value = expectation.get("source_overrides")
    if source_overrides_value is None:
        source_overrides: dict[str, Any] = {}
    elif isinstance(source_overrides_value, dict):
        source_overrides = source_overrides_value
    else:
        raise RegistryProvisioningError(
            "delivery_expectation.source_overrides must be a JSON object"
        )

    previous_override = _copy_json(source_overrides.get(SOURCE_KEY))
    if target == "staging":
        source_overrides[SOURCE_KEY] = _copy_json(FINLAB_PILOT_OVERRIDE)
        expectation["source_overrides"] = source_overrides
        changed = previous_override != FINLAB_PILOT_OVERRIDE
        action = "staging_finlab_pilot_applied" if changed else "staging_finlab_pilot_unchanged"
    elif previous_override == FINLAB_PILOT_OVERRIDE:
        source_overrides.pop(SOURCE_KEY, None)
        if source_overrides:
            expectation["source_overrides"] = source_overrides
        else:
            expectation.pop("source_overrides", None)
        changed = True
        action = "production_exact_finlab_pilot_removed"
    else:
        changed = False
        action = (
            "production_operator_finlab_override_preserved"
            if previous_override is not None
            else "production_full_market_policy_unchanged"
        )

    resulting_source_overrides = expectation.get("source_overrides")
    resulting_override_value = (
        resulting_source_overrides.get(SOURCE_KEY)
        if isinstance(resulting_source_overrides, dict)
        else None
    )
    record_count = expectation.get("record_count")
    global_minimum_record_count_value = (
        record_count.get("minimum_record_count") if isinstance(record_count, dict) else None
    )
    report = ProvisioningReport(
        deployment_target=target,
        dataset_key=DATASET_KEY,
        action=action,
        changed=changed,
        previous_finlab_override=_override_state(previous_override),
        resulting_finlab_override=_override_state(resulting_override_value),
        global_minimum_record_count=_bounded_threshold(global_minimum_record_count_value),
    )
    return working, report


def reconcile_supported_dataset_configs(
    existing_configs: Mapping[str, Mapping[str, Any] | None],
    *,
    deployment_target: str,
) -> tuple[dict[str, dict[str, Any]], ProvisioningReport]:
    """Restore authoritative declarations while preserving operator policy overrides."""
    target = _validate_target(deployment_target)
    expected_keys = set(SUPPORTED_DATASETS)
    actual_keys = set(existing_configs)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unexpected = sorted(actual_keys - expected_keys)
        raise RegistryProvisioningError(
            "supported dataset registry rows differ "
            f"(missing={missing!r}, unexpected={unexpected!r})"
        )

    seed_configs = {dataset["dataset_key"]: dataset["config"] for dataset in DATASETS}
    reconciled: dict[str, dict[str, Any]] = {}
    changed: list[str] = []
    for dataset_key in SUPPORTED_DATASETS:
        existing = existing_configs[dataset_key]
        if existing is not None and not isinstance(existing, Mapping):
            raise RegistryProvisioningError(
                f"dataset registry config must be a JSON object or null: {dataset_key}"
            )
        merged = _merge_operator_config(
            dataset_key,
            seed_configs[dataset_key],
            dict(existing) if existing is not None else None,
        )
        raw_expectation = merged.get("delivery_expectation")
        try:
            expectation = DeliveryExpectation.model_validate(raw_expectation)
        except ValueError as exc:
            raise RegistryProvisioningError(
                f"dataset delivery expectation is invalid: {dataset_key}"
            ) from exc
        allowed_sources = merged.get("allowed_sources")
        if not isinstance(allowed_sources, list) or any(
            not isinstance(source, str) or expectation.for_source(source).latest_date is None
            for source in allowed_sources
        ):
            raise RegistryProvisioningError(
                f"dataset latest-date policy is incomplete: {dataset_key}"
            )
        reconciled[dataset_key] = merged
        if merged != existing:
            changed.append(dataset_key)

    tw_config, report = provision_registry_config(
        reconciled[DATASET_KEY],
        deployment_target=target,
    )
    reconciled[DATASET_KEY] = tw_config
    if tw_config != existing_configs[DATASET_KEY] and DATASET_KEY not in changed:
        changed.append(DATASET_KEY)
    return reconciled, replace(
        report,
        action=f"{target}_registry_reconciled" if changed else report.action,
        changed=bool(changed),
        reconciled_dataset_keys=tuple(SUPPORTED_DATASETS),
        changed_dataset_keys=tuple(changed),
    )


async def _lock_registry(connection: AsyncConnection) -> None:
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": REGISTRY_PROVISIONING_LOCK_KEY},
    )


async def provision_registry(
    database_url: str,
    *,
    deployment_target: str,
) -> ProvisioningReport:
    """Apply policy to the live registry in one transaction."""
    target = _validate_target(deployment_target)
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as connection:
            await _lock_registry(connection)
            result = await connection.execute(
                text("""
                    SELECT dataset_key, config
                    FROM dataset_registry
                    WHERE dataset_key IN (
                        'us_equity_eod',
                        'tw_equity_eod',
                        'tw_equity_minute',
                        'tw_etf_minute'
                    )
                    ORDER BY dataset_key
                    FOR UPDATE
                """),
            )
            rows = result.mappings().all()
            new_configs, report = reconcile_supported_dataset_configs(
                {row["dataset_key"]: row["config"] for row in rows},
                deployment_target=target,
            )
            for dataset_key in report.changed_dataset_keys:
                await connection.execute(
                    text("""
                        UPDATE dataset_registry
                        SET config = CAST(:config AS jsonb), updated_at = now()
                        WHERE dataset_key = :dataset_key
                    """),
                    {
                        "dataset_key": dataset_key,
                        "config": json.dumps(
                            new_configs[dataset_key],
                            separators=(",", ":"),
                        ),
                    },
                )
        return report
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-target",
        choices=DEPLOYMENT_TARGETS,
        required=True,
        help="GitHub deployment target whose registry policy should be provisioned",
    )
    return parser


async def _main() -> int:
    args = _build_parser().parse_args()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        print("error: DATABASE_URL is required", file=sys.stderr)
        return 2

    try:
        report = await provision_registry(database_url, deployment_target=args.deployment_target)
    except RegistryProvisioningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - keep deployment output bounded and secret-free
        print(f"error: registry provisioning failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    payload = report.as_dict()
    logger.info("registry provisioning report=%s", json.dumps(payload, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(_main()))
