"""Durable monitoring for canonical full snapshots that never arrived."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import cast, exists, func, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.execution_profiles import ExecutionProfile, execution_profile
from app.models.registry import (
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DatasetRegistry,
    FullMarketDatasetState,
    IngestionRun,
    MissingDeliveryAlert,
)
from app.services.delivery_policy import (
    delivery_run_coverage_condition,
    parse_delivery_expectation,
    resolve_expected_data_date,
)
from app.services.feed_scope import lock_feed_scope
from app.services.full_market import _calendar_market, _deadline, reconcile_plan
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
        profile_states = list(
            await db.scalars(
                select(FullMarketDatasetState).where(
                    FullMarketDatasetState.dataset_key == dataset_key
                )
            )
        )
        if profile_states or ((dataset_config or {}).get("full_market") or {}).get("enabled"):
            profiles: list[ExecutionProfile] = [
                execution_profile(state.execution_profile) for state in profile_states
            ] or ["full_market"]
            for profile in profiles:
                for source in declaration.provider_scope():
                    created, closed = await _scan_full_market_feed(
                        db,
                        dataset=dataset,
                        source=source,
                        schema_id=declaration.schema_id,
                        schema_version=declaration.current_schema_version,
                        evaluated_at=evaluated_at,
                        profile=profile,
                    )
                    refreshed += created
                    resolved += closed
            await db.commit()
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


async def _scan_full_market_feed(
    db: AsyncSession,
    *,
    dataset: DatasetRegistry,
    source: str,
    schema_id: str,
    schema_version: int,
    evaluated_at: datetime,
    profile: ExecutionProfile = "full_market",
) -> tuple[int, int]:
    governance = (dataset.config or {}).get("full_market") or {}
    state = await db.get(
        FullMarketDatasetState, {"dataset_key": dataset.dataset_key, "execution_profile": profile}
    )
    start_value = (
        state.first_start_date.isoformat()
        if state
        else (governance.get("activation_date") if profile == "full_market" else None)
    )
    if not start_value:
        return 0, 0
    start_date = date.fromisoformat(start_value)
    from zoneinfo import ZoneInfo

    from app.services.effective_calendar import acquisition_dates
    from app.services.full_market_governance import feed_schedule

    zone, _ = feed_schedule(dataset.dataset_key)
    through = evaluated_at.astimezone(ZoneInfo(zone)).date()
    if dataset.dataset_key == "hk_equity_eod":
        cutover = (dataset.config or {}).get("hk_provider_cutover")
        if source == "factset":
            if not cutover:
                return 0, 0
            start_date = max(start_date, date.fromisoformat(cutover["effective_date"]))
        elif source == "twelve_data" and cutover:
            # The explicit acquisition switch retires Twelve Data HK monitoring.
            # Earlier gaps are recovered through FactSet historical requests, so
            # open retired-provider alerts are closed; their details are kept.
            retired = await db.execute(
                update(MissingDeliveryAlert)
                .where(
                    MissingDeliveryAlert.dataset_key == dataset.dataset_key,
                    MissingDeliveryAlert.source == source,
                    MissingDeliveryAlert.execution_profile == profile,
                    MissingDeliveryAlert.status == "open",
                )
                .values(
                    status="resolved",
                    resolved_at=evaluated_at,
                    details=func.coalesce(MissingDeliveryAlert.details, cast({}, JSONB)).op("||")(
                        cast(
                            {
                                "resolution": {
                                    "code": "PROVIDER_RETIRED",
                                    "provider": cutover["provider"],
                                    "effective_date": cutover["effective_date"],
                                }
                            },
                            JSONB,
                        )
                    ),
                )
                .returning(MissingDeliveryAlert.alert_id)
            )
            return 0, len(retired.scalars().all())
        elif source == "twelve_data":
            # Retained v1 delivery keeps its original alert identity.
            schema_version = 1
    if through < start_date:
        return 0, 0
    dates = await acquisition_dates(
        db,
        market=_calendar_market(dataset),
        dataset_key=dataset.dataset_key,
        provider=source,
        profile=profile,
        start=start_date,
        end=through,
    )
    plans = list(
        (
            await db.scalars(
                select(DailyDeliveryPlan).where(
                    DailyDeliveryPlan.dataset_key == dataset.dataset_key,
                    DailyDeliveryPlan.provider == source,
                    DailyDeliveryPlan.execution_profile == profile,
                    DailyDeliveryPlan.trade_date >= start_date,
                )
            )
        ).all()
    )
    by_date = {plan.trade_date: plan for plan in plans}
    created = 0
    policy_resolved = await db.execute(
        update(MissingDeliveryAlert)
        .where(
            MissingDeliveryAlert.dataset_key == dataset.dataset_key,
            MissingDeliveryAlert.source == source,
            MissingDeliveryAlert.execution_profile == profile,
            MissingDeliveryAlert.status == "open",
            MissingDeliveryAlert.expected_data_date >= start_date,
            MissingDeliveryAlert.expected_data_date <= through,
            MissingDeliveryAlert.expected_data_date.not_in(dates),
        )
        .values(
            status="resolved",
            resolved_at=evaluated_at,
            details={"code": "SKIPPED_CALENDAR", "reason": "provider_policy"},
        )
        .returning(MissingDeliveryAlert.alert_id)
    )
    resolved = len(policy_resolved.scalars().all())
    gap_streaks: dict[str, int] = {}
    unplanned_streak = 0
    for trade_date in dates:
        deadline = _deadline(dataset, trade_date)
        if evaluated_at < deadline:
            continue
        plan = by_date.get(trade_date)
        summary = await reconcile_plan(db, plan, now=evaluated_at, gap_limit=None) if plan else None
        # Only exchange-expected dates enter this loop. Data or durable no_data
        # removes that member from gaps and therefore resets its streak. Without
        # a frozen plan, escalate the feed gap without inventing member scope.
        gap_streaks = advance_gap_streaks(gap_streaks, summary.gaps if summary else [])
        unplanned_streak = 0 if plan else unplanned_streak + 1
        complete = bool(summary and summary.data + summary.no_data == summary.expected)
        if complete:
            resolution = await db.execute(
                update(MissingDeliveryAlert)
                .where(
                    MissingDeliveryAlert.dataset_key == dataset.dataset_key,
                    MissingDeliveryAlert.source == source,
                    MissingDeliveryAlert.execution_profile == profile,
                    MissingDeliveryAlert.schema_id == schema_id,
                    MissingDeliveryAlert.schema_version == schema_version,
                    MissingDeliveryAlert.expected_data_date == trade_date,
                    MissingDeliveryAlert.status == "open",
                )
                .values(status="resolved", resolved_at=evaluated_at)
                .returning(MissingDeliveryAlert.alert_id)
            )
            resolved += len(resolution.scalars().all())
            continue
        details: dict[str, Any] = {
            "code": "FULL_MARKET_INCOMPLETE" if profile == "full_market" else "PILOT_INCOMPLETE",
            "execution_profile": profile,
            "plan_id": str(plan.plan_id) if plan else None,
            "expected": summary.expected if summary else None,
            "data": summary.data if summary else 0,
            "no_data": summary.no_data if summary else 0,
            "missing": summary.missing if summary else None,
            "blocked": summary.blocked if summary else None,
            "deadline_at": deadline.isoformat(),
        }
        if source == "factset" and schema_id == "market_eod":
            threshold = get_settings().EOD_LONG_GAP_TRADING_DAYS
            long_gaps = [
                {"member_key": key, "consecutive_trading_days": count}
                for key, count in sorted(gap_streaks.items())
                if count >= threshold
            ]
            details.update(
                severity="error" if long_gaps or unplanned_streak >= threshold else "warning",
                long_gap_threshold=threshold,
                long_gap_members=long_gaps,
                consecutive_unplanned_days=unplanned_streak,
            )
        await db.execute(
            insert(MissingDeliveryAlert)
            .values(
                alert_id=uuid7(),
                dataset_key=dataset.dataset_key,
                source=source,
                execution_profile=profile,
                schema_id=schema_id,
                schema_version=schema_version,
                expected_data_date=trade_date,
                status="open",
                first_detected_at=evaluated_at,
                last_detected_at=evaluated_at,
                details=details,
            )
            .on_conflict_do_update(
                constraint="uq_missing_delivery_identity_date",
                set_={
                    "status": "open",
                    "resolved_at": None,
                    "last_detected_at": evaluated_at,
                    "details": details,
                },
            )
        )
        created += 1
    return created, resolved


def advance_gap_streaks(previous: dict[str, int], gaps: list[dict]) -> dict[str, int]:
    """A valid observation or authoritative no-data breaks a member's streak."""
    return {row["member_key"]: previous.get(row["member_key"], 0) + 1 for row in gaps}


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
                    MissingDeliveryAlert.execution_profile == "pilot",
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
                IngestionRun.delivery_part_id.is_(None),
                *_terminal_success_condition(),
            )
        )
    resolution = await db.execute(
        update(MissingDeliveryAlert)
        .where(
            MissingDeliveryAlert.status == "open",
            MissingDeliveryAlert.dataset_key == dataset_key,
            MissingDeliveryAlert.source == source,
            MissingDeliveryAlert.execution_profile == "pilot",
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
    profile = "pilot"
    if run.delivery_part_id is not None:
        part = await db.get(DailyDeliveryPart, run.delivery_part_id)
        plan = await db.get(DailyDeliveryPlan, part.plan_id) if part else None
        if plan is None:
            return 0
        profile = plan.execution_profile
        summary = await reconcile_plan(db, plan)
        if summary.data + summary.no_data != summary.expected:
            return 0
    if delivery_mode is None:
        dataset = await db.get(DatasetRegistry, dataset_key)
        expectation = parse_delivery_expectation(dataset.config if dataset else None)
        delivery_mode = expectation.delivery_mode.value if expectation else "full_snapshot"

    if (
        run.delivery_part_id is None
        and delivery_mode == "sequenced_snapshot"
        and not await _sequenced_snapshot_complete(
            db,
            dataset_key=dataset_key,
            source=source,
            schema_id=schema_id,
            schema_version=schema_version,
            data_date=data_date,
        )
    ):
        return 0

    resolution = await db.execute(
        update(MissingDeliveryAlert)
        .where(
            MissingDeliveryAlert.status == "open",
            MissingDeliveryAlert.dataset_key == dataset_key,
            MissingDeliveryAlert.source == source,
            MissingDeliveryAlert.execution_profile == profile,
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
    profile: ExecutionProfile | None = None,
) -> tuple[list[MissingDeliveryAlert], int]:
    conditions = []
    if profile is not None:
        conditions.append(MissingDeliveryAlert.execution_profile == profile)
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
