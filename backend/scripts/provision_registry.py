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
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "deployment_target": self.deployment_target,
            "dataset_key": self.dataset_key,
            "action": self.action,
            "changed": self.changed,
            "previous_finlab_override": self.previous_finlab_override,
            "resulting_finlab_override": self.resulting_finlab_override,
            "global_minimum_record_count": self.global_minimum_record_count,
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
                    SELECT config
                    FROM dataset_registry
                    WHERE dataset_key = :dataset_key
                    FOR UPDATE
                """),
                {"dataset_key": DATASET_KEY},
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise RegistryProvisioningError(
                    f"required dataset registry row is missing: {DATASET_KEY}"
                )

            new_config, report = provision_registry_config(
                row["config"],
                deployment_target=target,
            )
            if report.changed:
                await connection.execute(
                    text("""
                        UPDATE dataset_registry
                        SET config = CAST(:config AS jsonb), updated_at = now()
                        WHERE dataset_key = :dataset_key
                    """),
                    {
                        "dataset_key": DATASET_KEY,
                        "config": json.dumps(new_config, separators=(",", ":")),
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
