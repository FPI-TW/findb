"""A retired provider's closed work IDs feed the Fetcher local-data purge (#574)."""

import pytest

from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import DatasetRegistry, SchedulerControl
from app.services import provider_work as queue
from app.services.retired_provider import (
    RetiredProviderError,
    retired_provider_works,
    work_ids_digest,
)
from app.utils import utc_now, uuid7


async def _retired(db, *, retired=True, work_state="completed", attempt_state="settled"):
    if await db.get(DatasetRegistry, "tw_equity_minute") is None:
        db.add(
            DatasetRegistry(
                dataset_key="tw_equity_minute",
                name="tw_equity_minute",
                market="TW",
                asset_class="equity",
            )
        )
    db.add(
        SchedulerControl(
            scheduler_key="full_market_shioaji_v1",
            provider="shioaji",
            slot_id="taiwan_market_window",
            timezone="Asia/Taipei",
            retired_at=utc_now() if retired else None,
        )
    )
    await db.flush()
    works = []
    for index in range(2):
        work = ProviderWork(
            work_id=uuid7(),
            environment=queue.environment(),
            provider="shioaji",
            stable_key=f"maintenance:tw_equity_minute:{index}",
            kind="maintenance",
            dataset_key="tw_equity_minute",
            state=work_state if index else "completed",
        )
        db.add(work)
        await db.flush()
        db.add(
            ProviderWorkAttempt(work_id=work.work_id, state=attempt_state if index else "settled")
        )
        works.append(work)
    # Another environment's work never enters this environment's purge.
    db.add(
        ProviderWork(
            work_id=uuid7(),
            environment="elsewhere",
            provider="shioaji",
            stable_key="maintenance:tw_equity_minute:other",
            kind="maintenance",
            dataset_key="tw_equity_minute",
            state="queued",
        )
    )
    await db.flush()
    return works


@pytest.mark.asyncio
async def test_lists_every_closed_work_with_a_digest_of_the_sorted_ids(test_session):
    works = await _retired(test_session)

    result = await retired_provider_works(test_session, "shioaji")

    expected = sorted(str(work.work_id) for work in works)
    assert result["work_ids"] == expected
    assert result["provider"] == "shioaji"
    assert result["environment"] == queue.environment()
    assert result["sha256"] == work_ids_digest(list(reversed(expected)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup,reason",
    [
        ({"retired": False}, "control_not_retired"),
        ({"work_state": "queued"}, "work_not_terminal"),
        ({"attempt_state": "open"}, "attempt_not_settled"),
    ],
)
async def test_refuses_while_anything_could_still_run(test_session, setup, reason):
    await _retired(test_session, **setup)

    with pytest.raises(RetiredProviderError) as refused:
        await retired_provider_works(test_session, "shioaji")
    assert reason in refused.value.reasons


@pytest.mark.asyncio
async def test_refuses_while_the_owner_holds_an_attempt(test_session):
    works = await _retired(test_session)
    attempt = ProviderWorkAttempt(work_id=works[0].work_id, attempt_number=2, state="settled")
    test_session.add(attempt)
    await test_session.flush()
    test_session.add(
        ProviderWorkOwner(
            environment=queue.environment(), provider="shioaji", attempt_id=attempt.attempt_id
        )
    )
    await test_session.flush()

    with pytest.raises(RetiredProviderError) as refused:
        await retired_provider_works(test_session, "shioaji")
    assert refused.value.reasons == ["owner_holds_attempt"]


@pytest.mark.asyncio
async def test_refuses_a_provider_still_in_the_catalog(test_session):
    with pytest.raises(RetiredProviderError) as refused:
        await retired_provider_works(test_session, "finlab")
    assert "provider_in_catalog" in refused.value.reasons
