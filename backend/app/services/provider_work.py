"""PostgreSQL is the FIFO authority; callers retain the independent host flock.

Global transaction lock order: provider FIFO -> full-market environment/dataset/
calendar/control -> ingestion idempotency -> delivery policy -> run/job rows.
No owner is relinquished by a clock. Recovery requires the same host lock inode,
which can only be acquired after every inheriting provider subprocess has exited.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any, TypedDict
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Row, exists, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkOwner,
    ProviderWorkReceipt,
)
from app.models.registry import IngestionRun, SchedulerControl
from app.services.daily_runtime import daily_window
from app.utils import utc_now, uuid7

HEARTBEAT_TTL = timedelta(minutes=2)
TERMINAL_RUNS = {"completed", "completed_with_errors", "failed", "cancelled"}


class ProviderWorkError(ValueError):
    pass


class ProviderWorkDrainingError(ProviderWorkError):
    """Accepted normalization is still running; acquisition stays closed."""

    code = "PROVIDER_WORK_DRAINING"


class ProviderWorkPreparedDrainingError(ProviderWorkDrainingError):
    """Original preparation must drain or safely tail-retry."""

    code = "PROVIDER_WORK_PREPARED_DRAINING"


class ProviderWorkSuspendedError(ProviderWorkError):
    """Stop/cancel prohibits acquisition while retained preparation can drain."""


async def daily_readiness(db: AsyncSession, *, provider: str, values: dict) -> dict:
    """Freeze the first enqueue's published close before any provider I/O."""
    from app.models.registry import DatasetRegistry
    from app.services.effective_calendar import effective_days

    await lock_provider(db, provider)
    existing = await db.scalar(
        select(ProviderWork).where(
            ProviderWork.environment == environment(),
            ProviderWork.provider == provider,
            ProviderWork.stable_key == values["stable_key"],
        )
    )
    if existing is not None:
        # Enqueue replays cannot rewrite a previously frozen calendar or an
        # imported legacy scope. The ordinary immutable-scope check still runs.
        if "calendar_snapshot" in existing.scope:
            values["scope"] = {
                **values["scope"],
                "calendar_snapshot": existing.scope["calendar_snapshot"],
            }
        return values
    dataset = await db.get(DatasetRegistry, values["dataset_key"])
    if dataset is None:
        raise ProviderWorkError("unknown provider work dataset")
    day = values["trade_date"]
    snapshot = (
        await effective_days(db, "TAIFEX" if provider == "taifex" else dataset.market, day, day)
    )[0]
    closing = time.fromisoformat(snapshot["regular_close"])
    if provider == "taifex":
        closing = max(closing, time(17))
    elif provider == "factset":
        closing = max(closing, time(18, 30))
    ready = datetime.combine(day, closing, ZoneInfo(snapshot["timezone"]))
    requested = values["not_before"]
    if requested is not None and requested.tzinfo is None:
        raise ProviderWorkError("not_before must be timezone-aware")
    values["not_before"] = max(ready, requested) if requested else ready
    values["scope"] = {**values["scope"], "calendar_snapshot": snapshot}
    return values


class FenceIdentity(TypedDict):
    provider: str
    work_id: UUID
    generation: int
    token: UUID


def environment() -> str:
    return get_settings().APP_ENVIRONMENT


async def lock_provider(db: AsyncSession, provider: str) -> ProviderWorkOwner:
    """Serialize enqueue, claim, acceptance, close and settlement until commit."""
    env = environment()
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"provider-work:{env}:{provider}"},
    )
    owner = await db.get(ProviderWorkOwner, (env, provider), populate_existing=True)
    if owner is None:
        owner = ProviderWorkOwner(environment=env, provider=provider, generation=0)
        db.add(owner)
        await db.flush()
    return owner


async def enqueue(
    db: AsyncSession,
    *,
    provider: str,
    stable_key: str,
    kind: str,
    dataset_key: str,
    trade_date: date | None,
    execution_profile: str | None = None,
    scope: dict | None = None,
    not_before: datetime | None = None,
) -> ProviderWork:
    if not_before is not None and not_before.tzinfo is None:
        raise ProviderWorkError("not_before must be timezone-aware")
    await lock_provider(db, provider)
    existing = await db.scalar(
        select(ProviderWork).where(
            ProviderWork.environment == environment(),
            ProviderWork.provider == provider,
            ProviderWork.stable_key == stable_key,
        )
    )
    immutable = (kind, dataset_key, trade_date, execution_profile, scope or {})
    if existing is not None:
        if (
            existing.scope.get("legacy_id")
            and (
                existing.kind,
                existing.dataset_key,
                existing.trade_date,
                existing.execution_profile,
            )
            == immutable[:4]
        ):
            return existing
        if (
            existing.kind,
            existing.dataset_key,
            existing.trade_date,
            existing.execution_profile,
            existing.scope,
        ) != immutable:
            raise ProviderWorkError("stable work identity is already bound to another scope")
        return existing
    work = ProviderWork(
        work_id=uuid7(),
        environment=environment(),
        provider=provider,
        stable_key=stable_key,
        kind=kind,
        dataset_key=dataset_key,
        trade_date=trade_date,
        execution_profile=execution_profile,
        scope=scope or {},
    )
    db.add(work)
    await db.flush()
    db.add(
        ProviderWorkAttempt(
            work_id=work.work_id, attempt_number=1, not_before=not_before or utc_now()
        )
    )
    await db.flush()
    return work


