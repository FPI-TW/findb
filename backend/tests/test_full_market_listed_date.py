"""Members listed after a trade date are not expected on it; listing day is."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.canonical import CalendarYearRevision
from app.models.registry import DailyDeliveryMember, UniverseMember
from app.schemas.full_market import (
    DeliveryPlanCreateRequest,
    UniverseMemberInput,
    UniversePublishRequest,
)
from app.services.full_market import (
    _listing_dates,
    _member_columns,
    create_plan,
    publish_universe,
    submit_universe,
)
from tests.full_market_fixtures import historical_admission
from tests.test_full_market import DAY, _calendar, _dataset, _universe

KEY = "tw_equity_eod"


def _with_listing(body, listed: dict[str, object]):
    members = [
        member.model_copy(update={"listed_date": listed.get(member.symbol)})
        for member in body.members
    ]
    return body.model_copy(update={"members": members})


async def _published(db, body):
    release = await submit_universe(db, body, allowed_datasets=[KEY])
    if release.status != "published":
        await publish_universe(
            db,
            release.release_id,
            UniversePublishRequest(
                evidence_note="Verified official first baseline.", first_baseline_approved=True
            ),
            actor="test-owner",
        )
    await db.commit()
    return release


async def _plan(db, release, day):
    return await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=KEY,
            provider="finlab",
            trade_date=day,
            release_id=release.release_id,
        ),
        allowed_datasets=[KEY],
    )


@pytest.mark.asyncio
async def test_plan_expects_members_from_their_listing_date(test_session):
    tomorrow = DAY + timedelta(days=1)
    dataset = await _dataset(test_session, KEY)
    await _calendar(test_session, dataset.market, [DAY, tomorrow])
    body = _with_listing(_universe(KEY, 3), {"0002": DAY, "0003": tomorrow})
    release = await _published(test_session, body)
    revision = await test_session.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == dataset.market)
    )
    await historical_admission(test_session, dataset, release, DAY, revision.id)

    # One release serves both dates; each plan judges listing by its own trade date.
    before = await _plan(test_session, release, DAY)
    listed = await _plan(test_session, release, tomorrow)

    keys = {
        plan.plan_id: sorted(
            (
                await test_session.scalars(
                    select(DailyDeliveryMember.member_key).where(
                        DailyDeliveryMember.plan_id == plan.plan_id
                    )
                )
            ).all()
        )
        for plan in (before, listed)
    }
    assert before.summary.expected == 2 and listed.summary.expected == 3
    assert [key.split("|")[0] for key in keys[before.plan_id]] == ["0001", "0002"]
    assert len(keys[listed.plan_id]) == 3
    assert listed.summary.expected == release.member_count
    assert before.summary.missing == 2


@pytest.mark.asyncio
async def test_learning_listing_dates_does_not_count_as_membership_change(test_session):
    await _dataset(test_session, KEY)
    first = await _published(test_session, _universe(KEY, 3))
    stored = (
        await test_session.scalars(
            select(UniverseMember).where(UniverseMember.release_id == first.release_id)
        )
    ).all()
    # Members recorded without a listing date hash exactly as before.
    listing = await _listing_dates(test_session, first.release_id)
    assert listing == {}
    assert all("listed_date" not in _member_columns(member, listing) for member in stored)

    dated = _with_listing(
        _universe(KEY, 3, effective_date=DAY + timedelta(days=1)),
        {"0001": DAY - timedelta(days=400), "0002": DAY - timedelta(days=30), "0003": DAY},
    )
    second = await submit_universe(test_session, dated, allowed_datasets=[KEY])
    assert second.status == "published" and second.change_count == 0

    moved = _with_listing(
        _universe(KEY, 3, effective_date=DAY + timedelta(days=2)),
        {
            "0001": DAY - timedelta(days=400),
            "0002": DAY - timedelta(days=30),
            "0003": DAY + timedelta(days=5),
        },
    )
    third = await submit_universe(test_session, moved, allowed_datasets=[KEY])
    assert third.change_count == 1


def test_listing_date_must_be_a_date():
    with pytest.raises(ValidationError):
        UniverseMemberInput(
            symbol="0001",
            provider_symbol="0001",
            exchange="TWSE",
            currency="TWD",
            asset_class="equity",
            listed_date="2026/11/03",
        )
