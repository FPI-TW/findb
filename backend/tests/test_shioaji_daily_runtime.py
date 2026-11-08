"""Daily windows cannot block independent work or discard original FIFO scope."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWorkAttempt
from app.models.registry import SchedulerControl
from app.services import provider_work as queue
from app.services.daily_runtime import daily_window
from tests.test_full_market import _dataset
from tests.test_provider_work import identity


@pytest.mark.parametrize(
    ("hour", "minute", "opened"),
    [(17, 59, False), (18, 0, True), (23, 0, True), (0, 0, True), (5, 59, True), (6, 0, False)],
)
def test_authoritative_window_matches_exported_fetcher_policy(hour, minute, opened):
    import json
    from pathlib import Path

    from findb_fetcher.daily_runtime import window

    clock = datetime(2026, 10, 28, hour, minute, tzinfo=ZoneInfo("Asia/Taipei"))
    catalog = json.loads(
        (Path(__file__).parents[1] / "configs/execution_feeds.v1.json").read_text()
    )
    result = daily_window("shioaji", clock)
    assert result == window(catalog["daily_runtime_policies"]["shioaji"], clock)
    assert result["open"] is opened
    assert daily_window("finlab", clock)["open"] is True


async def setup_queue(db, monkeypatch):
    clock = [datetime(2026, 10, 28, 20, tzinfo=ZoneInfo("Asia/Taipei"))]
    monkeypatch.setattr(queue, "utc_now", lambda: clock[0])
    # The same clock must drive the live daily fence, not only queue selection.
    from app.services import daily_runtime

    monkeypatch.setattr(daily_runtime, "utc_now", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", True)
    keys = ["tw_equity_minute", "tw_etf_minute"]
    for key in keys:
        await _dataset(db, key)
    db.add(
        SchedulerControl(
            scheduler_key="full_market_shioaji_v1",
            provider="shioaji",
            execution_profile="full_market",
            timezone="Asia/Taipei",
            slot_id="taiwan_market_window",
            desired_state="running",
            observed_state="running",
        )
    )
    await db.flush()
    works = [
        await queue.enqueue(
            db,
            provider="shioaji",
            dataset_key=key,
            stable_key=f"daily:full_market:{key}:2026-10-27",
            kind="daily",
            execution_profile="full_market",
            trade_date=clock[0].date() - timedelta(days=1),
            scope={"frozen": key},
        )
        for key in keys
    ]
    await db.commit()
    return clock, keys, works


@pytest.mark.asyncio
async def test_slice_tail_retry_gives_etf_next_turn_and_preserves_frozen_work(
    test_session, monkeypatch
):
    clock, keys, works = await setup_queue(test_session, monkeypatch)
    claimed = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert claimed[0].work_id == works[0].work_id
    await queue.close(test_session, **identity(claimed))
    await queue.settle(
        test_session,
        **identity(claimed),
        outcome="retry",
        proof={
            "scheduling": {
                "processed": 25,
                "reason": "batch_yield",
                "elapsed_seconds": 12.5,
                "counts": {"pending": 75, "manual": 1},
                "failures": [{"member_key": "bad", "attempts": 8, "reason": "retry_exhausted"}],
            }
        },
        retry_at=clock[0] + timedelta(seconds=1),
    )
    clock[0] += timedelta(seconds=2)
    next_claim = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert next_claim[0].work_id == works[1].work_id
    rows = await queue.projection(test_session, provider="shioaji")
    stock = next(row for row in rows if row["dataset_key"] == keys[0])
    assert stock["last_batch"]["processed"] == 25
    assert stock["last_failure_code"] == "retry_exhausted"
    assert stock["attempt_number"] == 2
    assert works[0].scope == {"frozen": keys[0]}
    rounds = list(
        await test_session.scalars(
            select(ProviderWorkAttempt)
            .where(ProviderWorkAttempt.work_id == works[0].work_id)
            .order_by(ProviderWorkAttempt.attempt_number)
        )
    )
    assert rounds[0].failure_code is None
    assert rounds[1].queue_order > next_claim[1].queue_order
    # A later successful slice does not erase the most recent actual failure.
    await queue.close(test_session, **identity(next_claim))
    await queue.settle(
        test_session,
        **identity(next_claim),
        outcome="retry",
        proof={},
        retry_at=clock[0] + timedelta(hours=1),
    )
    stock_claim = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    await queue.close(test_session, **identity(stock_claim))
    await queue.settle(
        test_session,
        **identity(stock_claim),
        outcome="retry",
        proof={"scheduling": {"processed": 25, "reason": "batch_yield", "failures": []}},
        retry_at=clock[0] + timedelta(seconds=1),
    )
    stock = next(
        row
        for row in await queue.projection(test_session, provider="shioaji")
        if row["dataset_key"] == keys[0]
    )
    assert stock["last_batch"]["failures"] == []
    assert stock["last_failure_code"] == "retry_exhausted"


@pytest.mark.asyncio
async def test_live_close_blocks_daily_fence_but_maintenance_can_run_and_daily_resumes(
    test_session, monkeypatch
):
    clock, keys, works = await setup_queue(test_session, monkeypatch)
    clock[0] = datetime(2026, 10, 29, 5, 59, 59, tzinfo=ZoneInfo("Asia/Taipei"))
    claimed = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    clock[0] = datetime(2026, 10, 29, 6, tzinfo=ZoneInfo("Asia/Taipei"))
    # Keep the owner heartbeat live across the simulated boundary.
    await queue.heartbeat(test_session, **identity(claimed))
    with pytest.raises(queue.ProviderWorkSuspendedError):
        await queue.fence(test_session, acquisition=True, **identity(claimed))
    await queue.close(test_session, **identity(claimed))
    await queue.settle(
        test_session,
        **identity(claimed),
        outcome="retry",
        proof={"scheduling": {"reason": "acquisition_window_closed"}},
        retry_at=clock[0] + timedelta(seconds=1),
    )
    maintenance = await queue.enqueue(
        test_session,
        provider="shioaji",
        dataset_key=keys[0],
        stable_key="maintenance:window",
        kind="maintenance",
        trade_date=None,
        scope={},
    )
    await test_session.flush()
    maintenance_claim = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert maintenance_claim[0].work_id == maintenance.work_id
    await queue.fence(test_session, acquisition=True, **identity(maintenance_claim))
    await queue.freeze_manifest(
        test_session,
        **identity(maintenance_claim),
        manifest={
            "members": [{"key": "maintenance"}],
            "no_data_evidence": {"maintenance": {"test": "maintenance"}},
        },
    )
    await queue.close(test_session, **identity(maintenance_claim))
    await queue.settle(
        test_session,
        **identity(maintenance_claim),
        outcome="completed",
        proof={"no_data": ["maintenance"]},
    )
    rows = await queue.projection(test_session, provider="shioaji")
    assert all(
        row["wait_reason"] == "acquisition_window_closed" and row["position"] is None
        for row in rows
    )
    clock[0] = clock[0].replace(hour=18)
    resumed = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert resumed[0].work_id == works[1].work_id
    assert not resumed[1].drain_only
