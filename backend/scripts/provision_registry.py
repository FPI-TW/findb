"""Reconcile the same registry declarations in both AWS environments.

Restore the seed declarations; retain operator-owned policies and controls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from app.services.delivery_policy import DeliveryExpectation
from scripts.seed_data import DATASETS, SUPPORTED_DATASETS, _merge_operator_config

DEPLOYMENT_TARGETS = ("staging", "production")
REGISTRY_PROVISIONING_LOCK_KEY = 7_135_202_607_280_002

logger = logging.getLogger(__name__)


class RegistryProvisioningError(RuntimeError):
    """Raised when registry provisioning cannot safely continue."""


@dataclass(frozen=True)
class ProvisioningReport:
    """Auditable summary of one provisioning transaction."""

    deployment_target: str
    action: str
    changed: bool
    reconciled_dataset_keys: tuple[str, ...] = ()
    changed_dataset_keys: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "deployment_target": self.deployment_target,
            "action": self.action,
            "changed": self.changed,
            "reconciled_dataset_keys": list(self.reconciled_dataset_keys),
            "changed_dataset_keys": list(self.changed_dataset_keys),
        }


def _validate_target(deployment_target: str) -> str:
    target = deployment_target
    if target not in DEPLOYMENT_TARGETS:
        expected = ", ".join(DEPLOYMENT_TARGETS)
        raise RegistryProvisioningError(
            f"deployment target must be one of {expected}; got {deployment_target!r}"
        )
    return target


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
    return reconciled, ProvisioningReport(
        deployment_target=target,
        action=f"{target}_registry_reconciled" if changed else "operator_policy_preserved",
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
                    WHERE dataset_key IN :dataset_keys
                    ORDER BY dataset_key
                    FOR UPDATE
                """).bindparams(bindparam("dataset_keys", expanding=True)),
                {"dataset_keys": list(SUPPORTED_DATASETS)},
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
        help="AWS deployment target whose registry policy should be provisioned",
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
