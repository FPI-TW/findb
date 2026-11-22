"""A provider added after the Owner cutover binds its own host lock on first claim."""

import pytest
from sqlalchemy import delete, select

from app.models.provider_work import ProviderWork, ProviderWorkOwner
from app.models.registry import AdminAuditEvent, DatasetRegistry
from app.services import provider_work as queue
from app.utils import uuid7


async def _cutover_then_add(db, provider: str) -> None:
    """Attest every catalog provider on device 1, then drop one as if added later."""
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
    await db.execute(
        delete(ProviderWorkOwner).where(
            ProviderWorkOwner.environment == queue.environment(),
            ProviderWorkOwner.provider == provider,
        )
    )
    await db.commit()


async def _work(db, provider: str, state: str, scope: dict) -> None:
    if await db.get(DatasetRegistry, "provider_work_test") is None:
        db.add(
            DatasetRegistry(
                dataset_key="provider_work_test", name="test", market="US", asset_class="equity"
            )
        )
        await db.flush()
    db.add(
        ProviderWork(
            work_id=uuid7(),
            environment=queue.environment(),
            provider=provider,
            stable_key=f"test:{state}:{len(scope)}",
            kind="maintenance",
            dataset_key="provider_work_test",
            scope=scope,
            state=state,
        )
    )
    await db.commit()


async def _claim(db, provider: str, identity: str):
    return await queue.claim(
        db,
        provider=provider,
        datasets=["none"],
        host_lock_identity=identity,
        require_cutover=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", queue.CUTOVER_PROVIDERS)
async def test_a_new_provider_binds_its_lock_on_the_attested_device(test_session, provider):
    await _cutover_then_add(test_session, provider)
    identity = f"{queue.environment()}:1:999"

    assert await _claim(test_session, provider, identity) is None
    owner = await test_session.get(ProviderWorkOwner, (queue.environment(), provider))
    assert owner.host_lock_identity == identity
    audit = await test_session.scalar(
        select(AdminAuditEvent).where(AdminAuditEvent.action == "register_host_lock")
    )
    assert audit.resource_id == f"{queue.environment()}:{provider}"
    assert audit.details == {"provider": provider, "host_lock_identity": identity}

    # Bound once: a different lock file is refused like any attested provider.
    with pytest.raises(queue.ProviderWorkError, match="inode differs"):
        await _claim(test_session, provider, f"{queue.environment()}:1:998")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        "{env}:2:999",  # another device than the attested mount
        "{env}:1:100",  # an attested provider's own lock file
        "other:1:999",  # another environment
        "{env}:1",  # malformed
        "{env}:x:999",
    ],
)
async def test_a_new_provider_lock_off_the_attested_mount_stays_behind_cutover(
    test_session, identity
):
    provider = queue.CUTOVER_PROVIDERS[-1]
    await _cutover_then_add(test_session, provider)

    with pytest.raises(queue.ProviderWorkError, match="cutover preflight"):
        await _claim(test_session, provider, identity.format(env=queue.environment()))


@pytest.mark.asyncio
async def test_without_an_environment_cutover_no_provider_binds_itself(test_session):
    provider = queue.CUTOVER_PROVIDERS[0]
    with pytest.raises(queue.ProviderWorkError, match="cutover preflight"):
        await _claim(test_session, provider, f"{queue.environment()}:1:999")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "scope"),
    [("running", {}), ("completed", {}), ("queued", {"legacy_id": "x"})],
)
async def test_a_provider_with_used_or_legacy_work_needs_the_cutover(test_session, state, scope):
    provider = queue.CUTOVER_PROVIDERS[-1]
    await _cutover_then_add(test_session, provider)
    await _work(test_session, provider, state, scope)

    with pytest.raises(queue.ProviderWorkError, match="cutover preflight"):
        await _claim(test_session, provider, f"{queue.environment()}:1:999")


@pytest.mark.asyncio
async def test_queued_work_enqueued_before_the_first_consumer_does_not_block(test_session):
    provider = queue.CUTOVER_PROVIDERS[-1]
    await _cutover_then_add(test_session, provider)
    await _work(test_session, provider, "queued", {})

    assert await _claim(test_session, provider, f"{queue.environment()}:1:999") is None
    owner = await test_session.get(ProviderWorkOwner, (queue.environment(), provider))
    assert owner.host_lock_identity == f"{queue.environment()}:1:999"


@pytest.mark.asyncio
async def test_a_previously_claimed_owner_never_rebinds(test_session):
    provider = queue.CUTOVER_PROVIDERS[-1]
    await _cutover_then_add(test_session, provider)
    test_session.add(
        ProviderWorkOwner(environment=queue.environment(), provider=provider, generation=3)
    )
    await test_session.commit()

    with pytest.raises(queue.ProviderWorkError, match="cutover preflight"):
        await _claim(test_session, provider, f"{queue.environment()}:1:999")
