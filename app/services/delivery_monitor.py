"""Durable monitoring for canonical full snapshots that never arrived."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DatasetRegistry, IngestionRun, MissingDeliveryAlert
from app.services.delivery_policy import (
    parse_delivery_expectation,
    resolve_expected_data_date,
)
from app.services.ingress_contracts import DatasetContractDeclaration
from app.utils import utc_now, uuid7

logger = logging.getLogger(__name__)
_MONITOR_LOCK_KEY = "findb:delivery-monitor:v1"


@dataclass(frozen=True)
class DeliveryMonitorResult:
    created_or_refreshed: int = 0
    resolved: int = 0
    diagnostics: tuple[dict[str, Any], ...] = ()
    skipped_locked: bool = False


def _bounded_diagnostic(dataset_key: str, source: str, reason: str) -> dict[str, str]:
    return {
        "dataset_key": dataset_key[:50],
        "source": source[:50],
        "reason": reason[:80],
    }


async def scan_missing_deliveries(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> DeliveryMonitorResult:
    """Resolve late arrivals and upsert one alert per missing identity/date."""
    evaluated_at = (now or utc_now()).astimezone(timezone.utc)
    acquired = await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": _MONITOR_LOCK_KEY},
    )
    if not acquired:
        await db.rollback()
        return DeliveryMonitorResult(skipped_locked=True)

    delivered = exists(
        select(IngestionRun.run_id).where(
            IngestionRun.dataset_key == MissingDeliveryAlert.dataset_key,
            IngestionRun.source == MissingDeliveryAlert.source,
            IngestionRun.schema_id == MissingDeliveryAlert.schema_id,
            IngestionRun.schema_version == MissingDeliveryAlert.schema_version,
            IngestionRun.batch_data_date == MissingDeliveryAlert.expected_data_date,
            IngestionRun.delivery_mode == "full_snapshot",
            IngestionRun.is_rerun.is_(False),
        )
    )
    resolution = await db.execute(
        update(MissingDeliveryAlert)
        .where(MissingDeliveryAlert.status == "open", delivered)
        .values(status="resolved", resolved_at=evaluated_at)
        .returning(MissingDeliveryAlert.alert_id)
    )
    resolved = len(resolution.scalars().all())

    datasets = list(
        (await db.execute(select(DatasetRegistry).where(DatasetRegistry.is_active.is_(True))))
        .scalars()
        .all()
    )
    diagnostics: list[dict[str, Any]] = []
    refreshed = 0
    for dataset in datasets:
        try:
            declaration = DatasetContractDeclaration.model_validate(dataset.config or {})
            expectation = parse_delivery_expectation(dataset.config)
        except ValueError:
            logger.warning(
                "Skipping invalid delivery monitor config for dataset=%s",
                dataset.dataset_key[:50],
            )
            diagnostics.append(
                _bounded_diagnostic(dataset.dataset_key, "", "invalid_dataset_contract")
            )
            continue
        if expectation is None or expectation.missing_delivery.action == "disabled":
            continue
        latest = expectation.latest_date
        if latest is None:
            diagnostics.append(
                _bounded_diagnostic(dataset.dataset_key, "", "latest_date_policy_not_configured")
            )
            continue
        expected_date, reason = await resolve_expected_data_date(db, latest, evaluated_at)
        if expected_date is None:
            for source in expectation.missing_delivery.expected_sources:
                item = _bounded_diagnostic(
                    dataset.dataset_key, source, reason or "calendar_unavailable"
                )
                diagnostics.append(item)
                logger.warning("Missing delivery scan skipped: %s", item)
            continue

        for source in expectation.missing_delivery.expected_sources:
            present = await db.scalar(
                select(
                    exists().where(
                        IngestionRun.dataset_key == dataset.dataset_key,
                        IngestionRun.source == source,
                        IngestionRun.schema_id == declaration.schema_id,
                        IngestionRun.schema_version == declaration.current_schema_version,
                        IngestionRun.batch_data_date == expected_date,
                        IngestionRun.delivery_mode == "full_snapshot",
                        IngestionRun.is_rerun.is_(False),
                    )
                )
            )
            if present:
                continue
            details = {
                "code": "DATASET_DELIVERY_MISSING",
                "feed": f"{source}:{dataset.dataset_key}",
                "calendar_market": latest.calendar_market,
                "evaluated_at": evaluated_at.isoformat(),
            }
            statement = (
                insert(MissingDeliveryAlert)
                .values(
                    alert_id=uuid7(),
                    dataset_key=dataset.dataset_key,
                    source=source,
                    schema_id=declaration.schema_id,
                    schema_version=declaration.current_schema_version,
                    expected_data_date=expected_date,
                    status="open",
                    first_detected_at=evaluated_at,
                    last_detected_at=evaluated_at,
                    details=details,
                )
                .on_conflict_do_update(
                    constraint="uq_missing_delivery_identity_date",
                    set_={
                        "last_detected_at": evaluated_at,
                        "details": details,
                    },
                    where=MissingDeliveryAlert.status == "open",
                )
            )
            await db.execute(statement)
            refreshed += 1

    await db.commit()
    return DeliveryMonitorResult(
        created_or_refreshed=refreshed,
        resolved=resolved,
        diagnostics=tuple(diagnostics[:100]),
    )


async def list_missing_delivery_alerts(
    db: AsyncSession,
    *,
    status: str | None,
    dataset_key: str | None,
    source: str | None,
    page: int,
    page_size: int,
) -> tuple[list[MissingDeliveryAlert], int]:
    conditions = []
    if status:
        conditions.append(MissingDeliveryAlert.status == status)
    if dataset_key:
        conditions.append(MissingDeliveryAlert.dataset_key == dataset_key)
    if source:
        conditions.append(MissingDeliveryAlert.source == source.strip().lower())
    statement = select(MissingDeliveryAlert).where(*conditions)
    count_statement = select(func.count()).select_from(MissingDeliveryAlert).where(*conditions)
    total = int((await db.scalar(count_statement)) or 0)
    rows = list(
        (
            await db.execute(
                statement.order_by(
                    MissingDeliveryAlert.first_detected_at.desc(),
                    MissingDeliveryAlert.alert_id.desc(),
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return rows, total
