"""Catalogue symbols outside the list are requested and written, never expected (#629)."""

from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.registry import DailyDeliveryPlan, UniverseSupplement
from app.schemas.full_market import DeliveryPlanCreateRequest, UniverseMemberInput
from app.services.full_market import (
    FullMarketError,
    create_plan,
    get_plan,
    submit_universe,
)
from app.services.normalize.contracts import MarketEODContractNormalizer
from tests.test_full_market import DAY, _activated_plan, _source_headers, _universe
from tests.test_unlisted_symbols import _eod


def _late(symbol="LATE", provider_symbol=None):
    return UniverseMemberInput(
        symbol=symbol,
        provider_symbol=provider_symbol or symbol,
        exchange="XNAS",
        currency="USD",
        asset_class="equity",
        classification="ordinary",
    )


async def _with_supplement(db, *symbols):
    body = _universe("us_equity_eod", 2)
    body.supplementary = [_late(symbol) for symbol in symbols]
    await submit_universe(db, body, allowed_datasets=["us_equity_eod"])
    return body


@pytest.mark.asyncio
async def test_a_refresh_records_its_supplement_once_without_a_new_release(test_session):
    await _activated_plan(test_session, "us_equity_eod", count=2)
    await _with_supplement(test_session, "LATE", "NEWCO")
    # The same list again with the same supplement adds nothing; a list without
    # one keeps the recorded supplement.
    await _with_supplement(test_session, "LATE", "NEWCO")
    await submit_universe(
        test_session, _universe("us_equity_eod", 2), allowed_datasets=["us_equity_eod"]
    )
    rows = list(await test_session.scalars(select(UniverseSupplement)))
    assert len(rows) == 1 and rows[0].member_count == 2
    assert [row["symbol"] for row in rows[0].members] == ["LATE", "NEWCO"]


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["listed", "gap", "duplicate"])
async def test_a_supplement_must_stay_off_the_list_and_mapped(test_session, problem):
    await _activated_plan(test_session, "us_equity_eod", count=2)
    body = _universe("us_equity_eod", 2)
    late = _late("0001" if problem == "listed" else "LATE")
    if problem == "gap":
        late.mapping_status = "gap"
    body.supplementary = [late, late] if problem == "duplicate" else [late]
    with pytest.raises(FullMarketError) as raised:
        await submit_universe(test_session, body, allowed_datasets=["us_equity_eod"])
    assert raised.value.status_code == 422
    assert await test_session.scalar(select(func.count()).select_from(UniverseSupplement)) == 0


async def _plan_with_supplement(db, *symbols):
    """``_activated_plan`` for us_equity_eod, with a supplement recorded before the plan."""
    from app.models.canonical import CalendarYearRevision
    from tests.full_market_fixtures import historical_admission
    from tests.test_full_market import _baseline, _calendar, _dataset

    dataset = await _dataset(db, "us_equity_eod")
    await _calendar(db, dataset.market)
    _, release = await _baseline(db, "us_equity_eod", 1)
    revision = await db.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == dataset.market)
    )
    await historical_admission(db, dataset, release, DAY, revision.id)
    body = _universe("us_equity_eod", 1)
    body.supplementary = [_late(symbol) for symbol in symbols]
    await submit_universe(db, body, allowed_datasets=["us_equity_eod"])
    plan = await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key="us_equity_eod",
            provider="twelve_data",
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=["us_equity_eod"],
    )
    return dataset, plan


@pytest.mark.asyncio
async def test_a_plan_freezes_the_supplement_and_writes_its_served_rows_as_unlisted(
    owned_client, test_session
):
    dataset, plan = await _plan_with_supplement(test_session, "LATE")
    assert [row["symbol"] for row in plan.supplementary] == ["LATE"]
    assert (await test_session.get(DailyDeliveryPlan, plan.plan_id)).supplement_id is not None
    headers = await _source_headers(test_session, "twelve_data", ["us_equity_eod"])
    wrong = _eod(plan, "LATE", idempotency_key="supplement-wrong")
    wrong["payload"]["data"][0].update(currency="USD", source_symbol="OTHER")
    wrong["delivery"]["slot_id"] = "western_markets_window"
    refused = await owned_client.post("/api/v1/source/ingest", headers=headers, json=wrong)
    assert refused.status_code == 409 and "frozen supplement" in refused.text
    request = _eod(plan, "LATE", idempotency_key="supplement-late")
    request["payload"]["data"][0]["currency"] = "USD"
    request["delivery"]["slot_id"] = "western_markets_window"
    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    normalizer = MarketEODContractNormalizer(
        test_session,
        {
            **dataset.config,
            "_ingest_source": "twelve_data",
            "_ingest_fetched_at": request["fetched_at"],
        },
    )
    await normalizer.process(request["payload"], UUID(response.json()["run_id"]))
    summary = (
        await get_plan(
            test_session, plan.plan_id, provider="twelve_data", allowed_datasets=["us_equity_eod"]
        )
    ).summary
    assert (summary.expected, summary.unlisted_data, summary.unlisted) == (1, 1, ["LATE"])
    # A symbol in neither the list nor the supplement stays refused.
    stray = _eod(plan, "STRAY", idempotency_key="supplement-stray")
    stray["payload"]["data"][0]["currency"] = "USD"
    stray["delivery"]["slot_id"] = "western_markets_window"
    assert (
        await owned_client.post("/api/v1/source/ingest", headers=headers, json=stray)
    ).status_code == 409