async def claim(
    db: AsyncSession,
    *,
    provider: str,
    datasets: list[str],
    host_lock_identity: str,
    require_cutover: bool = False,
    recover: bool = False,
    requested_work_id: UUID | None = None,
    accepted_kinds: list[str] | None = None,
    execution_profile: str | None = None,
    now: datetime | None = None,
) -> tuple[ProviderWork, ProviderWorkAttempt, ProviderWorkOwner] | None:
    now = now or utc_now()
    owner = await lock_provider(db, provider)
    if require_cutover and owner.host_lock_identity is None:
        raise ProviderWorkError("provider work requires verified cutover preflight")
    if owner.host_lock_identity and owner.host_lock_identity != host_lock_identity:
        raise ProviderWorkError("provider shared host lock inode differs")
    if owner.recovery_reason and owner.attempt_id is None:
        raise ProviderWorkError("legacy provider work requires verified cutover drain")
    if owner.attempt_id is not None:
        attempt = await db.get(ProviderWorkAttempt, owner.attempt_id)
        assert attempt is not None
        work = await db.get(ProviderWork, attempt.work_id)
        assert work is not None
        if (
            accepted_kinds is not None
            and work.kind not in accepted_kinds
            or execution_profile is not None
            and work.kind == "daily"
            and work.execution_profile != execution_profile
        ):
            return None
        if requested_work_id is not None and work.work_id != requested_work_id:
            return None
        if work.dataset_key not in datasets:
            return None
        if not recover:
            if owner.expires_at is None or owner.expires_at <= now:
                owner.recovery_reason = "heartbeat_missing"
                if attempt.state == "open":
                    attempt.state = "recovery_blocked"
                work.state = "recovery_blocked"
            return None
        if owner.host_lock_identity != host_lock_identity:
            raise ProviderWorkError("recovery requires the original shared host lock inode")
        # Reacquiring the same OS lock proves descendants have released their
        # descriptors. Resume this owner, never skip to the next FIFO entry.
        attempt.state = "open" if attempt.state == "recovery_blocked" else attempt.state
        work.state = "running"
    else:
        from app.services.historical_backfill import reconcile_cancelled_queue

        await reconcile_cancelled_queue(db, provider=provider, now=now)
        candidate = (
            await db.execute(
                select(ProviderWork, ProviderWorkAttempt)
                .join(ProviderWorkAttempt)
                .where(
                    ProviderWork.environment == environment(),
                    ProviderWork.provider == provider,
                    ProviderWork.state == "queued",
                    ProviderWorkAttempt.state == "queued",
                    ProviderWorkAttempt.not_before <= now,
                    or_(
                        ProviderWork.kind != "daily",
                        ProviderWorkAttempt.proof["source_error_pending"].as_boolean().is_(True),
                        literal(daily_window(provider, now)["open"]),
                        text("""EXISTS (
                          SELECT 1 FROM jsonb_object_keys(provider_work.prepared_deliveries) AS prepared(member_key)
                          WHERE NOT EXISTS (SELECT 1 FROM provider_work_receipt receipt
                            WHERE receipt.work_id=provider_work.work_id AND receipt.member_key=prepared.member_key)
                        )"""),
                        exists(
                            select(ProviderWorkReceipt.run_id)
                            .join(IngestionRun)
                            .where(
                                ProviderWorkReceipt.work_id == ProviderWork.work_id,
                                IngestionRun.status.not_in(TERMINAL_RUNS),
                            )
                        ),
                    ),
                    or_(
                        ProviderWork.kind == "rerun",
                        ProviderWorkAttempt.proof["source_error_pending"].as_boolean().is_(True),
                        text("""EXISTS (
                          SELECT 1 FROM jsonb_object_keys(provider_work.prepared_deliveries) AS prepared(member_key)
                          WHERE NOT EXISTS (SELECT 1 FROM provider_work_receipt receipt
                            WHERE receipt.work_id=provider_work.work_id AND receipt.member_key=prepared.member_key)
                        )"""),
                        exists(
                            select(ProviderWorkReceipt.run_id)
                            .join(IngestionRun)
                            .where(
                                ProviderWorkReceipt.work_id == ProviderWork.work_id,
                                IngestionRun.status.not_in(TERMINAL_RUNS),
                            )
                        ),
                        literal(
                            owner.acquisition_not_before is None
                            or owner.acquisition_not_before <= now
                        ),
                    ),
                    or_(
                        ProviderWork.kind != "daily",
                        ProviderWorkAttempt.proof["source_error_pending"].as_boolean().is_(True),
                        text("""EXISTS (
                          SELECT 1 FROM jsonb_object_keys(provider_work.prepared_deliveries) AS prepared(member_key)
                          WHERE NOT EXISTS (SELECT 1 FROM provider_work_receipt receipt
                            WHERE receipt.work_id=provider_work.work_id AND receipt.member_key=prepared.member_key)
                        )"""),
                        exists(
                            select(ProviderWorkReceipt.run_id)
                            .join(IngestionRun)
                            .where(
                                ProviderWorkReceipt.work_id == ProviderWork.work_id,
                                IngestionRun.status.not_in(TERMINAL_RUNS),
                            )
                        ),
                        exists(
                            select(SchedulerControl.scheduler_key).where(
                                SchedulerControl.provider == ProviderWork.provider,
                                SchedulerControl.scheduler_key
                                == ProviderWork.execution_profile
                                + "_"
                                + ProviderWork.provider
                                + "_v1",
                                SchedulerControl.desired_state == "running",
                                SchedulerControl.retired_at.is_(None),
                            )
                        ),
                    ),
                )
                .order_by(ProviderWorkAttempt.queue_order)
                .limit(1)
            )
        ).first()
        if candidate is None:
            return None
        work, attempt = candidate
        if work.dataset_key not in datasets:
            return None
        if (
            accepted_kinds is not None
            and work.kind not in accepted_kinds
            or execution_profile is not None
            and work.kind == "daily"
            and work.execution_profile != execution_profile
        ):
            return None
        if requested_work_id is not None and work.work_id != requested_work_id:
            return None
        attempt.state, attempt.started_at, work.state = "open", now, "running"
        can_acquire = await acquisition_open(db, work)
        if work.kind in {"daily", "historical"} and attempt.drain_only and can_acquire:
            # A legal start may resume the original frozen remaining scope, but
            # only in a new round after the prior preparation/drain is terminal.
            accepted = (
                await db.execute(
                    select(ProviderWorkReceipt.member_key, IngestionRun.status)
                    .join(IngestionRun)
                    .where(ProviderWorkReceipt.work_id == work.work_id)
                )
            ).all()
            if set(work.prepared_deliveries or {}) <= {row.member_key for row in accepted} and all(
                row.status in TERMINAL_RUNS for row in accepted
            ):
                attempt.drain_only = False
        attempt.drain_only = (
            attempt.drain_only
            or not can_acquire
            or bool(
                work.kind != "rerun"
                and owner.acquisition_not_before
                and owner.acquisition_not_before > now
            )
        )
        owner.attempt_id = attempt.attempt_id
    if work.kind != "rerun" and owner.acquisition_not_before and owner.acquisition_not_before > now:
        # Recovery of an open round may follow a committed cooldown whose ACK
        # was lost. It can drain/close, never reacquire during that floor.
        attempt.drain_only = True
    owner.generation += 1
    owner.token = uuid7()
    owner.host_lock_identity = host_lock_identity
    owner.heartbeat_at, owner.expires_at = now, now + HEARTBEAT_TTL
    owner.recovery_reason = None
    attempt.generation = owner.generation
    attempt.token = owner.token
    await db.flush()
    return work, attempt, owner


