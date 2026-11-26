"""Execution lanes: one FIFO and owner per (provider, lane), one account (#691 step 2a)."""

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.provider_work import ClaimRequest
from app.models.provider_work import ProviderWork, ProviderWorkOwner
from app.models.registry import AdminAuditEvent, DatasetRegistry
from app.provider_catalog import ProviderCatalog
from app.services import provider_metadata
from app.services import provider_work as queue
from app.utils import utc_now

INTRADAY = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")


def _catalog_data(**lanes: str) -> dict:
    data = provider_metadata.provider_catalog().model_dump(mode="json")
    for feed in data["feeds"]:
        if feed["dataset_key"] in lanes:
            feed["lane"] = lanes[feed["dataset_key"]]
    return data


@pytest.fixture
def intraday_lane(monkeypatch):
    """The catalog as it will be once FinLab's intraday feeds run in their own lane."""
    catalog = ProviderCatalog.model_validate(_catalog_data(**{key: "intraday" for key in INTRADAY}))
    monkeypatch.setattr(provider_metadata, "provider_catalog", lambda: catalog)
    return catalog


async def _datasets(db, *keys: str) -> None:
    for key in keys:
        if await db.get(DatasetRegistry, key) is None:
            db.add(DatasetRegistry(dataset_key=key, name=key, market="TW", asset_class="equity"))
    await db.flush()


async def _maintenance(db, dataset_key: str) -> ProviderWork:
    return await queue.enqueue(
        db,
        provider="finlab",
        stable_key=f"maintenance:{dataset_key}:test",
        kind="maintenance",
        dataset_key=dataset_key,
        trade_date=None,
        scope={"dataset_key": dataset_key},
    )


async def _claim(db, lane: str, identity: str, **options):
    return await queue.claim(
        db,
        provider="finlab",
        datasets=["tw_equity_eod", *INTRADAY],
        host_lock_identity=identity,
        lane=lane,
        **options,
    )


def test_every_current_feed_runs_in_the_default_lane():
    catalog = provider_metadata.provider_catalog()
    assert {feed.lane for feed in catalog.feeds} == {"default"}
    for provider in catalog.providers:
        assert provider_metadata.provider_lanes(provider.provider) == ("default",)
    assert provider_metadata.provider_lanes("shioaji") == ()


@pytest.mark.parametrize(
    ("lanes", "message"),
    [
        # Historical and its retries run where the provider's backfills are.
        ({"tw_etf_eod": "tables"}, "only acquiring feeds without historical capability"),
        # Nothing would be left to run historical and maintenance work.
        (
            {"tw_equity_eod": "tables", "tw_etf_eod": "tables", **dict.fromkeys(INTRADAY, "x")},
            "only acquiring feeds without historical capability",
        ),
        ({"tw_equity_bar_1m": "Intraday"}, "String should match pattern"),
    ],
)
def test_the_catalog_rejects_a_lane_that_strands_the_default_runner(lanes, message):
    with pytest.raises(ValidationError, match=message):
        ProviderCatalog.model_validate(_catalog_data(**lanes))


def test_a_provider_needs_an_acquiring_feed_in_its_default_lane():
    data = _catalog_data(**dict.fromkeys(INTRADAY, "intraday"))
    data["feeds"] = [
        feed
        for feed in data["feeds"]
        if feed["provider"] != "finlab" or feed["dataset_key"] in INTRADAY
    ]
    with pytest.raises(ValidationError, match="acquiring default-lane feed"):
        ProviderCatalog.model_validate(data)


def test_a_work_takes_its_feed_lane_and_historical_always_runs_in_default(intraday_lane):
    assert provider_metadata.provider_lanes("finlab") == ("default", "intraday")
    assert provider_metadata.work_lane("finlab", "daily", "tw_equity_tick") == "intraday"
    assert provider_metadata.work_lane("finlab", "maintenance", "tw_etf_bar_1m") == "intraday"
    assert provider_metadata.work_lane("finlab", "historical", "tw_etf_bar_1m") == "default"
    assert provider_metadata.work_lane("finlab", "daily", "tw_equity_eod") == "default"


def test_a_claim_request_names_a_lane_only_in_the_catalog_shape():
    base = {"protocol": "provider-work-v1", "host_lock_identity": "local:1:2"}
    assert ClaimRequest.model_validate(base).lane is None
    assert ClaimRequest.model_validate({**base, "lane": "intraday"}).lane == "intraday"
    with pytest.raises(ValidationError):
        ClaimRequest.model_validate({**base, "lane": "Intraday-1"})


@pytest.mark.asyncio
async def test_lanes_keep_separate_fifos_and_owners(test_session, intraday_lane):
    await _datasets(test_session, "tw_equity_eod", "tw_equity_bar_1m")
    # The intraday work is queued first; the default lane still takes the daily table.
    bar = await _maintenance(test_session, "tw_equity_bar_1m")
    eod = await _maintenance(test_session, "tw_equity_eod")
    assert (bar.lane, eod.lane) == ("intraday", "default")

    default = await _claim(test_session, "default", "local:1:10")
    intraday = await _claim(test_session, "intraday", "local:1:11")

    assert default is not None and intraday is not None
    assert default[0].work_id == eod.work_id
    assert intraday[0].work_id == bar.work_id
    # Two owners hold an attempt at the same time, one per lane.
    owners = (
        await test_session.scalars(
            select(ProviderWorkOwner).where(ProviderWorkOwner.provider == "finlab")
        )
    ).all()
    assert {(owner.lane, owner.attempt_id is not None) for owner in owners} == {
        ("default", True),
        ("intraday", True),
    }
    # Each lane's fence is its own: the default identity cannot settle intraday work.
    with pytest.raises(queue.ProviderWorkError, match="fence differs"):
        await queue.fence(
            test_session,
            provider="finlab",
            work_id=bar.work_id,
            generation=default[2].generation,
            token=default[2].token,
        )


