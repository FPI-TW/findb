"""Write-time completeness, atomicity, backfill and summary-only freshness reads."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    SchedulerDataset,
)
from app.schemas.full_market import DeliveryOutcomeRequest
from app.services.delivery_plan_summary import (
    backfill_missing_summaries,
    refresh_plan_summary,
)
from app.services.full_market import record_outcome
from app.services.ingestion import IngestionService
from app.services.market_freshness import list_market_freshness
from app.services.normalize.contracts import (
    FuturesEODContractNormalizer,
    MarketMinuteContractNormalizer,
)
from app.utils import utc_now, uuid7
from tests.test_full_market import DAY, EVIDENCE, _activated_plan, _quote
from tests.test_minute_normalize import _minute_request


async def summary(db, plan_id):
    return await db.scalar(
        select(DailyDeliveryPlanSummary)
        .where(DailyDeliveryPlanSummary.plan_id == plan_id)
        .execution_options(populate_existing=True)
    )


@pytest.mark.asyncio
async def test_outcome_and_actual_write_override_update_summary(test_session):
    dataset, _, plan = await _activated_plan(test_session, count=1)
    stored = await summary(test_session, plan.plan_id)
    assert (stored.expected_count, stored.missing_count, stored.data_count) == (2, 2, 0)
    member = await test_session.scalar(
        select(DailyDeliveryMember)
        .where(DailyDeliveryMember.plan_id == plan.plan_id)
        .order_by(DailyDeliveryMember.member_key)
    )
    blocked = DeliveryOutcomeRequest(
        version=1,
        work_item_id=plan.parts[0].work_item_id,
        member_key=member.member_key,
        outcome="blocked",
        reason="source_error",
    )
    await record_outcome(
        test_session,
        plan.plan_id,
        blocked,
        provider="taifex",
        allowed_datasets=[dataset.dataset_key],
    )
    stored = await summary(test_session, plan.plan_id)
    assert (stored.blocked_count, stored.missing_count) == (1, 1)
    no_data = DeliveryOutcomeRequest(
        version=1,
        work_item_id=plan.parts[0].work_item_id,
        member_key=member.member_key,
        outcome="no_data",
        reason="no_trade",
        evidence={
            **EVIDENCE,
            "observed_at": utc_now(),
            "source_symbol": member.provider_symbol,
            "trade_date": DAY,
            "session": member.session,
            "source_status": "no_trade",
            "source_excerpt": "Official report confirms no trades for this contract.",
        },
    )
    await record_outcome(
        test_session,
        plan.plan_id,
        no_data,
        provider="taifex",
        allowed_datasets=[dataset.dataset_key],
    )
    stored = await summary(test_session, plan.plan_id)
    assert (stored.no_data_count, stored.blocked_count) == (1, 0)
    run = await IngestionService(test_session).create_ingestion_run(
        dataset.dataset_key,
        source="taifex",
        delivery_part_id=plan.parts[0].part_id,
        schema_id="futures_eod",
        schema_version=1,
        batch_data_date=DAY,
    )
    normalizer = FuturesEODContractNormalizer(test_session, dataset.config)
    await normalizer.process({"data": [_quote(session=member.session)]}, run.run_id)
    stored = await summary(test_session, plan.plan_id)
    assert (stored.data_count, stored.no_data_count, stored.missing_count) == (1, 0, 1)
    assert stored.latest_run_id == run.run_id and stored.last_completed_at is not None
    assert member.outcome == "no_data"  # Original durable evidence remains immutable.


@pytest.mark.asyncio
async def test_minute_retry_invalidates_complete_group_until_recovery(test_session):
    dataset, _, plan = await _activated_plan(test_session, "tw_equity_minute", count=1)
    await test_session.execute(
        text(
            "CREATE TABLE summary_minute PARTITION OF market_data_minute FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')"
        )
    )
    payload = _minute_request("tw_equity_minute", symbol="0001", source_symbol="0001")["payload"]
    payload["batch"]["data_date"] = DAY.isoformat()
    payload["batch"]["sequence_count"] = 2
    for key in ("trade_date", "bar_start_time", "bar_end_time", "signal_time"):
        payload["data"][0][key] = payload["data"][0][key].replace("2026-07-30", DAY.isoformat())
    normalizer = MarketMinuteContractNormalizer(
        test_session, {**dataset.config, "_ingest_source": "shioaji"}
    )

    dataset_key = dataset.dataset_key

    async def create(sequence):
        return await IngestionService(test_session).create_ingestion_run(
            dataset_key,
            source="shioaji",
            delivery_part_id=plan.parts[0].part_id,
            schema_id="market_minute",
            schema_version=1,
            batch_data_date=DAY,
            delivery_mode="sequenced_snapshot",
            snapshot_id="summary-snapshot",
            daily_update_id="summary-update",
            sequence=sequence,
            sequence_count=2,
            status="queued",
        )

    for sequence in (1, 2):
        run = await create(sequence)
        await normalizer.process(payload, run.run_id)
        stored = await summary(test_session, plan.plan_id)
        assert stored.data_count == (1 if sequence == 2 else 0)
    retry = await create(1)
    stored = await summary(test_session, plan.plan_id)
    assert stored.data_count == 0 and stored.missing_count == 1
    await normalizer.update_run_status(retry.run_id, "failed", 1, 0, 1)
    await test_session.commit()
    assert (await summary(test_session, plan.plan_id)).data_count == 0
    recovered = await create(1)
    await normalizer.process(payload, recovered.run_id)
    stored = await summary(test_session, plan.plan_id)
    assert stored.data_count == 1 and stored.missing_count == 0


@pytest.mark.asyncio
async def test_rollback_keeps_canonical_evidence_and_summary_atomic(test_session):
    dataset, _, plan = await _activated_plan(test_session, count=1)
    run = await IngestionService(test_session).create_ingestion_run(
        dataset.dataset_key,
        source="taifex",
        schema_id="futures_eod",
        schema_version=1,
        delivery_part_id=plan.parts[0].part_id,
        batch_data_date=DAY,
    )
    await test_session.commit()
    transaction = await test_session.begin_nested()
    await FuturesEODContractNormalizer(test_session, dataset.config).process(
        {"data": [_quote()]},
        run.run_id,
        commit=False,
    )
    assert (await summary(test_session, plan.plan_id)).data_count == 1
    await transaction.rollback()
    assert (await summary(test_session, plan.plan_id)).data_count == 0
    assert await test_session.scalar(text("SELECT count(*) FROM delivery_member_evidence")) == 0
    assert await test_session.scalar(text("SELECT count(*) FROM futures_contract_eod")) == 0


@pytest.mark.asyncio
async def test_backfill_is_bounded_and_pending_get_never_reconciles(test_session, monkeypatch):
    _, _, plan = await _activated_plan(test_session, count=1)
    test_session.add(
        SchedulerDataset(scheduler_key="full_market_taifex_v1", dataset_key=plan.dataset_key)
    )
    await test_session.execute(delete(DailyDeliveryPlanSummary))
    await test_session.commit()
    from app.services import full_market

    async def unexpected(*args, **kwargs):
        raise AssertionError("GET must not reconcile historical plans")

    with monkeypatch.context() as patch:
        patch.setattr(full_market, "reconcile_plan", unexpected)
        rows = await list_market_freshness(
            test_session, profile="full_market", now=datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
        )
        assert "delivery_summary_pending" in rows[0].feeds[0].configuration_error
        assert await summary(test_session, plan.plan_id) is None
    assert await backfill_missing_summaries(test_session, limit=1) == 1
    assert await backfill_missing_summaries(test_session, limit=1) == 0
    assert (await summary(test_session, plan.plan_id)).expected_count == 2


@pytest.mark.asyncio
async def test_rebuild_tool_defaults_to_dry_run_and_applies_only_matching_scope(
    test_session, test_engine, monkeypatch, capsys
):
    from scripts import rebuild_delivery_plan_summaries as tool

    _, _, plan = await _activated_plan(test_session, count=1)
    await test_session.execute(delete(DailyDeliveryPlanSummary))
    await test_session.commit()
    monkeypatch.setattr(tool, "engine", test_engine)
    args = SimpleNamespace(
        dataset=plan.dataset_key,
        provider=plan.provider,
        profile=plan.execution_profile,
        apply=False,
        batch_size=1,
    )
    await tool.rebuild(args)
    assert json.loads(capsys.readouterr().out) == {"apply": False, "matching_plans": 1}
    assert await summary(test_session, plan.plan_id) is None
    args.apply = True
    args.profile = "pilot"
    await tool.rebuild(args)
    assert await summary(test_session, plan.plan_id) is None
    args.profile = "full_market"
    await tool.rebuild(args)
    assert json.loads(capsys.readouterr().out)["rebuilt"] == 1
    assert (await summary(test_session, plan.plan_id)).missing_count == 2


@pytest.mark.asyncio
async def test_summary_only_read_is_bounded_and_read_only_with_history(
    test_session, test_engine, monkeypatch
):
    _, _, plan = await _activated_plan(test_session, count=1)
    test_session.add(
        SchedulerDataset(scheduler_key="full_market_taifex_v1", dataset_key=plan.dataset_key)
    )
    await test_session.commit()
    base = await test_session.get(DailyDeliveryPlan, plan.plan_id)
    original_members = list(
        await test_session.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == plan.plan_id)
        )
    )
    # A history with no complete day previously forced reconciliation of every plan.
    from app.services import full_market

    async def unexpected(*args, **kwargs):
        raise AssertionError("freshness cannot reconcile")

    monkeypatch.setattr(full_market, "reconcile_plan", unexpected)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    queries = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement.lower())

    async def read():
        queries.clear()
        async with factory() as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            result = await list_market_freshness(
                db, profile="full_market", now=datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
            )
        assert not any(
            table in q
            for q in queries
            for table in (
                "daily_delivery_member",
                "market_data_minute",
                "delivery_member_evidence",
                "raw.market_payload",
            )
        )
        return result, len(queries)

    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    try:
        before, before_count = await read()
        for offset in range(1, 87):
            clone = DailyDeliveryPlan(
                plan_id=uuid7(),
                dataset_key=base.dataset_key,
                provider=base.provider,
                trade_date=base.trade_date - timedelta(days=offset),
                release_id=base.release_id,
                execution_profile="full_market",
                activation_cutoff=base.activation_cutoff,
                deadline_at=base.deadline_at - timedelta(days=offset),
                member_sha256=base.member_sha256,
                expected_count=2,
            )
            test_session.add(clone)
            await test_session.flush()
            part = DailyDeliveryPart(
                part_id=uuid7(),
                plan_id=clone.plan_id,
                part_number=1,
                work_item_id="fp1:" + uuid7().hex,
                member_sha256=clone.member_sha256,
                expected_count=2,
            )
            test_session.add(part)
            await test_session.flush()
            for member in original_members:
                test_session.add(
                    DailyDeliveryMember(
                        member_id=uuid7(),
                        plan_id=clone.plan_id,
                        part_id=part.part_id,
                        member_key=member.member_key,
                        symbol=member.symbol,
                        provider_symbol=member.provider_symbol,
                        product_code=member.product_code,
                        contract_code=member.contract_code,
                        session=member.session,
                        mapping_status="mapped",
                    )
                )
            test_session.add(
                DailyDeliveryPlanSummary(
                    plan_id=clone.plan_id,
                    expected_count=2,
                    data_count=0,
                    no_data_count=0,
                    missing_count=2,
                    blocked_count=0,
                    updated_at=utc_now(),
                )
            )
        await test_session.commit()
        after, after_count = await read()
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observe)
    assert before_count == after_count
    assert after[0].feeds[0].total_records == before[0].feeds[0].total_records == 2
    assert after[0].feeds[0].latest_successful_data_date is None


@pytest.mark.asyncio
async def test_competing_outcome_writers_serialize_summaries(test_session, test_engine):
    _, _, plan = await _activated_plan(test_session, count=1)
    members = list(
        await test_session.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.plan_id == plan.plan_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    )
    ids = [m.member_id for m in members]
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    entered = asyncio.Event()
    async with factory() as first:
        row = await first.get(DailyDeliveryMember, ids[0])
        row.outcome = "blocked"
        await refresh_plan_summary(first, plan.plan_id)

        async def writer():
            async with factory() as second:
                row = await second.get(DailyDeliveryMember, ids[1])
                row.outcome = "blocked"
                await second.flush()
                entered.set()
                await refresh_plan_summary(second, plan.plan_id)
                await second.commit()

        task = asyncio.create_task(writer())
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            await first.commit()
            await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        stored = await summary(db, plan.plan_id)
        assert stored.blocked_count == 2 and stored.missing_count == 0


@pytest.mark.asyncio
async def test_profiles_metadata_and_failed_retry_update_only_affected_plan(test_session):
    from app.models.canonical import CalendarYearRevision
    from app.models.registry import UniverseRelease
    from app.schemas.full_market import DeliveryPlanCreateRequest
    from app.services.full_market import create_plan
    from app.services.normalization_queue import _schedule_retry
    from tests.full_market_fixtures import historical_admission

    dataset, _, full = await _activated_plan(test_session, count=1)
    revision = await test_session.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "TAIFEX")
    )
    await historical_admission(
        test_session,
        dataset,
        await test_session.get(UniverseRelease, full.release_id),
        DAY,
        revision.id,
        profile="pilot",
    )
    pilot = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=dataset.dataset_key,
            provider="taifex",
            trade_date=DAY,
            release_id=full.release_id,
            execution_profile="pilot",
        ),
        allowed_datasets=[dataset.dataset_key],
    )
    test_session.add_all(
        [
            SchedulerDataset(scheduler_key=f"{p}_taifex_v1", dataset_key=dataset.dataset_key)
            for p in ("pilot", "full_market")
        ]
    )
    await test_session.commit()
    for member in await test_session.scalars(
        select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == pilot.plan_id)
    ):
        await record_outcome(
            test_session,
            pilot.plan_id,
            DeliveryOutcomeRequest(
                version=1,
                work_item_id=pilot.parts[0].work_item_id,
                member_key=member.member_key,
                outcome="no_data",
                reason="no_trade",
                evidence={
                    **EVIDENCE,
                    "observed_at": utc_now(),
                    "source_symbol": member.provider_symbol,
                    "trade_date": DAY,
                    "session": member.session,
                    "source_status": "no_trade",
                    "source_excerpt": "Official session report confirms no trades.",
                },
            ),
            provider="taifex",
            allowed_datasets=[dataset.dataset_key],
        )
    pilot_updated = (await summary(test_session, pilot.plan_id)).updated_at
    service = IngestionService(test_session)
    run = await service.create_ingestion_run(
        dataset.dataset_key,
        source="taifex",
        schema_id="futures_eod",
        schema_version=1,
        batch_data_date=DAY,
        delivery_part_id=full.parts[0].part_id,
        status="queued",
    )
    job = await service.create_normalization_job(run)
    job.attempt_count = job.max_attempts
    await _schedule_retry(test_session, job.job_id, run.run_id, RuntimeError("test exhaustion"))
    assert (await summary(test_session, full.plan_id)).last_failure_code == "RETRY_EXHAUSTED"
    assert (await summary(test_session, pilot.plan_id)).updated_at == pilot_updated
    now = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    full_feed = (await list_market_freshness(test_session, profile="full_market", now=now))[
        0
    ].feeds[0]
    pilot_feed = (await list_market_freshness(test_session, profile="pilot", now=now))[0].feeds[0]
    assert full_feed.latest_successful_data_date is None
    assert full_feed.last_run_id == run.run_id and full_feed.last_failure_code == "RETRY_EXHAUSTED"
    assert pilot_feed.latest_successful_data_date == DAY and pilot_feed.status == "fresh"
