import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from scripts.cleanup_raw import run_repeated


@pytest.mark.asyncio
async def test_stop_during_cleanup_finishes_current_pass_before_returning():
    stop = asyncio.Event()
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def cleanup():
        entered.set()
        await finish.wait()

    with patch("scripts.cleanup_raw.main", side_effect=cleanup) as cleanup_mock:
        task = asyncio.create_task(run_repeated(stop=stop))
        await entered.wait()
        stop.set()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        await asyncio.wait_for(task, 1)
        assert cleanup_mock.await_count == 1


@pytest.mark.asyncio
async def test_stop_interrupts_daily_wait_without_another_pass():
    stop = asyncio.Event()
    with patch("scripts.cleanup_raw.main", new_callable=AsyncMock) as cleanup_mock:
        task = asyncio.create_task(run_repeated(stop=stop))
        while not cleanup_mock.await_count:
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, 1)
        assert cleanup_mock.await_count == 1


@pytest.mark.asyncio
async def test_failed_pass_retries_next_interval_and_can_stop():
    stop = asyncio.Event()

    async def cleanup():
        if cleanup_mock.await_count == 1:
            raise RuntimeError("temporary failure")
        stop.set()

    with patch("scripts.cleanup_raw.main", side_effect=cleanup) as cleanup_mock:
        await asyncio.wait_for(run_repeated(interval=0.001, stop=stop), 1)
        assert cleanup_mock.await_count == 2
