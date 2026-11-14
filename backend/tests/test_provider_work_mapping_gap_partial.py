"""A per-member daily work ends partial when mapping gaps are all its plan lacks."""

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import DailyDeliveryMember
from app.services import provider_work as queue
from app.services.full_market import get_plan
from app.utils import utc_now, uuid7
from tests.test_full_market import _activated_plan
from tests.test_full_market_lateness import _block


async def _gapped_plan(db, last: str | None = "mapping_gap", others: str | None = "mapping_gap"):
    """A plan whose last member is blocked for ``last`` and the rest for ``others``.

    ``None`` leaves a member missing. Returns the work and its mapping-gap member keys.
    """
    _, body, plan = await _activated_plan(db, count=2)
    members = (
        await db.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.plan_id == plan.plan_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    ).all()
    reasons = {member.member_key: others for member in members[:-1]}
    reasons[members[-1].member_key] = last
    for member in members:
        if reasons[member.member_key] is not None:
            await _block(db, plan, body.provider, member, reasons[member.member_key])
    keys = [key for key, reason in reasons.items() if reason == "mapping_gap"]
    work = SimpleNamespace(
        kind="daily",
        provider=body.provider,
        dataset_key=plan.dataset_key,
        trade_date=plan.trade_date,
        execution_profile=plan.execution_profile,
        manifest={
            "mode": "canonical",
            "plan_id": str(plan.plan_id),
            "members": [{"key": member.member_key} for member in members],
        },
    )
    return work, keys


@pytest.mark.asyncio
async def test_mapping_gaps_as_the_only_unresolved_members_are_accepted(test_session):
    work, keys = await _gapped_plan(test_session)

    assert len(keys) > 1
    await queue.validate_mapping_gap_partial(test_session, work, {"mapping_gaps": keys})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("last", "others"),
    [
        (None, "mapping_gap"),  # one member is still missing
        ("source_error", "mapping_gap"),  # one member is blocked for its own reason
        (None, None),  # nothing is a mapping gap
    ],
)
async def test_any_other_unresolved_member_keeps_the_work_from_settling_partial(
    test_session, last, others
):
    work, keys = await _gapped_plan(test_session, last, others)

    with pytest.raises(queue.ProviderWorkError) as raised:
        await queue.validate_mapping_gap_partial(test_session, work, {"mapping_gaps": keys})
    assert raised.value.code == "PROVIDER_WORK_INVALID_PROOF"


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["absent", "short", "extra", "duplicate", "text", "other_date"])
async def test_the_proof_must_name_exactly_the_plans_mapping_gaps(test_session, claim):
    work, keys = await _gapped_plan(test_session)
    proof = {
        "absent": {},
        "short": {"mapping_gaps": keys[:1]},
        "extra": {"mapping_gaps": [*keys, "UNKNOWN"]},
        "duplicate": {"mapping_gaps": [*keys, keys[0]]},
        "text": {"mapping_gaps": ",".join(keys)},
        "other_date": {"mapping_gaps": keys},
    }[claim]
    if claim == "other_date":
        work.trade_date += timedelta(days=1)

    with pytest.raises(queue.ProviderWorkError) as raised:
        await queue.validate_mapping_gap_partial(test_session, work, proof)
    assert raised.value.code == "PROVIDER_WORK_INVALID_PROOF"


@pytest.mark.asyncio
async def test_the_fetcher_decision_reads_the_same_plan_response(test_session, monkeypatch):
    """Fetcher names the gaps from Source's plan response; Source accepts exactly that."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "fetcher/src"))
    from findb_fetcher.full_market_runtime import only_mapping_gaps

    work, keys = await _gapped_plan(test_session)
    response = await get_plan(
        test_session,
        work.manifest["plan_id"],
        provider=work.provider,
        allowed_datasets=[work.dataset_key],
    )

    gaps = only_mapping_gaps(response.model_dump(mode="json"))

    assert gaps == sorted(keys)
    await queue.validate_mapping_gap_partial(test_session, work, {"mapping_gaps": gaps})


async def _owned_closed_work(db, source: SimpleNamespace) -> tuple[ProviderWork, dict]:
    work = ProviderWork(
        environment=queue.environment(),
        provider=source.provider,
        stable_key=f"daily:full_market:{source.dataset_key}:{source.trade_date}",
        kind="daily",
        dataset_key=source.dataset_key,
        trade_date=source.trade_date,
        execution_profile=source.execution_profile,
        state="running",
        manifest=source.manifest,
    )
    db.add(work)
    await db.flush()
    token = uuid7()
    attempt = ProviderWorkAttempt(work_id=work.work_id, state="closed", generation=1, token=token)
    db.add(attempt)
    await db.flush()
    owner = await queue.lock_provider(db, source.provider)
    owner.attempt_id, owner.generation, owner.token = attempt.attempt_id, 1, token
    owner.heartbeat_at, owner.expires_at = utc_now(), utc_now() + timedelta(minutes=5)
    await db.flush()
    return work, {
        "provider": source.provider,
        "work_id": work.work_id,
        "generation": 1,
        "token": token,
    }


@pytest.mark.asyncio
async def test_settle_records_partial_for_a_plan_lacking_only_mapping_gaps(test_session):
    source, keys = await _gapped_plan(test_session)
    work, identity = await _owned_closed_work(test_session, source)

    await queue.settle(test_session, outcome="partial", proof={"mapping_gaps": keys}, **identity)

    assert work.state == "partial" and work.completed_at is not None
    attempt = await test_session.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work.work_id)
    )
    assert (attempt.state, attempt.outcome, attempt.failure_code) == ("settled", "partial", None)
    assert attempt.proof == {"mapping_gaps": keys}


@pytest.mark.asyncio
async def test_settle_rejects_partial_while_another_member_is_unresolved(test_session):
    source, keys = await _gapped_plan(test_session, "source_error")
    work, identity = await _owned_closed_work(test_session, source)

    with pytest.raises(queue.ProviderWorkError) as raised:
        await queue.settle(
            test_session, outcome="partial", proof={"mapping_gaps": keys}, **identity
        )

    assert raised.value.code == "PROVIDER_WORK_INVALID_PROOF"
    assert work.state == "running"
