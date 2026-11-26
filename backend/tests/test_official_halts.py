"""FactSet HK members HKEX recorded halted count as no-data, citing HKEX's report (#593)."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.canonical import CalendarYearRevision
from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPlan,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
)
from app.schemas.full_market import DeliveryOutcomeRequest, DeliveryPlanCreateRequest
from app.services.delivery_plan_summary import stored_summary_response
from app.services.full_market import create_plan, plan_complete, reconcile_plan, record_outcome
from app.services.official_halts import official_halts
from tests.test_full_market import DAY, _activated_plan, _baseline, _dataset
from tests.test_full_market_lateness import _block

OFFICIAL = "hk_equity_official_eod"
REPORT = f"https://www.hkex.com.hk/eng/stat/smstat/dayquot/d{DAY:%y%m%d}e.htm"


async def _members(db, plan) -> dict[str, DailyDeliveryMember]:
    rows = await db.scalars(
        select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == plan.plan_id)
    )
    return {member.member_key: member for member in rows}


async def _hkex_plan(db):
    """HKEX's own plan of the same date, beside FactSet's (whose calendar it shares)."""
    from tests.full_market_fixtures import historical_admission

    dataset = await _dataset(db, OFFICIAL)
    body, release = await _baseline(db, OFFICIAL, 3)
    revision = await db.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "HK")
    )
    await historical_admission(db, dataset, release, DAY, revision.id)
    plan = await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=OFFICIAL,
            provider=body.provider,
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=[OFFICIAL],
    )
    return plan


async def _halt(db, plan, key: str) -> None:
    """HKEX records ``key`` halted with the report line, as its adapter does."""
    await record_outcome(
        db,
        plan.plan_id,
        DeliveryOutcomeRequest.model_validate(
            {
                "version": 1,
                "work_item_id": plan.parts[0].work_item_id,
                "member_key": key,
                "outcome": "no_data",
                "reason": "halted",
                "evidence": {
                    "url": REPORT,
                    "sha256": "a" * 64,
                    # After the Hong Kong close of the trade date.
                    "observed_at": datetime.combine(
                        DAY + timedelta(days=1), datetime.min.time(), timezone.utc
                    ).isoformat(),
                    "source_symbol": key,
                    "trade_date": DAY.isoformat(),
                    "source_status": "halted",
                    "source_excerpt": f"{int(key)} SOME HOLDINGS     HKD TRADING SUSPENDED",
                },
            }
        ),
        provider="hkex",
        allowed_datasets=[OFFICIAL],
    )


def _gaps(summary) -> dict[str, tuple[str, str | None]]:
    return {gap["member_key"]: (gap["status"], gap["reason"]) for gap in summary.gaps}


@pytest.mark.asyncio
async def test_a_member_hkex_recorded_halted_completes_the_factset_plan(test_session):
    _, factset, plan = await _activated_plan(test_session, "hk_equity_eod", count=3)
    official = await _hkex_plan(test_session)
    members = await _members(test_session, plan)
    # 00003 has an outcome of its own; HKEX's record never replaces it.
    await _block(test_session, plan, factset.provider, members["00003"], "source_error")
    before = await stored_summary_response(test_session, plan)
    assert (before.missing, before.blocked, before.no_data) == (2, 1, 0)

    for key in ("00001", "00003"):
        await _halt(test_session, official, key)

    # HKEX's outcome reclassifies FactSet's stored counts without waiting for an audit.
    stored = await stored_summary_response(test_session, plan)
    assert (stored.missing, stored.blocked, stored.no_data) == (1, 1, 1)
    full = await reconcile_plan(
        test_session, await test_session.get(DailyDeliveryPlan, plan.plan_id)
    )
    assert (full.missing, full.blocked, full.no_data) == (1, 1, 1)
    assert _gaps(full) == {"00002": ("missing", None), "00003": ("blocked", "source_error")}
    # FactSet's own member keeps its outcome unrecorded.
    test_session.expire_all()
    assert (await _members(test_session, plan))["00001"].outcome is None

    await _halt(test_session, official, "00002")
    stored = await stored_summary_response(test_session, plan)
    # Only the member FactSet itself reported blocked still holds the plan open.
    assert (stored.missing, stored.blocked, stored.no_data) == (0, 1, 2)
    assert not plan_complete(stored)


@pytest.mark.asyncio
async def test_only_a_halt_of_the_same_member_and_date_counts(test_session):
    _, _, plan = await _activated_plan(test_session, "hk_equity_eod", count=2)
    official = await _hkex_plan(test_session)
    await _halt(test_session, official, "00001")
    lookup = {"provider": "factset", "dataset_key": "hk_equity_eod", "keys": {"00001", "00002"}}

    halted = await official_halts(test_session, trade_date=DAY, **lookup)
    assert set(halted) == {"00001"}
    assert halted["00001"]["url"] == REPORT and halted["00001"]["dataset_key"] == OFFICIAL
    assert await official_halts(test_session, trade_date=DAY + timedelta(days=1), **lookup) == {}
    # HKEX itself, or any other feed, cites no exchange records.
    for provider, dataset_key in (("hkex", OFFICIAL), ("twelve_data", "us_equity_eod")):
        assert (
            await official_halts(
                test_session,
                provider=provider,
                dataset_key=dataset_key,
                trade_date=DAY,
                keys={"00001"},
            )
            == {}
        )
    # A recorded halt whose evidence names another security is not this member's.
    member = (await _members(test_session, official))["00001"]
    member.outcome_evidence = {**member.outcome_evidence, "source_symbol": "00002"}
    await test_session.flush()
    assert await official_halts(test_session, trade_date=DAY, **lookup) == {}


async def hkex_history(db, day: date, members: list[dict], status: str = "completed") -> None:
    """A finished HKEX historical date with the given member results."""
    request = HistoricalBackfillRequest(
        request_key=f"hkex-{day.isoformat()}",
        provider="hkex",
        dataset_key=OFFICIAL,
        market="HK",
        start_date=day,
        end_date=day,
        status=status,
        created_by="owner",
    )
    db.add(request)
    await db.flush()
    db.add(
        HistoricalBackfillItem(
            request_id=request.request_id,
            trade_date=day,
            status=status,
            result_evidence={
                "version": 2,
                "schema_id": "exchange_daily_quote",
                "schema_version": 1,
                "trade_date": day.isoformat(),
                "members": members,
            },
        )
    )
    await db.flush()


def report_raw(day: date, dataset: str = OFFICIAL, provider: str = "hkex") -> dict:
    digest = f"{day.toordinal():064x}"
    return {
        "ref": f"r2://{'a' * 32}/raw/{provider}/{dataset}/report/{digest}.htm.gz",
        "sha256": digest,
    }


@pytest.mark.asyncio
async def test_an_hkex_historical_date_proves_the_halt(test_session):
    _, _, plan = await _activated_plan(test_session, "hk_equity_eod", count=3)
    await _dataset(test_session, OFFICIAL)
    await hkex_history(
        test_session,
        DAY,
        [
            {"member_key": "00001", "outcome": "no_data", "raw": report_raw(DAY)},
            # A no-data claim citing another feed's raw is not HKEX's report.
            {
                "member_key": "00002",
                "outcome": "no_data",
                "raw": report_raw(DAY, "hk_equity_eod", "factset"),
            },
            {"member_key": "00003", "outcome": "missing"},
        ],
    )
    summary = await reconcile_plan(
        test_session, await test_session.get(DailyDeliveryPlan, plan.plan_id)
    )
    assert (summary.no_data, summary.missing) == (1, 2)
    assert set(_gaps(summary)) == {"00002", "00003"}
    # An unfinished exchange date proves nothing.
    item = await test_session.scalar(select(HistoricalBackfillItem))
    item.status = "running"
    await test_session.flush()
    summary = await reconcile_plan(
        test_session, await test_session.get(DailyDeliveryPlan, plan.plan_id)
    )
    assert (summary.no_data, summary.missing) == (0, 3)


@pytest.mark.asyncio
async def test_a_factset_backfill_date_completes_on_the_exchange_halt_record(
    client, test_session, test_engine, monkeypatch
):
    from app.models.provider_work import ProviderWorkAttempt
    from app.services import historical_backfill
    from tests.test_backfill_unavailable import LISTED, NEW, _complete, _settled, _setup

    headers = await _setup(test_session, monkeypatch)
    day = date(2026, 10, 15)
    # HKEX's own historical date of the same day printed 00001 as suspended.
    await hkex_history(
        test_session, day, [{"member_key": LISTED, "outcome": "no_data", "raw": report_raw(day)}]
    )
    await historical_backfill.create_request(
        test_session,
        provider="factset",
        dataset_key="hk_equity_eod",
        start_date=day,
        end_date=day,
        request_key="factset-halted",
        created_by="owner",
    )
    await test_session.commit()

    # FactSet wrote no row for either member: one is halted, the other not yet listed.
    item, owner = await _complete(client, headers, {LISTED: "missing", NEW: "missing"})
    status, state, proof = await _settled(test_session, item, owner["work_id"])
    assert status == state == "completed"
    assert proof["halted"] == [LISTED] and proof["unavailable"] == {NEW: "not_yet_trading"}
    assert await test_session.scalar(select(ProviderWorkAttempt.outcome)) == "completed"
