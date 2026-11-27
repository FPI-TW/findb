"""FinLab intraday work moves to its own lane; lane polls get their own rows (e5979e281ba9)."""

import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "36bfabaae072"
LANE = "e5979e281ba9"
INTRADAY = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")


async def _work(db, dataset_key: str, state: str, *, provider: str = "finlab") -> str:
    work_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO provider_work(work_id,environment,provider,stable_key,kind,dataset_key,"
            "trade_date,scope,state,created_at) VALUES (:work_id,'local',:provider,:key,'daily',"
            ":dataset,'2026-11-26','{}'::jsonb,:state,now())"
        ),
        {
            "work_id": work_id,
            "provider": provider,
            "key": f"daily:{dataset_key}:{work_id}",
            "dataset": dataset_key,
            "state": state,
        },
    )
    return work_id


async def _attempt(db, work_id: str, *, number: int = 1, state: str = "queued") -> str:
    attempt_id = str(uuid4())
    await db.execute(
        text(
            "INSERT INTO provider_work_attempt(attempt_id,work_id,attempt_number,state,"
            "not_before,drain_only) VALUES (:attempt_id,:work_id,:number,:state,now(),false)"
        ),
        {"attempt_id": attempt_id, "work_id": work_id, "number": number, "state": state},
    )
    return attempt_id


async def _lanes(db) -> dict[str, str]:
    rows = await db.execute(text("SELECT dataset_key, state, lane FROM provider_work"))
    return {f"{dataset}:{state}": lane for dataset, state, lane in rows.all()}


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["running", "recovery_blocked", "owned"])
async def test_upgrade_refuses_intraday_work_a_default_round_holds(held):
    async with get_active_migration_database_factory().clone(
        "1587769858c0", f"finlab_intraday_lane_{held}"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            if held == "owned":
                work = await _work(db, "tw_etf_tick", "queued")
                attempt = await _attempt(db, work)
                await db.execute(
                    text(
                        "INSERT INTO provider_work_owner(environment,provider,generation,attempt_id)"
                        " VALUES ('local','finlab',3,:attempt)"
                    ),
                    {"attempt": attempt},
                )
            else:
                await _work(db, "tw_equity_bar_1m", held)

        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, LANE)
        assert "running or owned in the default lane" in refused.value.stderr
        async with engine.connect() as db:
            assert set((await _lanes(db)).values()) == {"default"}


@pytest.mark.asyncio
async def test_upgrade_refuses_unfinished_intraday_work_that_already_ran_a_round():
    # The round's member progress sits in the default consumer's checkpoint; the lane
    # consumer would start the plan over and re-report immutable no_data outcomes.
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "finlab_intraday_lane_started"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            work = await _work(db, "tw_equity_bar_1m", "queued")
            await _attempt(db, work, state="settled")
            await _attempt(db, work, number=2)
            # A finished work that ran rounds is no obstacle.
            done = await _work(db, "tw_etf_tick", "failed")
            await _attempt(db, done, state="settled")

        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, LANE)
        assert "already ran a round in the default lane" in refused.value.stderr
        async with engine.connect() as db:
            assert set((await _lanes(db)).values()) == {"default"}

        async with engine.begin() as db:
            await db.execute(
                text("UPDATE provider_work SET state = 'partial' WHERE work_id = :work"),
                {"work": work},
            )
        await run_alembic(url, LANE)
        async with engine.connect() as db:
            assert set((await _lanes(db)).values()) == {"intraday"}


@pytest.mark.asyncio
async def test_queued_and_finished_intraday_work_moves_and_downgrade_refuses_a_used_lane():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "finlab_intraday_lane"
    ) as (url, engine):
        await run_alembic(url, BEFORE)
        async with engine.begin() as db:
            # Queued work that never ran moves; so does finished work. The daily EOD and
            # other providers' work stay where they are.
            for dataset in INTRADAY:
                await _work(db, dataset, "queued")
            await _work(db, "tw_equity_tick", "completed")
            await _work(db, "tw_equity_eod", "running")
            await _work(db, "us_equity_eod", "queued", provider="twelve_data")
            await db.execute(
                text(
                    "INSERT INTO provider_work_owner(environment,provider,generation) "
                    "VALUES ('local','finlab',7)"
                )
            )

        await run_alembic(url, LANE)
        async with engine.begin() as db:
            assert await _lanes(db) == {
                **{f"{dataset}:queued": "intraday" for dataset in INTRADAY},
                "tw_equity_tick:completed": "intraday",
                "tw_equity_eod:running": "default",
                "us_equity_eod:queued": "default",
            }
            control = await db.scalar(
                text("SELECT scheduler_key FROM scheduler_control WHERE provider = 'finlab'")
            )
            await db.execute(
                text(
                    "INSERT INTO scheduler_control_lane(scheduler_key,lane,observed_state,"
                    "last_heartbeat_at,updated_at) VALUES (:key,'intraday','running',now(),now())"
                ),
                {"key": control},
            )
            for lane, state in (("default", "running"), ("Intraday", "running"), ("x", "busy")):
                with pytest.raises(Exception, match="valid"):
                    async with db.begin_nested():
                        await db.execute(
                            text(
                                "INSERT INTO scheduler_control_lane(scheduler_key,lane,"
                                "observed_state,last_heartbeat_at,updated_at) "
                                "VALUES (:key,:lane,:state,now(),now())"
                            ),
                            {"key": control, "lane": lane, "state": state},
                        )
            # The intraday owner took a round: the two FIFOs can no longer be folded back.
            await db.execute(
                text(
                    "INSERT INTO provider_work_owner(environment,provider,lane,generation) "
                    "VALUES ('local','finlab','intraday',1)"
                )
            )

        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "intraday lane was used" in refused.value.stderr

        async with engine.begin() as db:
            # A lane owner that registered its lock but never claimed is unused.
            await db.execute(
                text(
                    "UPDATE provider_work_owner SET generation = 0, host_lock_identity = 'local:1:9'"
                    " WHERE lane = 'intraday'"
                )
            )
        await run_alembic(url, BEFORE, command="downgrade")
        async with engine.connect() as db:
            assert set((await _lanes(db)).values()) == {"default"}
            owners = (
                await db.execute(text("SELECT provider, lane, generation FROM provider_work_owner"))
            ).all()
            assert [tuple(row) for row in owners] == [("finlab", "default", 7)]
            assert (
                await db.scalar(text("SELECT to_regclass('public.scheduler_control_lane')"))
            ) is None


@pytest.mark.asyncio
async def test_downgrade_refuses_while_intraday_work_is_running():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "finlab_intraday_lane_running"
    ) as (url, engine):
        await run_alembic(url, LANE)
        async with engine.begin() as db:
            await _work(db, "tw_etf_bar_1m", "running")
            await db.execute(
                text(
                    "UPDATE provider_work SET lane = 'intraday' WHERE dataset_key = 'tw_etf_bar_1m'"
                )
            )
        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "intraday lane was used" in refused.value.stderr
