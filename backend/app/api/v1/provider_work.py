"""Authenticated provider FIFO operations; leases never travel inside payloads."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import verify_source_api_key
from app.dependencies import get_db
from app.execution_profiles import execution_profile
from app.services import provider_work as service

router = APIRouter(prefix="/provider-work", dependencies=[Depends(verify_source_api_key)])


class EnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stable_key: str = Field(min_length=1, max_length=200)
    kind: Literal["daily", "historical", "maintenance", "rerun"]
    dataset_key: str
    trade_date: date | None = None
    execution_profile: Literal["pilot", "full_market"] | None = None
    scope: dict = Field(default_factory=dict)
    not_before: datetime | None = None


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: Literal["provider-work-v1"]
    host_lock_identity: str = Field(min_length=3, max_length=200)
    recover: bool = False
    requested_work_id: UUID | None = None
    accepted_kinds: list[Literal["daily", "historical", "maintenance", "rerun"]] | None = None
    execution_profile: Literal["pilot", "full_market"] | None = None


class FenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: int = Field(ge=1)
    token: UUID


class HeartbeatRequest(FenceRequest):
    acquisition_not_before: datetime | None = None


class ManifestRequest(FenceRequest):
    manifest: dict


class PrepareRequest(FenceRequest):
    body: str = Field(min_length=2, max_length=16 * 1024 * 1024)
    stage: Literal["intent", "confirm"] = "confirm"
    job_key: str | None = Field(default=None, max_length=300)


class PreparedLookup(FenceRequest):
    job_key: str | None = None
    member_key: str | None = None
    delivery_key: str | None = Field(default=None, max_length=100)
    keys_only: bool = False
    pending_only: bool = False


class SettleRequest(FenceRequest):
    outcome: Literal["completed", "partial", "failed", "cancelled", "retry"]
    proof: dict
    retry_at: datetime | None = None
    failure_code: str | None = None


def scope(db: AsyncSession) -> tuple[str, list[str]]:
    from app.services.source_clients import PROVIDER_DATASET_SCOPE

    provider, datasets = db.info.get("source_name"), db.info.get("allowed_datasets")
    if (
        not isinstance(provider, str)
        or not isinstance(datasets, list)
        or not datasets
        or not set(datasets) <= set(PROVIDER_DATASET_SCOPE.get(provider, ()))
    ):
        raise HTTPException(status_code=403, detail="Source provider work scope denied")
    return provider, datasets


@router.post("/enqueue")
async def enqueue(body: EnqueueRequest, db: AsyncSession = Depends(get_db)) -> dict:
    provider, datasets = scope(db)
    if body.dataset_key not in datasets:
        raise HTTPException(status_code=403, detail="Source dataset scope denied")
    if body.kind == "rerun":
        raise HTTPException(
            status_code=409, detail="retained raw reruns must use the run rerun endpoint"
        )
    if body.kind == "maintenance" and (
        body.trade_date is not None
        or body.execution_profile is not None
        or not body.stable_key.startswith(f"maintenance:{body.dataset_key}:")
    ):
        raise HTTPException(status_code=422, detail="maintenance stable work identity differs")
    if body.kind == "daily" and (
        body.execution_profile is None
        or body.trade_date is None
        or body.stable_key != f"daily:{body.execution_profile}:{body.dataset_key}:{body.trade_date}"
    ):
        raise HTTPException(status_code=422, detail="daily stable work identity differs")
    if body.kind == "historical":
        from sqlalchemy import select

        from app.models.provider_work import ProviderWork

        existing = await db.scalar(
            select(ProviderWork).where(
                ProviderWork.environment == service.environment(),
                ProviderWork.provider == provider,
                ProviderWork.stable_key == body.stable_key,
                ProviderWork.kind == "historical",
            )
        )
        if existing is None:
            raise HTTPException(
                status_code=409, detail="historical work requires an approved backfill request"
            )
    try:
        values = body.model_dump()
        if body.kind == "daily":
            values = await service.daily_readiness(db, provider=provider, values=values)
        work = await service.enqueue(db, provider=provider, **values)
        await db.commit()
        return {"work_id": str(work.work_id), "state": work.state}
    except service.ProviderWorkError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/claim")
async def claim(body: ClaimRequest, db: AsyncSession = Depends(get_db)) -> dict:
    provider, datasets = scope(db)

    try:
        claimed = await service.claim(
            db,
            provider=provider,
            datasets=datasets,
            host_lock_identity=body.host_lock_identity,
            require_cutover=True,
            recover=body.recover,
            requested_work_id=body.requested_work_id,
            accepted_kinds=list(body.accepted_kinds) if body.accepted_kinds is not None else None,
            execution_profile=body.execution_profile,
        )
        await db.commit()
        if claimed is None:
            return {"work": None}
        work, attempt, owner = claimed
        return {
            "work": {
                "work_id": str(work.work_id),
                "stable_key": work.stable_key,
                "kind": work.kind,
                "dataset_key": work.dataset_key,
                "trade_date": work.trade_date,
                "execution_profile": work.execution_profile,
                "scope": work.scope,
                "manifest": work.manifest,
                "created_at": work.created_at,
                "state": attempt.state,
                "attempt_number": attempt.attempt_number,
                "drain_only": attempt.drain_only or attempt.state != "open",
                "scope_stopped": not await service.acquisition_open(db, work),
                "generation": owner.generation,
                "token": str(owner.token),
                "expires_at": owner.expires_at,
            }
        }
    except service.ProviderWorkError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def mutate(db: AsyncSession, work_id: UUID, body: FenceRequest, action: str) -> dict:
    provider, datasets = scope(db)
    identity: service.FenceIdentity = {
        "provider": provider,
        "work_id": work_id,
        "generation": body.generation,
        "token": body.token,
    }
    try:
        if action in {"close", "settle"}:
            from sqlalchemy import select

            from app.models.provider_work import ProviderWork, ProviderWorkAttempt

            await service.lock_provider(db, provider)
            previous = await db.scalar(
                select(ProviderWorkAttempt)
                .join(ProviderWork)
                .where(
                    ProviderWork.work_id == work_id,
                    ProviderWork.provider == provider,
                    ProviderWork.environment == service.environment(),
                    ProviderWork.dataset_key.in_(datasets),
                    ProviderWorkAttempt.generation == body.generation,
                    ProviderWorkAttempt.token == body.token,
                    ProviderWorkAttempt.state == "settled",
                )
            )
            if previous:
                replay_work = await db.get(ProviderWork, work_id)
                assert replay_work is not None
                failed_rerun_completion = (
                    isinstance(body, SettleRequest)
                    and replay_work.kind == "rerun"
                    and body.outcome == "completed"
                    and body.failure_code is None
                    and previous.outcome == "failed"
                    and previous.failure_code == "NORMALIZATION_FAILED"
                )
                if isinstance(body, SettleRequest) and (
                    (body.outcome != previous.outcome and not failed_rerun_completion)
                    or body.proof != previous.proof
                    or (body.failure_code != previous.failure_code and not failed_rerun_completion)
                ):
                    raise service.ProviderWorkError(
                        "settlement replay differs from durable round proof"
                    )
                return {"work_id": str(work_id), "state": replay_work.state}
        work, _, owner = await service.fence(db, **identity)
        authorization_evidence = {}
        if work.dataset_key not in datasets:
            raise HTTPException(status_code=403, detail="Source dataset scope denied")
        if action == "heartbeat":
            await service.heartbeat(
                db,
                acquisition_not_before=body.acquisition_not_before
                if isinstance(body, HeartbeatRequest)
                else None,
                **identity,
            )
        elif action == "authorize":
            await service.fence(db, acquisition=True, **identity)
            if work.kind == "historical":
                from sqlalchemy import select
                from sqlalchemy.orm import selectinload

                from app.models.registry import HistoricalBackfillItem
                from app.services.historical_backfill import _session_closed

                item = await db.scalar(
                    select(HistoricalBackfillItem)
                    .options(selectinload(HistoricalBackfillItem.request))
                    .where(
                        HistoricalBackfillItem.item_id
                        == UUID(work.scope.get("item_id", work.scope.get("legacy_id")))
                    )
                )
                if (
                    item is None
                    or item.request.status not in {"queued", "running"}
                    or item.status not in {"pending", "running"}
                    or not _session_closed(item, service.utc_now())
                ):
                    raise service.ProviderWorkSuspendedError(
                        "historical acquisition is stopped or not yet closed"
                    )
            elif work.kind == "daily":
                from app.services.full_market_admission import account_authorization

                authorization = await account_authorization(
                    db,
                    provider,
                    work.execution_profile or "pilot",
                    "",
                    "",
                    profile=execution_profile(work.execution_profile or "pilot"),
                )
                if not authorization["acquisition_allowed"]:
                    raise service.ProviderWorkSuspendedError("scheduler acquisition is stopped")
            expires_at = owner.expires_at
            assert expires_at is not None
            if work.kind == "daily":
                from app.services.daily_runtime import daily_window

                closes_at = daily_window(provider)["closes_at"]
                if closes_at:
                    expires_at = min(expires_at, datetime.fromisoformat(closes_at))
            authorization_evidence = {
                "provider": provider,
                "generation": owner.generation,
                "token": str(owner.token),
                "checked_at": service.utc_now(),
                "expires_at": expires_at,
            }
        elif action == "calendar-status":
            return await service.current_calendar_status(db, work)
        elif action == "close":
            await service.close(db, **identity)
        elif action == "execute-rerun":
            from sqlalchemy import select

            from app.models.registry import IngestionRun, NormalizationJob
            from app.services.ingestion import IngestionService

            if work.kind != "rerun":
                raise service.ProviderWorkError("provider work is not a retained-raw rerun")
            run = await db.get(IngestionRun, UUID(work.scope["run_id"]))
            assert run is not None
            existing = await db.scalar(
                select(NormalizationJob).where(NormalizationJob.run_id == run.run_id)
            )
            if existing is None:
                await service.fence(db, acquisition=True, **identity)
                await IngestionService(db).create_normalization_job(run)
        elif action == "manifest" and isinstance(body, ManifestRequest):
            await service.freeze_manifest(db, manifest=body.manifest, **identity)
        elif action == "prepare" and isinstance(body, PrepareRequest):
            await service.register_prepared(
                db, body=body.body, stage=body.stage, job_key=body.job_key, **identity
            )
        elif action == "prepared" and isinstance(body, PreparedLookup):
            if body.keys_only:
                keys = set(work.prepared_deliveries or {})
                if body.pending_only:
                    from sqlalchemy import select

                    from app.models.provider_work import ProviderWorkReceipt

                    accepted = await db.scalars(
                        select(ProviderWorkReceipt.member_key).where(
                            ProviderWorkReceipt.work_id == work_id
                        )
                    )
                    keys.difference_update(accepted.all())
                return {"member_keys": sorted(keys)}
            prepared = [
                row["body"]
                for key, row in (work.prepared_deliveries or {}).items()
                if (body.job_key is None and body.member_key is None and body.delivery_key is None)
                or body.delivery_key == key
                or (body.job_key is not None and row.get("job_key") == body.job_key)
                or (body.member_key is not None and body.member_key in row["canonical_members"])
            ]
            return {"bodies": prepared}
        elif action == "settle" and isinstance(body, SettleRequest):
            await service.settle(db, **body.model_dump(), provider=provider, work_id=work_id)
        await db.commit()
        return {"work_id": str(work_id), "state": work.state, **authorization_evidence}
    except service.ProviderWorkDrainingError as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    except service.ProviderWorkSuspendedError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "PROVIDER_WORK_SUSPENDED"}) from exc
    except service.ProviderWorkError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{work_id}/heartbeat")
async def heartbeat(
    work_id: UUID, body: HeartbeatRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    return await mutate(db, work_id, body, "heartbeat")


@router.post("/{work_id}/prepare")
async def prepare(work_id: UUID, body: PrepareRequest, db: AsyncSession = Depends(get_db)) -> dict:
    return await mutate(db, work_id, body, "prepare")


@router.post("/{work_id}/prepared")
async def prepared(work_id: UUID, body: PreparedLookup, db: AsyncSession = Depends(get_db)) -> dict:
    return await mutate(db, work_id, body, "prepared")


@router.post("/{work_id}/close")
async def close(work_id: UUID, body: FenceRequest, db: AsyncSession = Depends(get_db)) -> dict:
    return await mutate(db, work_id, body, "close")


@router.post("/{work_id}/authorize")
async def authorize(work_id: UUID, body: FenceRequest, db: AsyncSession = Depends(get_db)) -> dict:
    return await mutate(db, work_id, body, "authorize")


@router.post("/{work_id}/calendar-status")
async def calendar_status(
    work_id: UUID, body: FenceRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    return await mutate(db, work_id, body, "calendar-status")


@router.post("/{work_id}/execute-rerun")
async def execute_rerun(
    work_id: UUID, body: FenceRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    return await mutate(db, work_id, body, "execute-rerun")


@router.post("/{work_id}/manifest")
async def manifest(
    work_id: UUID, body: ManifestRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    return await mutate(db, work_id, body, "manifest")


@router.post("/{work_id}/settle")
async def settle(work_id: UUID, body: SettleRequest, db: AsyncSession = Depends(get_db)) -> dict:
    return await mutate(db, work_id, body, "settle")


@router.get("")
async def queue(db: AsyncSession = Depends(get_db)) -> dict:
    provider, datasets = scope(db)
    return {
        "data": [
            row
            for row in await service.projection(db, provider=provider)
            if row["dataset_key"] in datasets
        ]
    }