async def fence(
    db: AsyncSession,
    *,
    provider: str,
    work_id: UUID,
    generation: int,
    token: UUID,
    acquisition: bool = False,
) -> tuple[ProviderWork, ProviderWorkAttempt, ProviderWorkOwner]:
    owner = await lock_provider(db, provider)
    attempt = await db.get(ProviderWorkAttempt, owner.attempt_id) if owner.attempt_id else None
    if (
        attempt is None
        or attempt.work_id != work_id
        or owner.generation != generation
        or owner.token != token
    ):
        raise ProviderWorkError("provider work owner fence differs")
    work = await db.get(ProviderWork, work_id)
    assert work is not None
    if owner.recovery_reason or owner.expires_at is None or owner.expires_at <= utc_now():
        raise ProviderWorkError("provider work requires host recovery")
    if (
        acquisition
        and work.kind != "rerun"
        and owner.acquisition_not_before
        and owner.acquisition_not_before > utc_now()
    ):
        raise ProviderWorkSuspendedError("provider account cooldown is pending")
    if acquisition and (attempt.state != "open" or work.state != "running" or attempt.drain_only):
        raise ProviderWorkError("provider work acquisition is closed")
    if acquisition and work.provider == "factset" and work.kind == "daily":
        local_time = utc_now().astimezone(ZoneInfo("Asia/Hong_Kong")).time()
        if not time(18, 30) <= local_time < time(23):
            raise ProviderWorkSuspendedError("FactSet daily acquisition window is closed")
    if (
        acquisition
        and work.kind in {"historical", "daily", "maintenance"}
        and not await acquisition_open(db, work)
    ):
        raise ProviderWorkSuspendedError("provider acquisition is stopped or expired")
    return work, attempt, owner


async def heartbeat(
    db: AsyncSession, *, acquisition_not_before: datetime | None = None, **identity
) -> None:
    _, _, owner = await fence(db, **identity)
    if acquisition_not_before is not None:
        if acquisition_not_before.tzinfo is None:
            raise ProviderWorkError("provider account cooldown requires UTC-aware time")
        owner.acquisition_not_before = max(
            owner.acquisition_not_before or acquisition_not_before, acquisition_not_before
        )
    owner.heartbeat_at = utc_now()
    owner.expires_at = owner.heartbeat_at + HEARTBEAT_TTL