@pytest.mark.asyncio
async def test_a_daily_manifest_carries_exactly_the_plans_supplement(test_session, monkeypatch):
    from app.services import provider_work as queue
    from tests.test_provider_work import attest_cutover

    _, plan = await _plan_with_supplement(test_session, "LATE")

    async def open_window(*args, **kwargs):
        return True

    monkeypatch.setattr(queue, "acquisition_open", open_window)
    await queue.enqueue(
        test_session,
        provider="twelve_data",
        stable_key=f"daily:full_market:us_equity_eod:{DAY}",
        kind="daily",
        dataset_key="us_equity_eod",
        trade_date=DAY,
        execution_profile="full_market",
        scope={"feed": {"dataset_key": "us_equity_eod"}},
    )
    await attest_cutover(test_session)
    work, _, owner = await queue.claim(
        test_session,
        provider="twelve_data",
        datasets=["us_equity_eod"],
        host_lock_identity=f"{queue.environment()}:1:22",
    )
    fence = {
        "provider": "twelve_data",
        "work_id": work.work_id,
        "generation": owner.generation,
        "token": owner.token,
    }
    manifest = {
        "mode": "canonical",
        "plan_id": str(plan.plan_id),
        "members": [{"key": key} for part in plan.parts for key in part.member_keys],
    }
    with pytest.raises(queue.ProviderWorkError, match="frozen supplement"):
        await queue.freeze_manifest(
            test_session,
            manifest={**manifest, "supplementary": [{"key": "OTHER", "source_symbol": "OTHER"}]},
            **fence,
        )
    frozen = {**manifest, "supplementary": [{"key": "LATE", "source_symbol": "LATE"}]}
    await queue.freeze_manifest(test_session, manifest=frozen, **fence)
    assert work.manifest == frozen
    # Its symbols may be prepared although no list names them.
    assert await queue._unlisted_scope(test_session, work, {"LATE"})
    assert not await queue._unlisted_scope(test_session, work, {"STRAY"})


@pytest.mark.asyncio
async def test_a_repeated_supplement_is_the_newest_and_an_empty_one_is_kept(test_session):
    from datetime import timedelta

    from app.services.universe_supplement import latest_supplement

    await _activated_plan(test_session, "us_equity_eod", count=2)
    for index, symbols in enumerate((("A1",), ("B1",), ("A1",), ())):
        body = _universe("us_equity_eod", 2)
        body.observed_at = body.observed_at + timedelta(minutes=index)
        body.supplementary = [_late(symbol) for symbol in symbols]
        await submit_universe(test_session, body, allowed_datasets=["us_equity_eod"])
        latest = await latest_supplement(
            test_session, dataset_key="us_equity_eod", provider="twelve_data", day=DAY
        )
        # The same day's list A -> B -> A ends on A again; an empty one is recorded.
        assert [row["symbol"] for row in latest.members] == list(symbols)


@pytest.mark.asyncio
async def test_a_supplement_symbol_that_is_another_parts_member_stays_refused(
    owned_client, test_session
):
    from app.models.registry import DailyDeliveryMember, DailyDeliveryPart
    from app.utils import uuid7

    _, plan = await _plan_with_supplement(test_session, "LATE")
    # A stale supplement names a symbol another part of the plan holds as a member.
    other = DailyDeliveryPart(
        part_id=uuid7(),
        plan_id=plan.plan_id,
        part_number=2,
        work_item_id="fp1:" + uuid7().hex,
        member_sha256="a" * 64,
        expected_count=1,
    )
    test_session.add(other)
    await test_session.flush()
    test_session.add(
        DailyDeliveryMember(
            member_id=uuid7(),
            plan_id=plan.plan_id,
            part_id=other.part_id,
            member_key="LATE",
            symbol="LATE",
            provider_symbol="LATE",
            mapping_status="mapped",
        )
    )
    await test_session.commit()
    headers = await _source_headers(test_session, "twelve_data", ["us_equity_eod"])
    request = _eod(plan, "LATE", idempotency_key="supplement-other-part")
    request["payload"]["data"][0]["currency"] = "USD"
    request["delivery"]["slot_id"] = "western_markets_window"
    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 409 and "outside the declared plan part" in response.text
