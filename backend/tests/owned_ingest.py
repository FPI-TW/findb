"""Real FIFO ownership setup for contract/normalizer tests.

These tests exercise payload behavior under an already owned delivery scope.
Protocol rejection and lifecycle tests use the ordinary, unmodified client.
"""

from datetime import date
from uuid import UUID

from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import DatasetRegistry
from app.services import provider_work as queue


async def owned_headers(db, body: dict, headers: dict) -> dict:
    if any(key.lower() == "x-findb-work-id" for key in headers):
        return headers
    try:
        provider = body["source"]
        dataset = body["dataset_key"]
        day = date.fromisoformat(body["payload"]["batch"]["data_date"])
    except (KeyError, TypeError, ValueError):
        return headers
    if provider not in {"finlab", "twelve_data", "shioaji", "taifex", "factset"}:
        return headers
    if await db.get(DatasetRegistry, dataset) is None:
        return headers
    owner = await queue.lock_provider(db, provider)
    inode = f"{queue.environment()}:1:22"
    owner.host_lock_identity = inode
    stable = f"contract-test:{provider}:{dataset}:{day}"
    if owner.attempt_id:
        attempt = await db.get(ProviderWorkAttempt, owner.attempt_id)
        work = await db.get(ProviderWork, attempt.work_id)
        if work.stable_key != stable:
            # A contract test must drain its earlier accepted work before moving
            # to another date/dataset, exactly like a real consumer.
            await queue.close(
                db,
                provider=provider,
                work_id=work.work_id,
                generation=owner.generation,
                token=owner.token,
            )
            await queue.settle(
                db,
                provider=provider,
                work_id=work.work_id,
                generation=owner.generation,
                token=owner.token,
                outcome="completed",
                proof={},
            )
    work = await queue.enqueue(
        db,
        provider=provider,
        stable_key=stable,
        kind="maintenance",
        dataset_key=dataset,
        trade_date=day,
    )
    if owner.attempt_id:
        attempt = await db.get(ProviderWorkAttempt, owner.attempt_id)
    else:
        result = await queue.claim(
            db,
            provider=provider,
            datasets=[dataset],
            host_lock_identity=inode,
            require_cutover=True,
        )
        assert result is not None
        work, attempt, owner = result
    fence = {
        "provider": provider,
        "work_id": work.work_id,
        "generation": owner.generation,
        "token": owner.token,
    }
    if work.manifest is None:
        await queue.freeze_manifest(db, manifest={"members": [{"key": "contract"}]}, **fence)
    await db.commit()
    return {
        **headers,
        "X-FinDB-Work-Id": str(work.work_id),
        "X-FinDB-Work-Generation": str(owner.generation),
        "X-FinDB-Work-Token": str(owner.token),
        "X-FinDB-Work-Member": "contract",
    }


class OwnedIngestClient:
    def __init__(self, client, db):
        self.client, self.db = client, db

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def post(self, url, **kwargs):
        if str(url).endswith("/api/v1/source/ingest") and isinstance(kwargs.get("json"), dict):
            kwargs["headers"] = await owned_headers(
                self.db, kwargs["json"], kwargs.get("headers", {})
            )
        return await self.client.post(url, **kwargs)


def set_owned_identity(db, headers):
    db.info["provider_work"] = {
        "work_id": UUID(headers["X-FinDB-Work-Id"]),
        "generation": int(headers["X-FinDB-Work-Generation"]),
        "token": UUID(headers["X-FinDB-Work-Token"]),
        "member_key": headers["X-FinDB-Work-Member"],
    }


async def finish_contract_scope(db, provider):
    """Drain the opt-in contract fixture before testing another work kind."""
    from app.models.provider_work import ProviderWorkReceipt
    from app.models.registry import IngestionRun

    owner = await queue.lock_provider(db, provider)
    if not owner.attempt_id:
        return
    attempt = await db.get(ProviderWorkAttempt, owner.attempt_id)
    work = await db.get(ProviderWork, attempt.work_id)
    assert work.stable_key.startswith("contract-test:")
    runs = list(
        await db.scalars(
            select(IngestionRun)
            .join(ProviderWorkReceipt)
            .where(ProviderWorkReceipt.work_id == work.work_id)
        )
    )
    assert all(row.status in {"completed", "completed_with_errors", "failed"} for row in runs)
    identity = dict(
        provider=provider, work_id=work.work_id, generation=owner.generation, token=owner.token
    )
    await queue.close(db, **identity)
    failed = not runs or any(
        row.status != "completed" or row.success_records != row.total_records for row in runs
    )
    await queue.settle(
        db,
        **identity,
        outcome="failed" if failed else "completed",
        proof={},
        failure_code="NORMALIZATION_FAILED" if failed else None,
    )
    await db.commit()


async def execute_owned_rerun(db, run_id):
    """A test consumer creates a rerun job only after claiming its FIFO scope."""
    from app.models.registry import IngestionRun
    from app.services.ingestion import IngestionService

    run = await db.get(IngestionRun, run_id)
    await finish_contract_scope(db, run.source)
    result = await queue.claim(
        db,
        provider=run.source,
        datasets=[run.dataset_key],
        host_lock_identity=f"{queue.environment()}:1:22",
        require_cutover=True,
    )
    assert result is not None
    work, _, owner = result
    assert work.kind == "rerun" and work.scope["run_id"] == str(run_id)
    await queue.fence(
        db,
        provider=run.source,
        work_id=work.work_id,
        generation=owner.generation,
        token=owner.token,
        acquisition=True,
    )
    await IngestionService(db).create_normalization_job(run)
    await db.commit()