async def freeze_manifest(db: AsyncSession, *, manifest: dict, **identity) -> None:
    work, _, _ = await fence(db, **identity)
    if work.manifest == manifest:
        return
    if work.manifest is not None:
        raise ProviderWorkError("expected scope is already frozen")
    await fence(db, acquisition=True, **identity)
    if work.kind == "daily" and not await acquisition_open(db, work):
        raise ProviderWorkSuspendedError("daily acquisition is stopped")
    if (
        not manifest
        or not isinstance(manifest.get("members"), list)
        or not 1 <= len(manifest["members"]) <= 100000
    ):
        raise ProviderWorkError("frozen expected member manifest is required")
    keys = [row.get("key") for row in manifest["members"] if isinstance(row, dict)]
    if (
        len(keys) != len(manifest["members"])
        or any(not isinstance(key, str) or not key or len(key) > 200 for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise ProviderWorkError("expected member identities must be unique")
    if work.manifest is not None and work.manifest != manifest:
        raise ProviderWorkError("expected scope is already frozen")
    if "contract" in manifest:
        from app.models.registry import DatasetRegistry
        from app.services.ingress_contracts import parse_dataset_contract_declaration

        dataset = await db.get(DatasetRegistry, work.dataset_key)
        declaration = parse_dataset_contract_declaration(dataset.config) if dataset else None
        if (
            manifest["contract"] != {"schema_id": "market_eod", "schema_version": 2}
            or work.kind not in {"daily", "historical"}
            or declaration is None
            or declaration.schema_id != "market_eod"
            or 2 not in declaration.accepted_schema_versions
            or work.provider not in declaration.provider_scope()
        ):
            raise ProviderWorkError("frozen EOD v2 contract is outside the dataset scope")
    if work.kind == "daily":
        from app.models.registry import (
            DailyDeliveryMember,
            DailyDeliveryPlan,
            DailyExecutionDecision,
        )

        if manifest.get("calendar_skip"):
            decision = await db.get(
                DailyExecutionDecision,
                (work.dataset_key, work.provider, work.trade_date, work.execution_profile),
            )
            if decision is None or decision.kind != "skipped_calendar" or keys != ["calendar_skip"]:
                raise ProviderWorkError("calendar skip lacks frozen Source decision")
        else:
            try:
                plan = await db.get(DailyDeliveryPlan, UUID(manifest["plan_id"]))
            except (ValueError, KeyError):
                plan = None
            expected = (
                set(
                    await db.scalars(
                        select(DailyDeliveryMember.member_key).where(
                            DailyDeliveryMember.plan_id == plan.plan_id
                        )
                    )
                )
                if plan
                else set()
            )
            if (
                plan is None
                or plan.provider != work.provider
                or plan.dataset_key != work.dataset_key
                or plan.trade_date != work.trade_date
                or plan.execution_profile != work.execution_profile
                or set(keys) != expected
            ):
                raise ProviderWorkError("daily manifest differs from frozen plan scope")
            if "contract" in manifest:
                frozen = {
                    member.member_key: member
                    for member in await db.scalars(
                        select(DailyDeliveryMember).where(
                            DailyDeliveryMember.plan_id == plan.plan_id
                        )
                    )
                }
                if any(
                    row.get("mapping_status") != frozen[row["key"]].mapping_status
                    or row.get("source_symbol") != frozen[row["key"]].provider_symbol
                    for row in manifest["members"]
                ):
                    raise ProviderWorkError("EOD manifest mapping differs from the frozen plan")
    elif work.kind == "historical" and "contract" in manifest:
        from app.models.registry import HistoricalBackfillItem, HistoricalBackfillRequest

        try:
            item = await db.get(HistoricalBackfillItem, UUID(work.scope["item_id"]))
            request = await db.get(HistoricalBackfillRequest, item.request_id) if item else None
        except (ValueError, KeyError):
            request = None
        snapshot = request.universe_snapshot if request else None
        if not snapshot or manifest.get("universe_release_id") != snapshot["release_id"]:
            raise ProviderWorkError("EOD historical manifest lacks the request's frozen cohort")
        expected_rows = {
            row["member_key"]: (row["mapping_status"], row["provider_symbol"])
            for row in snapshot["members"]
        }
        observed_rows = {
            row["key"]: (row.get("mapping_status"), row.get("source_symbol"))
            for row in manifest["members"]
        }
        if observed_rows != expected_rows:
            raise ProviderWorkError("EOD historical manifest differs from the frozen cohort")
    work.manifest = manifest


async def acceptance(
    db: AsyncSession, *, provider: str, dataset_key: str, trade_date: date | None
) -> ProviderWork:
    """Lock before any other ingest locks, even on duplicate fast paths."""
    identity = db.info.get("provider_work")
    if identity is None:
        raise ProviderWorkError("provider work protocol is required")
    owner = await lock_provider(db, provider)
    work = await db.get(ProviderWork, identity["work_id"])
    if (
        work is None
        or work.environment != environment()
        or work.provider != provider
        or work.dataset_key != dataset_key
        or work.trade_date != trade_date
    ):
        raise ProviderWorkError("provider work delivery scope differs")
    descriptor = (work.prepared_deliveries or {}).get(identity["member_key"])
    if descriptor and descriptor["body_sha256"] != db.info.get("provider_work_body_sha256"):
        raise ProviderWorkError("prepared delivery bytes differ from retained descriptor")
    if (
        work.state not in {"completed", "partial", "failed", "cancelled"}
        and (not await acquisition_open(db, work))
        and (descriptor is None)
    ):
        raise ProviderWorkError("stopped work only accepts previously registered prepared bytes")
    # Receipt replay is checked separately after exact idempotency lookup. A
    # closed/settled work may replay its own receipt but accept no new body.
    db.info["provider_work_locked_owner"] = owner
    return work


async def acquisition_open(
    db: AsyncSession, work: ProviderWork, *, check_window: bool = True
) -> bool:
    if work.kind != "rerun" and work.dataset_key == "hk_equity_eod":
        from app.models.registry import DatasetRegistry
        from app.services.hk_provider_cutover import acquisition_allowed

        dataset = await db.get(DatasetRegistry, work.dataset_key)
        if dataset is None or not acquisition_allowed(
            dataset,
            work.provider,
            trade_date=work.trade_date if work.kind == "daily" else None,
            maintenance=work.kind == "maintenance",
        ):
            return False
    if work.kind == "daily":
        if check_window and not daily_window(work.provider)["open"]:
            return False
        control = await db.get(SchedulerControl, f"{work.execution_profile}_{work.provider}_v1")
        return control is not None and control.desired_state == "running"
    if work.kind == "historical":
        from app.models.registry import HistoricalBackfillItem, HistoricalBackfillRequest
        from app.services.historical_backfill import expiry_cutoff

        try:
            item_id = UUID(work.scope.get("item_id", work.scope.get("legacy_id")))
        except (ValueError, TypeError):
            return False
        return bool(
            await db.scalar(
                select(HistoricalBackfillItem.item_id)
                .join(HistoricalBackfillRequest)
                .where(
                    HistoricalBackfillItem.item_id == item_id,
                    HistoricalBackfillItem.status.in_(("pending", "running")),
                    HistoricalBackfillRequest.status.in_(("queued", "running")),
                    HistoricalBackfillItem.trade_date >= expiry_cutoff(work.provider, utc_now()),
                )
            )
        )
    return True


async def current_calendar_status(db: AsyncSession, work: ProviderWork) -> dict:
    """Compare a frozen daily decision with today's published calendar, without rewriting it."""
    if work.kind != "daily" or work.trade_date is None or work.execution_profile is None:
        raise ProviderWorkError("calendar status requires daily provider work")
    from app.models.registry import DailyExecutionDecision, DatasetRegistry
    from app.services.effective_calendar import execution_snapshot

    dataset = await db.get(DatasetRegistry, work.dataset_key)
    decision = await db.get(
        DailyExecutionDecision,
        (work.dataset_key, work.provider, work.trade_date, work.execution_profile),
    )
    if dataset is None or decision is None:
        raise ProviderWorkError("calendar status requires a frozen daily decision")
    current = await execution_snapshot(
        db,
        "TAIFEX" if dataset.dataset_key == "tw_futures_eod" else dataset.market,
        work.trade_date,
        work.provider,
    )
    frozen_source = decision.snapshot.get("calendar_source")
    frozen_revision_id = decision.snapshot.get("calendar_revision_id")
    repair_required = bool(
        decision.kind == "planned"
        and current["calendar_source"] == "published"
        and not current["is_open"]
        and (
            frozen_source != "published"
            or current.get("calendar_revision_id") != frozen_revision_id
        )
    )
    return {
        "work_id": str(work.work_id),
        "trade_date": work.trade_date.isoformat(),
        "frozen_kind": decision.kind,
        "frozen_calendar_source": frozen_source,
        "frozen_calendar_revision_id": frozen_revision_id,
        "current_is_open": current["is_open"],
        "current_calendar_source": current["calendar_source"],
        "current_calendar_revision_id": current.get("calendar_revision_id"),
        "repair_required": repair_required,
    }


async def register_prepared(
    db: AsyncSession, *, body: str, stage: str, job_key: str | None, **identity
) -> None:
    """Bind immutable bytes before stop/cancel, under the cancellation lock."""
    from app.services.ingestion import payload_sha256
    from app.services.ingress_contracts import validate_ingress_request

    work, attempt, _ = await fence(db, **identity)
    try:
        request = validate_ingress_request(json.loads(body))
    except ValueError as exc:
        raise ProviderWorkError("prepared contract is invalid") from exc
    payload = request.payload.model_dump(mode="json")
    members = {
        f"{row['product_code']}|{row['contract_code']}|{row['session']}"
        if request.schema_id == "futures_eod"
        else row["symbol"]
        for row in payload["data"]
    }
    expected = {row["key"] for row in (work.manifest or {}).get("members", [])}
    if (
        request.source != work.provider
        or request.dataset_key != work.dataset_key
        or request.payload.batch.data_date != work.trade_date
        or not members
        or not members <= expected
    ):
        raise ProviderWorkError("prepared delivery is outside frozen expected scope")
    descriptor = {
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "payload_sha256": payload_sha256(payload),
        "canonical_members": sorted(members),
    }
    previous = (work.prepared_deliveries or {}).get(request.idempotency_key)
    if previous is not None:
        if any(previous[key] != value for key, value in descriptor.items()) or (
            job_key is not None and previous.get("job_key") != job_key
        ):
            raise ProviderWorkError("prepared delivery descriptor cannot change")
        if stage == "confirm" and not previous.get("confirmed"):
            work.prepared_deliveries = {
                **work.prepared_deliveries,
                request.idempotency_key: {**previous, "confirmed": True},
            }
        return
    if stage != "intent":
        raise ProviderWorkError("durable preparation intent is required before local save")
    if job_key is not None and any(
        row.get("job_key") == job_key for row in (work.prepared_deliveries or {}).values()
    ):
        raise ProviderWorkError("prepared job identity is already bound")
    if attempt.state != "open" or attempt.drain_only:
        raise ProviderWorkError("closed work cannot create a preparation intent")
    # A call dispatched inside the window may finish after closure. Binding its
    # response is persistence, not permission for another provider acquisition.
    # Keep control/cancellation checks; only the daily clock is excluded here.
    if not await acquisition_open(db, work, check_window=False):
        # Imported accepted receipts may bind their original local prepared
        # bytes; this never creates a new run or permits new provider I/O.
        accepted = await db.scalar(
            select(ProviderWorkReceipt)
            .join(IngestionRun)
            .where(
                ProviderWorkReceipt.work_id == work.work_id,
                ProviderWorkReceipt.member_key == request.idempotency_key,
                ProviderWorkReceipt.payload_sha256 == descriptor["payload_sha256"],
            )
        )
        if accepted is None:
            raise ProviderWorkError("new preparation is stopped")
    work.prepared_deliveries = {
        **(work.prepared_deliveries or {}),
        request.idempotency_key: {
            **descriptor,
            "body": body,
            "job_key": job_key,
            "confirmed": False,
        },
    }


async def associate_receipt(
    db: AsyncSession,
    *,
    work: ProviderWork | None,
    run_id: UUID,
    payload_sha256: str,
    replay: bool = False,
    canonical_members: set[str] | None = None,
) -> None:
    if work is None:
        return
    identity = db.info["provider_work"]
    existing = await db.get(ProviderWorkReceipt, run_id)
    if replay:
        if (
            existing is None
            or existing.work_id != work.work_id
            or existing.member_key != identity["member_key"]
            or existing.payload_sha256 != payload_sha256
        ):
            raise ProviderWorkError("receipt replay belongs to another stable work")
        return
    _, attempt, _ = await fence(
        db,
        provider=work.provider,
        work_id=work.work_id,
        generation=identity["generation"],
        token=identity["token"],
    )
    descriptor = (work.prepared_deliveries or {}).get(identity["member_key"])
    retained = descriptor and descriptor["body_sha256"] == db.info.get("provider_work_body_sha256")
    if (attempt.state != "open" or attempt.drain_only or work.state != "running") and not retained:
        raise ProviderWorkError("provider work acceptance is closed")
    members = {row["key"]: row for row in (work.manifest or {}).get("members", [])}
    member = members.get(identity["member_key"])
    canonical_mode = (work.manifest or {}).get("mode") == "canonical"
    if canonical_mode:
        if not canonical_members or not canonical_members <= set(members):
            raise ProviderWorkError("delivery is outside frozen canonical scope")
    elif member is None or (
        member.get("payload_sha256") and member["payload_sha256"] != payload_sha256
    ):
        raise ProviderWorkError("delivery is outside frozen expected manifest")
    db.add(
        ProviderWorkReceipt(
            run_id=run_id,
            work_id=work.work_id,
            attempt_id=attempt.attempt_id,
            member_key=identity["member_key"],
            payload_sha256=payload_sha256,
        )
    )


async def close(db: AsyncSession, **identity) -> None:
    _, attempt, _ = await fence(db, **identity)
    if attempt.state == "open":
        attempt.state = "closed"


async def settle(
    db: AsyncSession,
    *,
    outcome: str,
    proof: dict,
    retry_at: datetime | None = None,
    failure_code: str | None = None,
    **identity,
) -> None:
    work, attempt, owner = await fence(db, **identity)
    if attempt.state not in {"closed", "draining"}:
        raise ProviderWorkError("close acquisition before settling provider work")
    receipts = (
        await db.execute(
            select(ProviderWorkReceipt, IngestionRun)
            .join(IngestionRun)
            .where(ProviderWorkReceipt.work_id == work.work_id)
        )
    ).all()
    pending = set(work.prepared_deliveries or {}) - {receipt.member_key for receipt, _ in receipts}
    if pending and outcome != "retry":
        manual = (
            outcome == "failed"
            and failure_code
            and pending
            <= set(proof.get("manual_pending_prepared", []))
            <= set(work.prepared_deliveries)
        )
        if not manual:
            attempt.state = "draining"
            raise ProviderWorkPreparedDrainingError(
                "original prepared deliveries remain unaccepted"
            )
    if any(run.status not in TERMINAL_RUNS for _, run in receipts):
        attempt.state = "draining"
        raise ProviderWorkDrainingError("accepted runs have not all reached terminal state")
    if (
        work.kind == "rerun"
        and outcome == "completed"
        and any(
            run.status != "completed"
            or run.failed_records
            or run.success_records != run.total_records
            for _, run in receipts
        )
    ):
        outcome, failure_code = "failed", "NORMALIZATION_FAILED"
    eod_v2 = (work.manifest or {}).get("contract") == {
        "schema_id": "market_eod",
        "schema_version": 2,
    }
    if outcome == "partial" or outcome == "completed" and eod_v2:
        from app.schemas.eod_completion import EODCompletionEvidence
        from app.services.eod_completion import validate_eod_completion

        try:
            evidence = EODCompletionEvidence.model_validate(proof.get("eod"))
            await validate_eod_completion(
                db, work, [run for _, run in receipts], evidence, outcome=outcome
            )
        except ValueError as exc:
            raise ProviderWorkError("EOD v2 completion evidence is invalid") from exc
        if (
            work.kind == "historical"
            and db.info.get("historical_completion_validated") != work.work_id
        ):
            raise ProviderWorkError("historical completion requires its domain evidence validator")
        # Preserve the exact validated client proof for acknowledgement replay.
        # Actual writes remain queryable in permanent receipt/member evidence.
    elif outcome == "completed":
        expected = {row["key"] for row in (work.manifest or {}).get("members", [])}
        completed = {
            receipt.member_key
            for receipt, run in receipts
            if run.status == "completed"
            and run.failed_records == 0
            and run.total_records == run.success_records
        }
        if (work.manifest or {}).get("mode") == "canonical":
            from app.models.registry import DeliveryMemberEvidence

            good_runs = [
                run
                for _, run in receipts
                if run.status == "completed"
                and run.failed_records == 0
                and run.total_records == run.success_records
            ]
            groups: dict[tuple, list[IngestionRun]] = {}
            for run in good_runs:
                if run.sequence_count is not None:
                    groups.setdefault((run.snapshot_id, run.daily_update_id), []).append(run)
            complete_runs = {run.run_id for run in good_runs if run.sequence_count is None}
            for group in groups.values():
                counts = {run.sequence_count for run in group}
                sequence_count = group[0].sequence_count
                assert sequence_count is not None
                if len(counts) == 1 and {run.sequence for run in group} == set(
                    range(1, sequence_count + 1)
                ):
                    complete_runs.update(run.run_id for run in group)
            completed = set(
                await db.scalars(
                    select(DeliveryMemberEvidence.member_key).where(
                        DeliveryMemberEvidence.run_id.in_(complete_runs)
                    )
                )
            )
        no_data = set(proof.get("no_data", []))
        if work.kind == "daily" and (work.manifest or {}).get("calendar_skip"):
            completed = expected
            no_data = set()
        if work.kind == "daily" and (work.manifest or {}).get("plan_id"):
            from app.services.full_market import get_plan

            plan = await get_plan(
                db,
                UUID((work.manifest or {})["plan_id"]),
                provider=work.provider,
                allowed_datasets=[work.dataset_key],
            )
            if (
                plan.trade_date != work.trade_date
                or plan.execution_profile != work.execution_profile
                or plan.status != "complete"
            ):
                raise ProviderWorkError("frozen daily plan has not completed its expected scope")
            no_data = expected - completed
        if (
            not expected
            or completed | no_data != expected
            or (
                work.kind != "daily"
                and no_data - set((work.manifest or {}).get("no_data_evidence", {}))
            )
        ):
            raise ProviderWorkError("complete expected scope lacks durable terminal evidence")
        if (
            work.kind == "historical"
            and db.info.get("historical_completion_validated") != work.work_id
        ):
            raise ProviderWorkError(
                "historical completion requires the historical evidence validator"
            )
    if outcome not in {"completed", "partial", "failed", "cancelled", "retry"}:
        raise ProviderWorkError("invalid provider work outcome")
    pending_errors = proof.get("pending_source_errors", [])
    if "pending_source_errors" in proof:
        members = {row["key"] for row in (work.manifest or {}).get("members", [])}
        if (
            outcome != "retry"
            or work.kind != "daily"
            or not (work.manifest or {}).get("plan_id")
            or not isinstance(pending_errors, list)
            or not pending_errors
            or any(not isinstance(key, str) for key in pending_errors)
            or len(pending_errors) != len(set(pending_errors))
            or not set(pending_errors) <= members
        ):
            raise ProviderWorkError("pending Source outcomes differ from frozen daily scope")
    attempt.state, attempt.outcome, attempt.settled_at = "settled", outcome, utc_now()
    attempt.proof, attempt.failure_code = proof, failure_code
    if outcome == "retry":
        if retry_at is None or retry_at.tzinfo is None or retry_at <= utc_now():
            raise ProviderWorkError("retry requires future not_before")
        # Retry budget belongs to frozen scope, not this queue attempt.
        work.state = "queued"
        db.add(
            ProviderWorkAttempt(
                work_id=work.work_id,
                attempt_number=attempt.attempt_number + 1,
                not_before=retry_at,
                drain_only=attempt.drain_only or not await acquisition_open(db, work),
                proof={"source_error_pending": True, "pending_source_errors": pending_errors}
                if pending_errors
                else {},
            )
        )
    else:
        work.state, work.completed_at = outcome, utc_now()
    owner.attempt_id, owner.token, owner.heartbeat_at, owner.expires_at = None, None, None, None
    await db.flush()


async def _projection_acquisition_states(
    db: AsyncSession,
    rows: Sequence[Row[Any]],
    controls: dict[str, str],
    now: datetime,
) -> dict[UUID, bool]:
    """Batch the read-only acquisition checks used for drain classification.

    This mirrors acquisition_open without loading work bodies, scopes or one
    historical request per row. It grants no ownership or acquisition authority.
    """
    from app.models.registry import (
        DatasetRegistry,
        HistoricalBackfillItem,
        HistoricalBackfillRequest,
    )
    from app.services.historical_backfill import expiry_cutoff
    from app.services.hk_provider_cutover import acquisition_allowed

    relevant = [
        row for row in rows if not row.drain_only and (row.has_prepared or row.source_error_pending)
    ]
    item_ids = {}
    for row in relevant:
        if row.kind == "historical":
            value = row.item_id if row.has_item_id else row.legacy_id
            try:
                item_ids[row.work_id] = UUID(value)
            except (ValueError, TypeError, AttributeError):
                pass
    historical_dates = (
        {
            row.item_id: row.trade_date
            for row in (
                await db.execute(
                    select(HistoricalBackfillItem.item_id, HistoricalBackfillItem.trade_date)
                    .join(HistoricalBackfillRequest)
                    .where(
                        HistoricalBackfillItem.item_id.in_(item_ids.values()),
                        HistoricalBackfillItem.status.in_(("pending", "running")),
                        HistoricalBackfillRequest.status.in_(("queued", "running")),
                    )
                )
            ).all()
        }
        if item_ids
        else {}
    )
    hk_dataset = (
        await db.get(DatasetRegistry, "hk_equity_eod")
        if any(row.dataset_key == "hk_equity_eod" and row.kind != "rerun" for row in relevant)
        else None
    )
    result = {}
    for row in relevant:
        allowed = True
        if row.kind != "rerun" and row.dataset_key == "hk_equity_eod":
            allowed = hk_dataset is not None and acquisition_allowed(
                hk_dataset,
                row.provider,
                trade_date=row.trade_date if row.kind == "daily" else None,
                maintenance=row.kind == "maintenance",
            )
        if row.kind == "daily":
            allowed = (
                allowed
                and daily_window(row.provider, now)["open"]
                and controls.get(f"{row.execution_profile}_{row.provider}_v1") == "running"
            )
        elif row.kind == "historical":
            trade_date = historical_dates.get(item_ids.get(row.work_id))
            allowed = (
                allowed
                and trade_date is not None
                and trade_date >= expiry_cutoff(row.provider, now)
            )
        result[row.work_id] = allowed
    return result


async def projection(db: AsyncSession, *, provider: str | None = None) -> list[dict]:
    # A prepared JSON object can contain many MB of immutable delivery bodies.
    # Never select that object (or repeat it for historical retry rounds) for a
    # monitoring page: return only booleans computed beside the durable records.
    prepared = (
        func.jsonb_object_keys(ProviderWork.prepared_deliveries)
        .table_valued("member_key")
        .render_derived(name="prepared")
    )
    accepted = (
        select(ProviderWorkReceipt.run_id)
        .where(
            ProviderWorkReceipt.work_id == ProviderWork.work_id,
            ProviderWorkReceipt.member_key == prepared.c.member_key,
        )
        .correlate(ProviderWork, prepared)
        .exists()
    )
    previous = (
        select(ProviderWorkAttempt.proof["scheduling"])
        .where(
            ProviderWorkAttempt.work_id == ProviderWork.work_id,
            ProviderWorkAttempt.state == "settled",
        )
        .order_by(ProviderWorkAttempt.attempt_number.desc())
        .limit(1)
        .correlate(ProviderWork)
        .scalar_subquery()
    )
    member_failure = ProviderWorkAttempt.proof["scheduling"]["failures"][0]["reason"].as_string()
    last_failure = (
        select(func.coalesce(ProviderWorkAttempt.failure_code, member_failure))
        .where(
            ProviderWorkAttempt.work_id == ProviderWork.work_id,
            or_(ProviderWorkAttempt.failure_code.is_not(None), member_failure.is_not(None)),
        )
        .order_by(ProviderWorkAttempt.attempt_number.desc())
        .limit(1)
        .correlate(ProviderWork)
        .scalar_subquery()
    )
    stmt = (
        select(
            ProviderWork.work_id,
            ProviderWork.provider,
            ProviderWork.dataset_key,
            ProviderWork.kind,
            ProviderWork.stable_key,
            ProviderWork.trade_date,
            ProviderWork.execution_profile,
            ProviderWork.scope["item_id"].label("item_id"),
            ProviderWork.scope["legacy_id"].label("legacy_id"),
            ProviderWork.scope.bool_op("?")("item_id").label("has_item_id"),
            ProviderWorkAttempt.attempt_id,
            ProviderWorkAttempt.state,
            ProviderWorkAttempt.queue_order,
            ProviderWorkAttempt.attempt_number,
            ProviderWorkAttempt.drain_only,
            ProviderWorkAttempt.not_before,
            ProviderWorkAttempt.proof["source_error_pending"].label("source_error_pending"),
            select(literal(1))
            .select_from(prepared)
            .correlate(ProviderWork)
            .exists()
            .label("has_prepared"),
            select(literal(1))
            .select_from(prepared)
            .where(~accepted)
            .correlate(ProviderWork)
            .exists()
            .label("pending_prepared"),
            select(ProviderWorkReceipt.run_id)
            .join(IngestionRun)
            .where(
                ProviderWorkReceipt.work_id == ProviderWork.work_id,
                IngestionRun.status.not_in(TERMINAL_RUNS),
            )
            .correlate(ProviderWork)
            .exists()
            .label("pending_run"),
            previous.label("last_batch"),
            last_failure.label("last_failure_code"),
        )
        .join(ProviderWorkAttempt)
        .where(ProviderWork.environment == environment(), ProviderWorkAttempt.state != "settled")
        .order_by(ProviderWork.provider, ProviderWorkAttempt.queue_order)
    )
    if provider:
        stmt = stmt.where(ProviderWork.provider == provider)
    owners = {
        row.provider: row
        for row in (
            await db.execute(
                select(
                    ProviderWorkOwner.provider,
                    ProviderWorkOwner.attempt_id,
                    ProviderWorkOwner.heartbeat_at,
                    ProviderWorkOwner.expires_at,
                    ProviderWorkOwner.recovery_reason,
                    ProviderWorkOwner.acquisition_not_before,
                ).where(ProviderWorkOwner.environment == environment())
            )
        ).all()
    }
    control_rows = (
        await db.execute(
            select(
                SchedulerControl.scheduler_key,
                SchedulerControl.desired_state,
                SchedulerControl.retired_at,
            )
        )
    ).all()
    controls = {
        row.scheduler_key: row.desired_state for row in control_rows if row.retired_at is None
    }
    rows = []
    positions: dict[str, int] = {}
    now = utc_now()
    pending = (await db.execute(stmt)).all()
    acquisition_states = await _projection_acquisition_states(
        db,
        pending,
        {row.scheduler_key: row.desired_state for row in control_rows},
        now,
    )
    for work in pending:
        drain_only = work.drain_only or (
            bool(work.has_prepared or work.source_error_pending)
            and not acquisition_states[work.work_id]
        )
        owner = owners.get(work.provider)
        active = owner is not None and owner.attempt_id == work.attempt_id
        needs_drain = work.pending_prepared or work.pending_run or bool(work.source_error_pending)
        runtime_window = daily_window(work.provider, now) if work.kind == "daily" else None
        window_closed = bool(runtime_window and not runtime_window["open"] and not needs_drain)
        last_batch = work.last_batch
        suspended = (
            work.kind == "daily"
            and controls.get(f"{work.execution_profile}_{work.provider}_v1") != "running"
            and not needs_drain
        )
        cooling = bool(
            owner and owner.acquisition_not_before and owner.acquisition_not_before > now
        )
        cooldown_blocked = cooling and not needs_drain and work.kind != "rerun"
        drain_only = drain_only or bool(cooling and needs_drain)
        runnable = (
            work.state == "queued"
            and work.not_before <= now
            and not suspended
            and not cooldown_blocked
            and not window_closed
        )
        if runnable:
            positions[work.provider] = positions.get(work.provider, 0) + 1
        rows.append(
            {
                "work_id": str(work.work_id),
                "provider": work.provider,
                "dataset_key": work.dataset_key,
                "kind": work.kind,
                "stable_key": work.stable_key,
                "trade_date": work.trade_date,
                "execution_profile": work.execution_profile,
                "state": work.state,
                "queue_order": work.queue_order,
                "position": positions[work.provider] if runnable else None,
                "active": active,
                "attempt_number": work.attempt_number,
                "drain_only": drain_only,
                "not_before": work.not_before,
                "heartbeat_at": owner.heartbeat_at if active and owner else None,
                "daily_runtime_window": runtime_window,
                "last_batch": last_batch,
                "last_failure_code": work.last_failure_code
                or (
                    last_batch["failures"][0]["reason"]
                    if last_batch and last_batch.get("failures")
                    else None
                ),
                "wait_reason": (
                    "recovery_required"
                    if active
                    and owner
                    and (owner.recovery_reason or not owner.expires_at or owner.expires_at <= now)
                    else "provider_active"
                    if runnable and owner and owner.attempt_id
                    else "acquisition_window_closed"
                    if window_closed and not active
                    else "account_cooldown"
                    if cooldown_blocked and not active
                    else "batch_yield"
                    if last_batch
                    and last_batch.get("reason") == "batch_yield"
                    and not active
                    and runnable
                    else "not_before"
                    if work.not_before > now
                    else "account_cooldown"
                    if cooldown_blocked and not active
                    else "suspended"
                    if suspended and not active
                    else "prepared_drain"
                    if drain_only
                    else None
                ),
            }
        )
    return rows


async def apply_cutover(db: AsyncSession, proof: dict) -> None:
    """Owner-attested host proof plus DB drain; never infer death from TTL."""
    checked = datetime.fromisoformat(proof["checked_at"])
    providers = ("factset", "finlab", "shioaji", "taifex", "twelve_data")
    if (
        checked.tzinfo is None
        or not timedelta(0) <= utc_now() - checked <= timedelta(minutes=5)
        or proof["environment"] != environment()
        or set(proof["host_lock_identities"]) != set(providers)
    ):
        raise ProviderWorkError("cutover host inventory is stale or outside environment scope")
    owners = {provider: await lock_provider(db, provider) for provider in providers}
    if any(owner.attempt_id is not None for owner in owners.values()):
        raise ProviderWorkError("provider work is already active")
    if await db.scalar(
        select(IngestionRun.run_id)
        .where(IngestionRun.source.in_(providers), IngestionRun.status.not_in(TERMINAL_RUNS))
        .limit(1)
    ):
        raise ProviderWorkError("legacy accepted normalization must drain before cutover")
    from app.models.registry import HistoricalBackfillItem, HistoricalBackfillRequest

    if await db.scalar(
        select(HistoricalBackfillItem.item_id)
        .join(HistoricalBackfillRequest)
        .where(
            HistoricalBackfillItem.status == "running",
            HistoricalBackfillRequest.status.in_(("queued", "running")),
        )
        .limit(1)
    ):
        raise ProviderWorkError("legacy historical work must finish or be cancelled before cutover")
    for provider, owner in owners.items():
        inode = proof["host_lock_identities"][provider]
        if not inode.startswith(environment() + ":") or len(inode.split(":")) != 3:
            raise ProviderWorkError("shared host lock identity is invalid")
        owner.host_lock_identity, owner.recovery_reason = inode, None
    # Imported legacy requests that finished during the drain remain immutable;
    # close only their queue projections, never rewrite their receipts/bodies.
    imported = await db.scalars(
        select(ProviderWork).where(
            ProviderWork.environment == environment(),
            ProviderWork.kind == "historical",
            ProviderWork.state == "queued",
        )
    )
    for work in imported:
        item = await db.get(
            HistoricalBackfillItem, UUID(work.scope.get("item_id", work.scope.get("legacy_id")))
        )
        if item and item.status in {
            "completed",
            "partial",
            "failed",
            "cancelled",
            "skipped_calendar",
        }:
            retained_transition = item.status == "cancelled" or (
                item.status == "failed" and item.failure_code == "BACKFILL_EXPIRED"
            )
            if retained_transition and (
                work.prepared_deliveries
                or await db.scalar(
                    select(ProviderWorkReceipt.run_id)
                    .where(ProviderWorkReceipt.work_id == work.work_id)
                    .limit(1)
                )
            ):
                for attempt in await db.scalars(
                    select(ProviderWorkAttempt).where(
                        ProviderWorkAttempt.work_id == work.work_id,
                        ProviderWorkAttempt.state == "queued",
                    )
                ):
                    attempt.drain_only = True
                continue
            work.state = "completed" if item.status == "skipped_calendar" else item.status
            work.completed_at = item.completed_at
            for attempt in await db.scalars(
                select(ProviderWorkAttempt).where(
                    ProviderWorkAttempt.work_id == work.work_id,
                    ProviderWorkAttempt.state == "queued",
                )
            ):
                attempt.state, attempt.outcome, attempt.settled_at = (
                    "settled",
                    work.state,
                    utc_now(),
                )
