"""Expand-only FIFO migration preserves raw historical leases and creation order."""

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory
from tests.test_migration_upgrade import _run_alembic


async def _upgrade(url, revision):
    try:
        await _run_alembic(url, revision)
    except subprocess.CalledProcessError as exc:
        pytest.fail(exc.stderr)


@pytest.mark.asyncio
async def test_fifo_import_preserves_historical_scope_tokens_and_order():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "provider_work_import"
    ) as (url, engine):
        await _upgrade(url, "a42c89d3e610")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry(dataset_key,name,asset_class,market,frequency,is_active,config,created_at,updated_at) VALUES('tw_equity_eod','TW','equity','TW','daily',true,'{}',now(),now()) ON CONFLICT DO NOTHING"
                )
            )
            for index in (0, 1):
                ids = {"request": uuid7(), "item": uuid7(), "token": uuid7(), "index": str(index)}
                await db.execute(
                    text(
                        "INSERT INTO historical_backfill_request(request_id,request_key,provider,dataset_key,market,start_date,end_date,created_by,status,created_at,updated_at) VALUES(:request,'import-'||CAST(:index AS text),'finlab','tw_equity_eod','TW','2026-10-15','2026-10-15','owner','running',now()+CAST(:index AS integer)*interval '1 second',now())"
                    ),
                    ids,
                )
                await db.execute(
                    text(
                        'INSERT INTO historical_backfill_item(item_id,request_id,trade_date,status,lease_attempt,lease_token,lease_expires_at,calendar_snapshot) VALUES(:item,:request,\'2026-10-15\',\'running\',2,:token,now()+interval \'1 hour\',\'{"market":"TW","trade_date":"2026-10-15","timezone":"Asia/Taipei","regular_close":"13:30:00"}\')'
                    ),
                    ids,
                )
            before = (
                (
                    await db.execute(
                        text("SELECT to_jsonb(i) FROM historical_backfill_item i ORDER BY item_id")
                    )
                )
                .scalars()
                .all()
            )
        await _upgrade(url, "head")
        async with engine.begin() as db:
            assert (
                await db.execute(
                    text("SELECT to_jsonb(i) FROM historical_backfill_item i ORDER BY item_id")
                )
            ).scalars().all() == before
            rows = (
                await db.execute(
                    text(
                        "SELECT w.scope->>'legacy_id',a.attempt_number FROM provider_work w JOIN provider_work_attempt a USING(work_id) ORDER BY a.queue_order"
                    )
                )
            ).all()
            assert rows == [(before[0]["item_id"], 1), (before[1]["item_id"], 1)]
            assert (
                await db.scalar(
                    text("SELECT recovery_reason FROM provider_work_owner WHERE provider='finlab'")
                )
                == "legacy_consumers_require_verified_drain"
            )


