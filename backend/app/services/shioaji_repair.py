"""Audited overlays and recovery grants, serialized with provider acceptance."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkReceipt
from app.models.registry import DailyDeliveryMember, DailyDeliveryPlan, UniverseMember
from app.models.shioaji_repair import ShioajiMemberRepair, ShioajiRepairAck
from app.schemas.shioaji_repair import RepairApplyRequest, RepairPreviewRequest
from app.services import provider_work as queue
from app.services.full_market import FullMarketError, _digest
from app.utils import utc_now


async def effective_mappings(db: AsyncSession, plan_id) -> dict[str, str]:
    rows = await db.scalars(
        select(ShioajiMemberRepair)
        .where(
            ShioajiMemberRepair.plan_id == plan_id,
            ShioajiMemberRepair.environment == queue.environment(),
            ShioajiMemberRepair.provider_symbol.is_not(None),
        )
        .order_by(ShioajiMemberRepair.created_at, ShioajiMemberRepair.repair_id)
    )
    return {row.member_key: row.provider_symbol for row in rows if row.provider_symbol}


async def work_scope(db: AsyncSession, work_id) -> tuple[ProviderWork, DailyDeliveryPlan]:
    work = await db.get(ProviderWork, work_id, populate_existing=True)
    if (
        work is None
        or work.environment != queue.environment()
        or work.provider != "shioaji"
        or work.kind != "daily"
        or not (work.manifest or {}).get("plan_id")
    ):
        raise FullMarketError("Repair requires an existing Shioaji daily frozen work", 409)
    from uuid import UUID

    plan = await db.get(DailyDeliveryPlan, UUID((work.manifest or {})["plan_id"]))
    if (
        plan is None
        or plan.provider != work.provider
        or plan.dataset_key != work.dataset_key
        or plan.execution_profile != work.execution_profile
        or plan.trade_date != work.trade_date
    ):
        raise FullMarketError("Repair work differs from frozen plan", 409)
    return work, plan


async def inventory(db: AsyncSession, work_id) -> dict:
    from app.services.full_market import reconcile_plan

    work, plan = await work_scope(db, work_id)
    members = list(
        await db.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.plan_id == plan.plan_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    )
    universe = {
        row.member_key: row
        for row in await db.scalars(
            select(UniverseMember).where(UniverseMember.release_id == plan.release_id)
        )
    }
    summary = await reconcile_plan(db, plan, gap_limit=None)
    gaps = {row["member_key"]: row for row in summary.gaps}
    prepared = work.prepared_deliveries or {}
    bound = {member for row in prepared.values() for member in row.get("canonical_members", [])}
    accepted = set(
        await db.scalars(
            select(ProviderWorkReceipt.member_key).where(ProviderWorkReceipt.work_id == work_id)
        )
    )
    # A legacy receipt whose canonical membership cannot be proven freezes all
    # members, rather than guessing from a delivery idempotency key.
    ambiguous = bool(accepted - prepared.keys()) or any(
        not row.get("canonical_members") for row in prepared.values()
    )
    mappings = await effective_mappings(db, plan.plan_id)
    repairs = list(
        await db.scalars(
            select(ShioajiMemberRepair)
            .where(ShioajiMemberRepair.work_id == work_id)
            .order_by(ShioajiMemberRepair.created_at, ShioajiMemberRepair.repair_id)
        )
    )
    result = {
        "work_id": str(work_id),
        "environment": work.environment,
        "execution_profile": work.execution_profile,
        "plan_id": str(plan.plan_id),
        "release_id": str(plan.release_id),
        "dataset_key": work.dataset_key,
        "trade_date": plan.trade_date.isoformat(),
        "state": work.state,
        "manifest_sha256": _digest(work.manifest),
        "prepared_sha256": _digest(prepared),
        "repair_ids": [str(row.repair_id) for row in repairs],
        "approved_request_ids": sorted({row.request_id for row in repairs}),
        "members": [
            {
                "member_key": row.member_key,
                "symbol": row.symbol,
                "provider_symbol": mappings.get(row.member_key, row.provider_symbol),
                "original_provider_symbol": row.provider_symbol,
                "mapping_status": "mapped" if row.member_key in mappings else row.mapping_status,
                "original_mapping_status": row.mapping_status,
                "exchange": universe[row.member_key].exchange,
                "currency": universe[row.member_key].currency,
                "status": gaps[row.member_key]["status"] if row.member_key in gaps else "resolved",
                "reason": gaps[row.member_key]["reason"] if row.member_key in gaps else None,
                "has_prepared": ambiguous or row.member_key in bound,
            }
            for row in members
        ],
    }
    return {**result, "sha256": _digest(result)}


async def preview(db: AsyncSession, work_id, body: RepairPreviewRequest) -> dict:
    current = await inventory(db, work_id)
    rows = {row["member_key"]: row for row in current["members"]}
    if current["state"] in {"completed", "cancelled"}:
        raise FullMarketError("Completed/cancelled work cannot be reopened", 409)
    for change in body.members:
        member = rows.get(change.member_key)
        if not member or member["has_prepared"] or member["status"] == "resolved":
            raise FullMarketError("Repair member is outside unresolved, unprepared scope", 409)
        if change.mapping:
            mapping = change.mapping
            if (
                member["original_mapping_status"] != "gap"
                or member["mapping_status"] != "gap"
                or (mapping.symbol, mapping.exchange, mapping.currency)
                != (member["symbol"], member["exchange"], member["currency"])
            ):
                raise FullMarketError("Mapping evidence differs from original gap identity", 422)
        elif member["mapping_status"] == "gap":
            raise FullMarketError("Mapping gap requires catalogue evidence", 422)
    proposal = body.model_dump(mode="json")
    return {
        "inventory": current,
        "proposal": proposal,
        "sha256": _digest({"inventory": current["sha256"], "proposal": proposal}),
    }


async def apply(db: AsyncSession, work_id, body: RepairApplyRequest, *, actor: str) -> dict:
    owner = await queue.lock_provider(db, "shioaji")
    await work_scope(db, work_id)
    proposal = RepairPreviewRequest.model_validate(body.model_dump(exclude={"expected_sha256"}))
    request_hash = _digest(body.model_dump(mode="json"))
    previous = list(
        await db.scalars(
            select(ShioajiMemberRepair).where(
                ShioajiMemberRepair.work_id == work_id,
                ShioajiMemberRepair.request_id == body.request_id,
            )
        )
    )
    if previous:
        if len(previous) != len(body.members) or any(
            row.request_sha256 != request_hash for row in previous
        ):
            raise FullMarketError("Repair request ID is already bound to different bytes", 409)
        return {"repair_ids": sorted(str(row.repair_id) for row in previous), "replayed": True}
    if owner.attempt_id:
        active = await db.get(ProviderWorkAttempt, owner.attempt_id)
        if active and active.work_id == work_id:
            raise FullMarketError("Wait for the original work round to settle before approval", 409)
    report = await preview(db, work_id, proposal)
    if report["sha256"] != body.expected_sha256:
        raise FullMarketError("Repair preview is stale", 409)
    work, plan = await work_scope(db, work_id)
    rows = {row["member_key"]: row for row in report["inventory"]["members"]}
    ids = []
    for change in body.members:
        row = ShioajiMemberRepair(
            work_id=work_id,
            plan_id=plan.plan_id,
            environment=work.environment,
            execution_profile=work.execution_profile,
            request_id=body.request_id,
            request_sha256=request_hash,
            member_key=change.member_key,
            original_sha256=_digest(rows[change.member_key]),
            provider_symbol=change.mapping.provider_symbol if change.mapping else None,
            observation_limit=change.observation_limit,
            evidence={
                "observation": change.evidence.model_dump(mode="json"),
                "mapping": change.mapping.model_dump(mode="json") if change.mapping else None,
            },
            reason=change.reason,
            actor=actor,
        )
        db.add(row)
        await db.flush()
        ids.append(str(row.repair_id))
    if work.state in {"failed", "partial"}:
        last = await db.scalar(
            select(func.max(ProviderWorkAttempt.attempt_number)).where(
                ProviderWorkAttempt.work_id == work_id
            )
        )
        db.add(
            ProviderWorkAttempt(
                work_id=work_id, attempt_number=(last or 0) + 1, not_before=utc_now()
            )
        )
        work.state, work.completed_at = "queued", None
    return {"repair_ids": sorted(ids), "replayed": False}


async def approved(
    db: AsyncSession, work: ProviderWork, *, ack_ids: list | None = None, generation: int = 0
) -> dict:
    _, plan = await work_scope(db, work.work_id)
    rows = list(
        await db.scalars(
            select(ShioajiMemberRepair)
            .where(ShioajiMemberRepair.work_id == work.work_id)
            .order_by(ShioajiMemberRepair.created_at, ShioajiMemberRepair.repair_id)
        )
    )
    ids = {str(row.repair_id) for row in rows}
    if ack_ids is not None:
        if not set(ack_ids) <= ids:
            raise FullMarketError("Repair ACK differs from original work", 409)
        for row in rows:
            if (
                str(row.repair_id) in ack_ids
                and await db.get(ShioajiRepairAck, row.repair_id) is None
            ):
                db.add(ShioajiRepairAck(repair_id=row.repair_id, generation=generation))
    mappings = await effective_mappings(db, plan.plan_id)
    frozen = {
        row.member_key: row
        for row in await db.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == plan.plan_id)
        )
    }
    return {
        "work_id": str(work.work_id),
        "plan_id": str(plan.plan_id),
        "repairs": [
            {
                "repair_id": str(row.repair_id),
                "member_key": row.member_key,
                "provider_symbol": mappings.get(
                    row.member_key, frozen[row.member_key].provider_symbol
                ),
                "mapping_repaired": row.member_key in mappings,
                "observation_limit": row.observation_limit,
                "original_sha256": row.original_sha256,
            }
            for row in rows
        ],
    }
