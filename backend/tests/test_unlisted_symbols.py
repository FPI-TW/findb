"""The frozen list measures gaps; it never withholds rows a whole-market provider served."""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.models.canonical import Instrument
from app.models.registry import DailyDeliveryPlanSummary, DeliveryMemberEvidence
from app.services.full_market import get_plan, plan_summary_response
from app.services.normalize.contracts import MarketEODContractNormalizer
from app.services.unlisted_symbols import listed_elsewhere
from app.utils import utc_now
from tests.test_full_market import DAY, _activated_plan, _baseline, _dataset, _source_headers


def _eod(plan, *symbols, idempotency_key="unlisted-test"):
    return {
        "dataset_key": plan.dataset_key,
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": plan.provider,
        "request_key": idempotency_key,
        "idempotency_key": idempotency_key,
        "fetched_at": utc_now().isoformat(),
        "delivery": {
            "slot_id": "taiwan_market_window",
            "scheduled_for": utc_now().isoformat(),
            "target_data_date": plan.trade_date.isoformat(),
            "work_item_id": plan.parts[0].work_item_id,
        },
        "payload": {
            "batch": {
                "data_date": plan.trade_date.isoformat(),
                "delivery_mode": "incremental",
                "declared_record_count": len(symbols),
            },
            "data": [
                {
                    "symbol": symbol,
                    "source_symbol": symbol,
                    "trade_date": DAY.isoformat(),
                    "currency": "TWD",
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": 5,
                }
                for symbol in symbols
            ],
        },
    }


@pytest.mark.asyncio
async def test_finlab_daily_writes_unlisted_rows_outside_expected(owned_client, test_session):
    dataset, _, plan = await _activated_plan(test_session, "tw_equity_eod", count=1)
    headers = await _source_headers(test_session, "finlab", ["tw_equity_eod"])
    request = _eod(plan, "0001", "7799")
    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    normalizer = MarketEODContractNormalizer(
        test_session,
        {**dataset.config, "_ingest_source": "finlab", "_ingest_fetched_at": request["fetched_at"]},
    )
    assert (await normalizer.process(request["payload"], run_id)).success_records == 2

    # Both writes are evidence; only the member counts toward expected.
    assert set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(DeliveryMemberEvidence.run_id == run_id)
        )
    ) == {"0001", "7799"}
    summary = (
        await get_plan(
            test_session, plan.plan_id, provider="finlab", allowed_datasets=["tw_equity_eod"]
        )
    ).summary
    assert (summary.expected, summary.data, summary.missing) == (1, 1, 0)
    assert (summary.unlisted_data, summary.unlisted) == (1, ["7799"])
    stored = await test_session.scalar(
        select(DailyDeliveryPlanSummary).where(DailyDeliveryPlanSummary.plan_id == plan.plan_id)
    )
    assert stored.unlisted_data_count == 1
    listed = (
        await plan_summary_response(test_session, await _plan_row(test_session, plan))
    ).summary
    assert (listed.unlisted_data, listed.unlisted) == (1, [])
    instrument = await test_session.scalar(select(Instrument).where(Instrument.symbol == "7799"))
    assert (instrument.asset_class, instrument.market) == ("equity", "TW")


async def _plan_row(db, plan):
    from app.models.registry import DailyDeliveryPlan

    return await db.get(DailyDeliveryPlan, plan.plan_id)


@pytest.mark.asyncio
async def test_unlisted_row_named_by_another_asset_class_list_is_refused(
    owned_client, test_session
):
    _, _, plan = await _activated_plan(test_session, "tw_equity_eod", count=1)
    await _dataset(test_session, "tw_etf_eod")
    # The ETF list names 0001 and 0002; 0002 is no equity member.
    await _baseline(test_session, "tw_etf_eod", count=2)
    assert await listed_elsewhere(
        test_session, provider="finlab", dataset_key="tw_equity_eod", symbols={"0002", "7799"}
    ) == {"0002"}
    headers = await _source_headers(test_session, "finlab", ["tw_equity_eod"])
    refused = await owned_client.post(
        "/api/v1/source/ingest", headers=headers, json=_eod(plan, "0002")
    )
    assert refused.status_code == 409 and "outside the declared plan part" in refused.text
    wrong_currency = _eod(plan, "7799", idempotency_key="unlisted-usd")
    wrong_currency["payload"]["data"][0]["currency"] = "USD"
    response = await owned_client.post(
        "/api/v1/source/ingest", headers=headers, json=wrong_currency
    )
    assert response.status_code == 409 and "currency differs" in response.text


@pytest.mark.asyncio
async def test_member_scoped_feed_still_refuses_unlisted_rows(owned_client, test_session):
    _, _, plan = await _activated_plan(test_session, "hk_equity_eod", count=1)
    headers = await _source_headers(test_session, "factset", ["hk_equity_eod"])
    request = _eod(plan, "09999")
    request.update(schema_version=2)
    request["payload"]["data"][0]["currency"] = "HKD"
    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 409 and "outside the declared plan part" in response.text


@pytest.mark.asyncio
async def test_finlab_served_row_of_a_mapping_gap_member_counts_as_data(owned_client, test_session):
    from sqlalchemy import update

    from app.models.registry import DailyDeliveryMember, UniverseMember

    dataset, _, plan = await _activated_plan(test_session, "tw_equity_eod", count=2)
    for model, scope in (
        (DailyDeliveryMember, DailyDeliveryMember.plan_id == plan.plan_id),
        (UniverseMember, UniverseMember.release_id == plan.release_id),
    ):
        await test_session.execute(
            update(model).where(scope, model.member_key == "0002").values(mapping_status="gap")
        )
    await test_session.commit()
    headers = await _source_headers(test_session, "finlab", ["tw_equity_eod"])
    request = _eod(plan, "0002")
    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    normalizer = MarketEODContractNormalizer(
        test_session,
        {**dataset.config, "_ingest_source": "finlab", "_ingest_fetched_at": request["fetched_at"]},
    )
    await normalizer.process(request["payload"], UUID(response.json()["run_id"]))
    summary = (
        await get_plan(
            test_session, plan.plan_id, provider="finlab", allowed_datasets=["tw_equity_eod"]
        )
    ).summary
    assert (summary.data, summary.blocked, summary.mapping_gap) == (1, 0, 0)
    assert summary.unlisted_data == 0


@pytest.mark.asyncio
async def test_member_scoped_feed_still_refuses_mapping_gap_data(owned_client, test_session):
    from sqlalchemy import update

    from app.models.registry import DailyDeliveryMember, UniverseMember

    _, _, plan = await _activated_plan(test_session, "hk_equity_eod", count=2)
    for model, scope in (
        (DailyDeliveryMember, DailyDeliveryMember.plan_id == plan.plan_id),
        (UniverseMember, UniverseMember.release_id == plan.release_id),
    ):
        await test_session.execute(
            update(model).where(scope, model.member_key == "00002").values(mapping_status="gap")
        )
    await test_session.commit()
    headers = await _source_headers(test_session, "factset", ["hk_equity_eod"])
    request = _eod(plan, "00002")
    request.update(schema_version=2)
    request["payload"]["data"][0]["currency"] = "HKD"
    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 409 and "Mapping-gap" in response.text, response.text
