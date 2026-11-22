"""A FinLab daily work writes served unlisted symbols without changing what settles it."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWorkAttempt, ProviderWorkReceipt
from app.models.registry import DeliveryMemberEvidence
from app.services import provider_work as queue
from app.services.delivery_plan_summary import refresh_plan_summary
from app.services.ingestion import IngestionService
from tests.test_full_market import DAY, _activated_plan
from tests.test_provider_work_mapping_gap_partial import _owned_closed_work


def _work(plan, provider="finlab", **manifest):
    return SimpleNamespace(
        kind="daily",
        provider=provider,
        dataset_key=plan.dataset_key,
        trade_date=plan.trade_date,
        execution_profile=plan.execution_profile,
        manifest={
            "mode": "canonical",
            "plan_id": str(plan.plan_id),
            "members": [{"key": "0001"}],
            **manifest,
        },
    )


@pytest.mark.asyncio
async def test_a_canonical_daily_plan_manifest_admits_unlisted_symbols(test_session):
    _, _, plan = await _activated_plan(test_session, "tw_equity_eod", count=1)
    assert await queue._unlisted_scope(test_session, _work(plan), {"7799"})
    # Only a plan-bound canonical manifest of a delivering feed admits them.
    assert not await queue._unlisted_scope(test_session, _work(plan, plan_id=None), {"7799"})
    assert not await queue._unlisted_scope(
        test_session, _work(plan, provider="twelve_data"), {"7799"}
    )


@pytest.mark.asyncio
async def test_unlisted_write_evidence_never_breaks_a_completed_daily_settlement(test_session):
    dataset, _, plan = await _activated_plan(test_session, "tw_equity_eod", count=1)
    work, identity = await _owned_closed_work(test_session, _work(plan))
    attempt = await test_session.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work.work_id)
    )
    run = await IngestionService(test_session).create_ingestion_run(
        dataset.dataset_key,
        source="finlab",
        delivery_part_id=plan.parts[0].part_id,
        schema_id="market_eod",
        schema_version=1,
        batch_data_date=DAY,
    )
    run.status, run.total_records, run.success_records, run.failed_records = (
        "completed",
        2,
        2,
        0,
    )
    test_session.add(
        ProviderWorkReceipt(
            run_id=run.run_id,
            work_id=work.work_id,
            attempt_id=attempt.attempt_id,
            member_key="full:0001",
            payload_sha256="a" * 64,
        )
    )
    for key in ("0001", "7799"):
        test_session.add(DeliveryMemberEvidence(run_id=run.run_id, member_key=key))
    await test_session.flush()
    await refresh_plan_summary(test_session, plan.plan_id)

    await queue.settle(test_session, outcome="completed", proof={}, **identity)

    assert work.state == "completed"
