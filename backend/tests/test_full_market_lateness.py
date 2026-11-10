from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.registry import DailyDeliveryMember, DailyDeliveryPlan
from app.schemas.full_market import DeliveryOutcomeRequest
from app.services.full_market import reconcile_plan, record_outcome
from app.utils import ensure_utc
from tests.test_full_market import _activated_plan


async def _block(db, plan, provider: str, member: DailyDeliveryMember, reason: str) -> None:
    await record_outcome(
        db,
        plan.plan_id,
        DeliveryOutcomeRequest(
            version=1,
            work_item_id=plan.parts[0].work_item_id,
            member_key=member.member_key,
            outcome="blocked",
            reason=reason,
        ),
        provider=provider,
        allowed_datasets=[plan.dataset_key],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second", "late"),
    [("mapping_gap", False), ("source_error", True), (None, True)],
)
async def test_mapping_gaps_stay_blocked_without_making_the_plan_late(test_session, second, late):
    _, body, plan = await _activated_plan(test_session, count=2)
    members = (
        await test_session.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.plan_id == plan.plan_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    ).all()
    *unmapped, last = members
    for member in unmapped:
        await _block(test_session, plan, body.provider, member, "mapping_gap")
    if second is not None:
        await _block(test_session, plan, body.provider, last, second)
    plan_row = await test_session.get(DailyDeliveryPlan, plan.plan_id)
    assert plan_row is not None
    after_deadline = ensure_utc(plan_row.deadline_at) + timedelta(seconds=1)

    summary = await reconcile_plan(test_session, plan_row, now=after_deadline)

    assert summary.mapping_gap == len(unmapped) + (second == "mapping_gap")
    assert summary.blocked == len(unmapped) + (second is not None)
    assert summary.missing == (second is None)
    assert summary.is_late is late
    assert {gap["reason"] for gap in summary.gaps} >= {"mapping_gap"}