@pytest.mark.asyncio
async def test_an_unknown_lane_is_refused(test_session, intraday_lane):
    with pytest.raises(queue.ProviderWorkError, match="lane is not in the catalog"):
        await _claim(test_session, "tables", "local:1:10")


@pytest.mark.asyncio
async def test_a_cooldown_one_lane_records_holds_every_lane(test_session, intraday_lane):
    await _datasets(test_session, "tw_equity_eod", "tw_equity_bar_1m")
    bar = await _maintenance(test_session, "tw_equity_bar_1m")
    eod = await _maintenance(test_session, "tw_equity_eod")
    _, _, intraday_owner = await _claim(test_session, "intraday", "local:1:11")
    _, _, default_owner = await _claim(test_session, "default", "local:1:10")

    await queue.heartbeat(
        test_session,
        provider="finlab",
        work_id=bar.work_id,
        generation=intraday_owner.generation,
        token=intraday_owner.token,
        acquisition_not_before=utc_now() + timedelta(minutes=10),
    )

    # The account is shared: the default lane may not acquire during the cooldown.
    with pytest.raises(queue.ProviderWorkSuspendedError, match="cooldown"):
        await queue.fence(
            test_session,
            provider="finlab",
            work_id=eod.work_id,
            generation=default_owner.generation,
            token=default_owner.token,
            acquisition=True,
        )
    assert await queue.provider_cooldown(test_session, "finlab") == (
        intraday_owner.acquisition_not_before
    )


async def _cutover(db) -> None:
    await queue.apply_cutover(
        db,
        {
            "protocol": "provider-work-v1",
            "environment": queue.environment(),
            "host_id": "test-host",
            "inventory_sha256": "a" * 64,
            "checked_at": queue.utc_now().isoformat(),
            "host_lock_identities": {
                name: f"{queue.environment()}:1:{100 + index}"
                for index, name in enumerate(queue.CUTOVER_PROVIDERS)
            },
        },
    )
    await db.commit()


@pytest.mark.asyncio
async def test_a_new_lane_binds_its_own_lock_beside_the_proven_default_lane(
    test_session, intraday_lane
):
    await _cutover(test_session)
    await _datasets(test_session, "tw_equity_bar_1m")
    # Work the default owner settled before the lane existed does not hold it back.
    done = await _maintenance(test_session, "tw_equity_bar_1m")
    done.state = "completed"
    await test_session.commit()
    identity = f"{queue.environment()}:1:999"

    assert await _claim(test_session, "intraday", identity, require_cutover=True) is None

    owner = await test_session.get(ProviderWorkOwner, (queue.environment(), "finlab", "intraday"))
    assert owner.host_lock_identity == identity
    audit = await test_session.scalar(
        select(AdminAuditEvent).where(AdminAuditEvent.action == "register_host_lock")
    )
    assert audit.resource_id == f"{queue.environment()}:finlab:intraday"
    assert audit.details == {
        "provider": "finlab",
        "lane": "intraday",
        "host_lock_identity": identity,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["default_lock_reused", "default_unproven", "open_work"])
async def test_a_new_lane_stays_behind_the_cutover_when_unproven(test_session, intraday_lane, case):
    await _cutover(test_session)
    identity = f"{queue.environment()}:1:999"
    finlab = queue.CUTOVER_PROVIDERS.index("finlab")
    if case == "default_lock_reused":
        identity = f"{queue.environment()}:1:{100 + finlab}"
    elif case == "default_unproven":
        default = await test_session.get(
            ProviderWorkOwner, (queue.environment(), "finlab", "default")
        )
        default.recovery_reason = "heartbeat_missing"
        await test_session.commit()
    else:
        await _datasets(test_session, "tw_equity_bar_1m")
        work = await _maintenance(test_session, "tw_equity_bar_1m")
        work.state = "running"
        await test_session.commit()

    with pytest.raises(queue.ProviderWorkError, match="cutover preflight"):
        await _claim(test_session, "intraday", identity, require_cutover=True)


@pytest.mark.asyncio
async def test_one_lane_lock_never_waits_for_another_but_the_provider_lock_does(
    test_engine, intraday_lane
):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as holder, factory() as other:
        await queue.lock_lane(holder, "finlab", "default")
        try:
            # Another lane of the same provider proceeds while the default lane is held.
            await asyncio.wait_for(queue.lock_lane(other, "finlab", "intraday"), 5)
            await other.rollback()
            # A provider-wide operation (start, stop, cutover) waits for every lane.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(queue.lock_provider(other, "finlab"), 0.5)
        finally:
            await other.rollback()
            await holder.rollback()
