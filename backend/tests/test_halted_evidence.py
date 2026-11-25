"""Nasdaq's trade-halt list completes a US member that used every no-data retry (#593)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.registry import DailyDeliveryMember, DailyDeliveryPlan
from app.schemas.full_market import DeliveryOutcomeRequest
from app.services.full_market import reconcile_plan, record_outcome
from tests.test_full_market import DAY, _activated_plan


@pytest.mark.asyncio
async def test_nasdaq_halt_evidence_settles_a_us_member_as_no_data(test_session):
    """The Fetcher's halts-feed evidence passes Source's no-data checks unchanged."""
    _, body, plan = await _activated_plan(test_session, "us_equity_eod", count=1)
    member = (
        await test_session.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == plan.plan_id)
        )
    ).first()
    request = DeliveryOutcomeRequest.model_validate(
        {
            "version": 1,
            "work_item_id": plan.parts[0].work_item_id,
            "member_key": member.member_key,
            "outcome": "no_data",
            "reason": "halted",
            "evidence": {
                "url": "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts",
                "sha256": "a" * 64,
                # After the US close of the trade date.
                "observed_at": datetime.combine(
                    DAY + timedelta(days=1), datetime.min.time(), timezone.utc
                ).isoformat(),
                "source_symbol": member.provider_symbol,
                "trade_date": DAY.isoformat(),
                "source_status": "halted",
                "source_excerpt": '{"HaltDate":"02/22/2019","ReasonCode":"T12"}',
            },
        }
    )

    summary = await record_outcome(
        test_session,
        plan.plan_id,
        request,
        provider=body.provider,
        allowed_datasets=[plan.dataset_key],
    )

    assert summary.no_data == 1
    full = await reconcile_plan(
        test_session, await test_session.get(DailyDeliveryPlan, plan.plan_id)
    )
    assert full.no_data == 1 and full.gaps == []