@pytest.mark.asyncio
async def test_imported_daily_prepared_receipt_replays_under_original_work_without_rewrite(
    monkeypatch,
):
    import json
    from copy import deepcopy

    from sqlalchemy import select

    from app.config import get_settings
    from app.models.provider_work import ProviderWorkReceipt
    from app.models.raw import RawMarketPayload
    from app.models.registry import (
        DailyDeliveryMember,
        DatasetRegistry,
        IngestionRun,
        SchedulerControl,
    )
    from app.services import provider_work as queue
    from app.services.canonical_ingestion import accept_canonical_ingest
    from app.services.normalization_queue import execute_normalization
    from app.utils import utc_now
    from scripts.seed_data import DATASETS
    from tests import test_full_market
    from tests.test_full_market import DAY, _activated_plan, _ingress, _quote

    async def existing_dataset(db, key="tw_futures_eod"):
        dataset = await db.get(DatasetRegistry, key)
        dataset.config = deepcopy(
            next(item["config"] for item in DATASETS if item["dataset_key"] == key)
        )
        dataset.is_active = True
        await db.commit()
        return dataset

    monkeypatch.setattr(test_full_market, "_dataset", existing_dataset)

    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "provider_work_receipt_import"
    ) as (url, engine):
        await _upgrade(url, "a42c89d3e610")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            _, _, plan = await _activated_plan(db)
            body = json.dumps(
                _ingress(plan, [_quote()]), sort_keys=True, separators=(",", ":")
            ).encode()
            source_info = dict(db.info)
            accepted = await accept_canonical_ingest(db, body)
            run = await db.get(IngestionRun, accepted.run_id)
            raw = await db.get(RawMarketPayload, run.raw_payload_id)
            original_payload, original_hash = raw.payload, raw.payload_sha256
            work_id, run_id = plan.plan_id, run.run_id
        await _upgrade(url, "head")
        async with sessions() as db:
            from app.models.registry import NormalizationJob

            job = await db.scalar(select(NormalizationJob).where(NormalizationJob.run_id == run_id))
            delivery = job.delivery_id
        await execute_normalization(run_id, delivery, database_url=url)
        async with sessions() as db:
            imported = await queue.enqueue(
                db,
                provider="taifex",
                stable_key=f"daily:full_market:tw_futures_eod:{DAY}",
                kind="daily",
                dataset_key="tw_futures_eod",
                trade_date=DAY,
                execution_profile="full_market",
                scope={"feed": {"dataset_key": "tw_futures_eod"}},
            )
            assert imported.work_id == work_id and imported.scope == {"legacy_id": str(work_id)}
            proof = {
                "environment": get_settings().APP_ENVIRONMENT,
                "checked_at": utc_now().isoformat(),
                "host_lock_identities": {
                    provider: f"{get_settings().APP_ENVIRONMENT}:1:{index}"
                    for index, provider in enumerate(
                        ("factset", "finlab", "shioaji", "taifex", "twelve_data"), 1
                    )
                },
            }
            await queue.apply_cutover(db, proof)
            claimed = await queue.claim(
                db,
                provider="taifex",
                datasets=["tw_futures_eod"],
                host_lock_identity=proof["host_lock_identities"]["taifex"],
                recover=True,
            )
            assert claimed and claimed[0].work_id == work_id
            _, _, owner = claimed
            fence = {
                "provider": "taifex",
                "work_id": work_id,
                "generation": owner.generation,
                "token": owner.token,
            }
            assert imported.prepared_deliveries == {}
            members = list(
                await db.scalars(
                    select(DailyDeliveryMember.member_key).where(
                        DailyDeliveryMember.plan_id == work_id
                    )
                )
            )
            await queue.freeze_manifest(
                db,
                **fence,
                manifest={"plan_id": str(work_id), "members": [{"key": key} for key in members]},
            )
            control = await db.get(SchedulerControl, "full_market_taifex_v1")
            control.desired_state = "stopped"
            await db.flush()
            monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
            # The pre-migration receipt authorizes only the original payload;
            # its local prepared envelope is retained byte for byte after stop.
            await queue.register_prepared(
                db, **fence, body=body.decode(), stage="intent", job_key="legacy-prepared"
            )
            await db.commit()
            await queue.register_prepared(
                db, **fence, body=body.decode(), stage="confirm", job_key="legacy-prepared"
            )
            assert imported.prepared_deliveries["full-market-test"]["body"].encode() == body
            assert imported.prepared_deliveries["full-market-test"]["confirmed"]
            db.info.update(source_info)
            db.info["provider_work"] = {
                "work_id": work_id,
                "generation": owner.generation,
                "token": owner.token,
                "member_key": "full-market-test",
            }
            replay = await accept_canonical_ingest(db, body)
            assert replay.is_duplicate and replay.run_id == run_id
            raw = await db.get(RawMarketPayload, run.raw_payload_id)
            assert raw.payload == original_payload and raw.payload_sha256 == original_hash
            assert (await db.get(ProviderWorkReceipt, run_id)).work_id == work_id


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset_key", ["tw_equity_minute", "tw_etf_minute"])
async def test_full_upgrade_preserves_partial_minute_group_and_continues_original_checkpoint(
    dataset_key, monkeypatch, tmp_path
):
    import json
    from copy import deepcopy
    from datetime import timedelta
    from pathlib import Path

    from sqlalchemy import select

    from app.config import get_settings
    from app.models.provider_work import ProviderWorkReceipt
    from app.models.raw import RawMarketPayload
    from app.models.registry import DatasetRegistry, IngestionRun, NormalizationJob
    from app.schemas.ingress import minute_sequence_key_digest
    from app.services import provider_work as queue
    from app.services.canonical_ingestion import accept_canonical_ingest
    from app.services.full_market import get_plan
    from app.services.normalization_queue import execute_normalization
    from scripts.seed_data import DATASETS
    from tests import test_full_market
    from tests.test_full_market import DAY, _activated_plan
    from tests.test_full_market_lifecycle import _fetcher_member_minute_request
    from tests.test_provider_work import attest_cutover, identity

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "fetcher/src"))
    from findb_fetcher.full_market_state import FullMarketState

    async def existing_dataset(db, key):
        dataset = await db.get(DatasetRegistry, key)
        dataset.config = deepcopy(
            next(item["config"] for item in DATASETS if item["dataset_key"] == key)
        )
        dataset.is_active = True
        await db.commit()
        return dataset

    monkeypatch.setattr(test_full_market, "_dataset", existing_dataset)
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "minute_fifo_import"
    ) as (url, engine):
        await _upgrade(url, "a42c89d3e610")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        bodies, accepted_ids, raw_before = [], [], {}
        async with sessions() as db:
            _, _, plan = await _activated_plan(db, dataset_key, count=1)
            plan_id = plan.plan_id
            base = _fetcher_member_minute_request(plan, "0001", sequence_count=3)
            for sequence in (1, 2, 3):
                body = deepcopy(base)
                body["dataset_key"] = dataset_key
                batch = body["payload"]["batch"]
                batch["sequence"] = sequence
                row = body["payload"]["data"][0]
                from datetime import datetime

                for field in ("bar_start_time", "bar_end_time", "signal_time"):
                    row[field] = (
                        datetime.fromisoformat(row[field]) + timedelta(minutes=sequence - 1)
                    ).isoformat()
                digest = minute_sequence_key_digest(
                    dataset_key=dataset_key,
                    data_date=DAY,
                    snapshot_id=batch["snapshot_id"],
                    sequence=sequence,
                )
                body["request_key"], body["idempotency_key"] = f"mmr:{digest}", f"mms:{digest}"
                bodies.append(
                    json.dumps(
                        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode()
                )
            source_info = dict(db.info)
            for body in bodies[:2]:
                accepted = await accept_canonical_ingest(db, body)
                accepted_ids.append(accepted.run_id)
                run = await db.get(IngestionRun, accepted.run_id)
                raw = await db.get(RawMarketPayload, run.raw_payload_id)
                raw_before[accepted.run_id] = (
                    raw.raw_payload_id,
                    raw.payload_sha256,
                    deepcopy(raw.payload),
                )
        for run_id in accepted_ids:
            async with sessions() as db:
                job = await db.scalar(
                    select(NormalizationJob).where(NormalizationJob.run_id == run_id)
                )
                delivery_id = job.delivery_id
            await execute_normalization(run_id, delivery_id, database_url=url)
        async with sessions() as db:
            result = await get_plan(db, plan_id, provider="shioaji", allowed_datasets=[dataset_key])
            assert result.status == "incomplete" and (
                result.summary.expected,
                result.summary.data,
                result.summary.missing,
            ) == (1, 0, 1)
            frozen_plan = result.model_dump(mode="json")
            immutable_plan = (
                await db.execute(
                    text("SELECT to_jsonb(p) FROM daily_delivery_plan p WHERE plan_id=:id"),
                    {"id": plan_id},
                )
            ).scalar_one()
        checkpoint = FullMarketState(tmp_path / "full-market.sqlite3")
        checkpoint.bind_profile("full_market")
        checkpoint.record_plan(frozen_plan, "shioaji")
        key = frozen_plan["parts"][0]["work_item_id"] + ":0001"
        checkpoint.prepare(
            key,
            json.dumps(
                [json.loads(body) for body in bodies],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        )
        for body, run_id in zip(bodies, accepted_ids):
            checkpoint.save_sequence_receipt(key, json.loads(body)["idempotency_key"], str(run_id))
        checkpoint_before = checkpoint.prepared(key)
        await _upgrade(url, "head")
        async with sessions() as db:
            imported = await queue.enqueue(
                db,
                provider="shioaji",
                stable_key=f"daily:full_market:{dataset_key}:{DAY}",
                kind="daily",
                dataset_key=dataset_key,
                trade_date=DAY,
                execution_profile="full_market",
                scope={"feed": {"dataset_key": dataset_key}},
            )
            assert imported.work_id == plan_id and imported.scope == {"legacy_id": str(plan_id)}
            assert set(
                await db.scalars(
                    select(ProviderWorkReceipt.run_id).where(ProviderWorkReceipt.work_id == plan_id)
                )
            ) == set(accepted_ids)
            assert (
                await db.execute(
                    text("SELECT to_jsonb(p) FROM daily_delivery_plan p WHERE plan_id=:id"),
                    {"id": plan_id},
                )
            ).scalar_one() == immutable_plan
            assert checkpoint.prepared(key) == checkpoint_before
            await attest_cutover(db)
            monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
            claimed = await queue.claim(
                db,
                provider="shioaji",
                datasets=[dataset_key],
                host_lock_identity="local:1:22",
                recover=True,
            )
            fence = identity(claimed)
            await queue.freeze_manifest(
                db,
                **fence,
                manifest={
                    "mode": "canonical",
                    "plan_id": str(plan_id),
                    "members": [{"key": "0001"}],
                },
            )
            db.info.update(source_info)
            for index, body in enumerate(bodies):
                request = json.loads(body)
                await queue.register_prepared(
                    db,
                    **fence,
                    body=body.decode(),
                    stage="intent",
                    job_key=f"legacy-sequence:{index + 1}",
                )
                await queue.register_prepared(
                    db,
                    **fence,
                    body=body.decode(),
                    stage="confirm",
                    job_key=f"legacy-sequence:{index + 1}",
                )
                db.info["provider_work"] = {**fence, "member_key": request["idempotency_key"]}
                accepted = await accept_canonical_ingest(db, body)
                if index < 2:
                    assert accepted.is_duplicate and accepted.run_id == accepted_ids[index]
                else:
                    final_id = accepted.run_id
        async with sessions() as db:
            job = await db.scalar(
                select(NormalizationJob).where(NormalizationJob.run_id == final_id)
            )
            delivery_id = job.delivery_id
        await execute_normalization(final_id, delivery_id, database_url=url)
        async with sessions() as db:
            result = await get_plan(db, plan_id, provider="shioaji", allowed_datasets=[dataset_key])
            assert result.status == "complete" and result.summary.data == 1
            await queue.close(db, **fence)
            await queue.settle(db, **fence, outcome="completed", proof={})
            await db.commit()
            for run_id, original in raw_before.items():
                run = await db.get(IngestionRun, run_id)
                raw = await db.get(RawMarketPayload, run.raw_payload_id)
                assert (raw.raw_payload_id, raw.payload_sha256, raw.payload) == original
                assert run.sequence_count == 3 and run.status == "completed"
            assert checkpoint.prepared(key) == checkpoint_before
            assert {
                checkpoint.sequence_receipt(key, json.loads(body)["idempotency_key"])
                for body in bodies[:2]
            } == {str(run_id) for run_id in accepted_ids}


@pytest.mark.asyncio
async def test_account_cooldown_expand_upgrade_preserves_installed_owner_and_reopens():
    from datetime import timedelta

    from app.utils import utc_now

    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "provider_account_cooldown"
    ) as (url, engine):
        await _upgrade(url, "b53da90e7f21")
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO provider_work_owner(environment,provider,generation,host_lock_identity) VALUES ('staging','twelve_data',19,'shared:1:23')"
                )
            )
        await _upgrade(url, "head")
        floor = utc_now() + timedelta(seconds=120)
        async with engine.begin() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT generation,host_lock_identity,acquisition_not_before FROM provider_work_owner WHERE provider='twelve_data'"
                    )
                )
            ).one()
            assert tuple(row) == (19, "shared:1:23", None)
            await db.execute(
                text(
                    "UPDATE provider_work_owner SET acquisition_not_before=:floor WHERE provider='twelve_data'"
                ),
                {"floor": floor},
            )
        await engine.dispose()
        async with engine.connect() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT generation,host_lock_identity,acquisition_not_before FROM provider_work_owner WHERE provider='twelve_data'"
                    )
                )
            ).one()
            assert tuple(row) == (19, "shared:1:23", floor)
