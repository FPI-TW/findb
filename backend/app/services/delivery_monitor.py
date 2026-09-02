"""Durable monitoring for canonical full snapshots that never arrived."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DatasetRegistry, IngestionRun, MissingDeliveryAlert
from app.services.delivery_policy import (
    delivery_run_coverage_condition,
    parse_delivery_expectation,
    resolve_expected_data_date,
)
from app.services.feed_scope import lock_feed_scope
from app.services.ingress_contracts import DatasetContractDeclaration
from app.services.sequenced_snapshots import list_sequenced_snapshot_groups
from app.utils import utc_now, uuid7

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryMonitorResult:
    created_or_refreshed: int = 0
    resolved: int = 0
    diagnostics: tuple[dict[str, Any], ...] = ()
    skipped_locked: bool = False


def _terminal_success_condition():
    """SQL predicate for a run that may satisfy a delivery expectation."""
    return (
        IngestionRun.status == "completed",
        IngestionRun.total_records == IngestionRun.success_records,
        IngestionRun.failed_records == 0,
    )


def _bounded_diagnostic(dataset_key: str, source: str, reason: str) -> dict[str, str]:
    return {
        "dataset_key": dataset_key[:50],
        "source": source[:50],
        "reason": reason[:80],
    }


async def _sequenced_snapshot_complete(
    db: AsyncSession,
    *,
    dataset_key: str,
    source: str,
    schema_id: str,
    schema_version: int,
    data_date: date,
) -> bool:
    """Return whether one successful, coherent minute sequence group is done."""
    groups = await list_sequenced_snapshot_groups(
        db,
        dataset_key=dataset_key,
        source=source,
        schema_id=schema_id,
        schema_version=schema_version,
        data_date=data_date,
    )
    # A stale complete snapshot cannot supersede a newer failed/partial
    # coherent group for the same expected date.
    return bool(groups and groups[0].complete)


async def scan_missing_deliveries(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> DeliveryMonitorResult:
    """Resolve late arrivals and upsert one alert per missing identity/date."""
    evaluated_at = (now or utc_now()).astimezone(timezone.utc)
    datasets = list(
        (await db.execute(select(DatasetRegistry).where(DatasetRegistry.is_active.is_(True))))
        .scalars()
        .all()
    )
    await db.commit()
    diagnostics: list[dict[str, Any]] = []
    refreshed = 0
    resolved = 0
    skipped_locked = False
    for dataset in datasets:
        dataset_key = dataset.dataset_key
        dataset_config = dataset.config
        try:
            declaration = DatasetContractDeclaration.model_validate(dataset_config or {})
            expectation = parse_delivery_expectation(dataset_config)
        except ValueError:
            logger.warning(
                "Skipping invalid delivery monitor config for dataset=%s",
                dataset_key[:50],
            )
            diagnostics.append(_bounded_diagnostic(dataset_key, "", "invalid_dataset_contract"))
            continue
        if expectation is None or expectation.missing_delivery.action == "disabled":
            continue
        latest = expectation.latest_date
        if latest is None:
            diagnostics.append(
                _bounded_diagnostic(dataset_key, "", "latest_date_policy_not_configured")
            )
            continue

        for source in expectation.missing_delivery.expected_sources:
            acquired = await lock_feed_scope(
                db,
                dataset_key=dataset_key,
                source=source,
                schema_id=declaration.schema_id,
                schema_version=declaration.current_schema_version,
                wait=False,
            )
            if not acquired:
                skipped_locked = True
                await db.rollback()
                diagnostics.append(_bounded_diagnostic(dataset_key, source, "feed_scope_locked"))
                continue

            delivery_mode = expectation.delivery_mode.value
            resolved += await _resolve_open_alerts_for_feed(
                db,
                dataset_key=dataset_key,
                source=source,
                schema_id=declaration.schema_id,
                schema_version=declaration.current_schema_version,
                resolved_at=evaluated_at,
                delivery_mode=delivery_mode,
            )
            deadline = expectation.missing_delivery.deadline_local_time
            if delivery_mode == "sequenced_snapshot" or deadline is not None:
                resolver_kwargs: dict[str, Any] = {
                    "strict_current_session": True,
                }
                if deadline is not None:
                    resolver_kwargs["current_session_deadline"] = deadline
                    if expectation.schedule is not None:
                        resolver_kwargs["operational_deadline_timezone"] = (
                            expectation.schedule.timezone
                        )
                        resolver_kwargs["target_date_lag_days"] = (
                            expectation.schedule.target_date_lag_days
                        )
                expected_date, reason = await resolve_expected_data_date(
                    db,
                    latest,
                    evaluated_at,
                    **resolver_kwargs,
                )
            else:
                # Keep the legacy call shape for downstream test/fixture
                # adapters that wrap the resolver positionally.
                expected_date, reason = await resolve_expected_data_date(
                    db,
                    latest,
                    evaluated_at,
                )
            if expected_date is None:
                item = _bounded_diagnostic(dataset_key, source, reason or "calendar_unavailable")
                diagnostics.append(item)
                logger.warning("Missing delivery scan skipped: %s", item)
                await db.commit()
                continue

            if delivery_mode == "sequenced_snapshot":
                present = await _sequenced_snapshot_complete(
                    db,
                    dataset_key=dataset_key,
                    source=source,
                    schema_id=declaration.schema_id,
                    schema_version=declaration.current_schema_version,
                    data_date=expected_date,
                )
            else:
                present = bool(
                    await db.scalar(
                        select(
                            exists().where(
                                IngestionRun.dataset_key == dataset_key,
                                IngestionRun.source == source,
                                IngestionRun.schema_id == declaration.schema_id,
                                IngestionRun.schema_version == declaration.current_schema_version,
                                delivery_run_coverage_condition(delivery_mode, expected_date),
                                IngestionRun.is_rerun.is_(False),
                                *_terminal_success_condition(),
                            )
                        )
                    )
                )
            if present:
                await db.commit()
                continue
            details = {
                "code": "DATASET_DELIVERY_MISSING",
                "feed": f"{source}:{dataset_key}",
                "calendar_market": latest.calendar_market,
                "evaluated_at": evaluated_at.isoformat(),
            }
            statement = (
                insert(MissingDeliveryAlert)
                .values(
                    alert_id=uuid7(),
                    dataset_key=dataset_key,
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
        skipped_locked=skipped_locked,
    )


async def _resolve_open_alerts_for_feed(
    db: AsyncSession,
    *,
    dataset_key: str,
    source: str,
    schema_id: str,
    schema_version: int,
    resolved_at: datetime,
    delivery_mode: str = "full_snapshot",
) -> int:
    if delivery_mode == "sequenced_snapshot":
        alerts = (
            await db.execute(
                select(
                    MissingDeliveryAlert.alert_id,
                    MissingDeliveryAlert.expected_data_date,
                ).where(
                    MissingDeliveryAlert.status == "open",
                    MissingDeliveryAlert.dataset_key == dataset_key,
                    MissingDeliveryAlert.source == source,
                    MissingDeliveryAlert.schema_id == schema_id,
                    MissingDeliveryAlert.schema_version == schema_version,
                )
            )
        ).all()
        resolved_alert_ids = []
        for alert_id, expected_data_date in alerts:
            if await _sequenced_snapshot_complete(
                db,
                dataset_key=dataset_key,
                source=source,
                schema_id=schema_id,
                schema_version=schema_version,
                data_date=expected_data_date,
            ):
                resolved_alert_ids.append(alert_id)
        if not resolved_alert_ids:
            return 0
        resolution = await db.execute(
            update(MissingDeliveryAlert)
            .where(MissingDeliveryAlert.alert_id.in_(resolved_alert_ids))
            .values(status="resolved", resolved_at=resolved_at)
            .returning(MissingDeliveryAlert.alert_id)
        )
        return len(resolution.scalars().all())
    else:
        delivered = exists(
            select(IngestionRun.run_id).where(
                IngestionRun.dataset_key == dataset_key,
                IngestionRun.source == source,
                IngestionRun.schema_id == schema_id,
                IngestionRun.schema_version == schema_version,
                delivery_run_coverage_condition(
                    delivery_mode,
                    MissingDeliveryAlert.expected_data_date,
                ),
                IngestionRun.is_rerun.is_(False),
                *_terminal_success_condition(),
            )
        )
    resolution = await db.execute(
        update(MissingDeliveryAlert)
        .where(
            MissingDeliveryAlert.status == "open",
            MissingDeliveryAlert.dataset_key == dataset_key,
            MissingDeliveryAlert.source == source,
            MissingDeliveryAlert.schema_id == schema_id,
            MissingDeliveryAlert.schema_version == schema_version,
            delivered,
        )
        .values(status="resolved", resolved_at=resolved_at)
        .returning(MissingDeliveryAlert.alert_id)
    )
    return len(resolution.scalars().all())


async def resolve_missing_delivery_for_run(
    db: AsyncSession,
    *,
    run_id: UUID | None = None,
    dataset_key: str,
    source: str,
    schema_id: str,
    schema_version: int,
    data_date: date,
    resolved_at: datetime | None = None,
    delivery_mode: str | None = None,
) -> int:
    """Resolve the exact alert inside the accepting ingress transaction."""
    run = await db.get(IngestionRun, run_id) if run_id is not None else None
    if (
        run is None
        or run.is_rerun
        or run.status != "completed"
        or run.dataset_key != dataset_key
        or run.source != source
        or run.schema_id != schema_id
        or run.schema_version != schema_version
        or run.batch_data_date != data_date
        or run.total_records != run.success_records
        or run.failed_records != 0
    ):
        return 0
    if delivery_mode is None:
        dataset = await db.get(DatasetRegistry, dataset_key)
        expectation = parse_delivery_expectation(dataset.config if dataset else None)
        delivery_mode = expectation.delivery_mode.value if expectation else "full_snapshot"

    if delivery_mode == "sequenced_snapshot" and not await _sequenced_snapshot_complete(
        db,
        dataset_key=dataset_key,
        source=source,
        schema_id=schema_id,
        schema_version=schema_version,
        data_date=data_date,
    ):
        return 0

    resolution = await db.execute(
        update(MissingDeliveryAlert)
        .where(
            MissingDeliveryAlert.status == "open",
            MissingDeliveryAlert.dataset_key == dataset_key,
            MissingDeliveryAlert.source == source,
            MissingDeliveryAlert.schema_id == schema_id,
            MissingDeliveryAlert.schema_version == schema_version,
            MissingDeliveryAlert.expected_data_date == data_date,
        )
        .values(status="resolved", resolved_at=resolved_at or utc_now())
        .returning(MissingDeliveryAlert.alert_id)
    )
    return len(resolution.scalars().all())


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
