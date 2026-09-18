"""Tests for DB-backed deployment credential reconciliation."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import APIKey
from app.services.api_keys import hash_api_key
from app.utils import utc_now, uuid7
from scripts.reconcile_deployment_credentials import (
    LOOKUP_SERVE_SPEC,
    QUEUE_HEALTH_NAME,
    STATIC_CACHE_SPEC,
    CredentialSpec,
    reconcile_machine_credential,
    reconcile_queue_health_credential,
)


async def test_queue_health_credential_is_created_and_idempotent(
    test_session: AsyncSession,
) -> None:
    plaintext = "findb_adm_test_queue_health_credential_123456789"

    created = await reconcile_queue_health_credential(test_session, plaintext)
    unchanged = await reconcile_queue_health_credential(test_session, plaintext)
    row = (
        await test_session.execute(
            select(APIKey).where(APIKey.name == QUEUE_HEALTH_NAME, APIKey.revoked_at.is_(None))
        )
    ).scalar_one()

    assert created.action == "created"
    assert unchanged.action == "unchanged"
    assert row.key_hash == hash_api_key(plaintext)
    assert row.fingerprint == hash_api_key(plaintext)[:16]
    assert row.kind == "admin"
    assert row.role == "viewer"
    assert row.scopes == ["admin:queue:read"]


async def test_queue_health_credential_rotation_revokes_previous_key(
    test_session: AsyncSession,
) -> None:
    previous = "findb_adm_previous_queue_health_credential_12345"
    replacement = "findb_adm_replacement_queue_health_credential_123"
    await reconcile_queue_health_credential(test_session, previous)

    rotated = await reconcile_queue_health_credential(test_session, replacement)
    rows = list(
        (
            await test_session.execute(
                select(APIKey).where(APIKey.name == QUEUE_HEALTH_NAME).order_by(APIKey.created_at)
            )
        )
        .scalars()
        .all()
    )

    assert rotated.action == "rotated"
    assert len(rows) == 2
    assert rows[0].revoked_at is not None
    assert rows[1].rotated_from_id == rows[0].key_id
    assert rows[1].key_hash == hash_api_key(replacement)


async def test_queue_health_credential_rejects_key_reused_by_another_identity(
    test_session: AsyncSession,
) -> None:
    from scripts.reconcile_deployment_credentials import DeploymentCredentialError

    plaintext = "findb_adm_reused_queue_health_credential_123456"
    test_session.add(
        APIKey(
            key_id=uuid7(),
            key_hash=hash_api_key(plaintext),
            fingerprint=hash_api_key(plaintext)[:16],
            kind="admin",
            name="another-machine",
            owner="test",
            role="viewer",
            tier="internal",
            scopes=[],
            rate_limit_requests=100,
            rate_limit_window=60,
            page_size_limit=100,
            usage_count=0,
            created_at=utc_now(),
        )
    )
    await test_session.commit()

    with pytest.raises(DeploymentCredentialError):
        await reconcile_queue_health_credential(test_session, plaintext)


@pytest.mark.parametrize(
    ("spec", "plaintext"),
    [
        (LOOKUP_SERVE_SPEC, "findb_srv_test_dashboard_lookup_credential_123456"),
        (STATIC_CACHE_SPEC, "findb_srv_test_static_cache_credential_123456789"),
    ],
)
async def test_serve_deployment_credentials_use_runtime_serve_scope(
    test_session: AsyncSession,
    spec: CredentialSpec,
    plaintext: str,
) -> None:
    await reconcile_machine_credential(test_session, plaintext, spec)
    await test_session.commit()

    row = (await test_session.execute(select(APIKey).where(APIKey.name == spec.name))).scalar_one()

    assert row.kind == "serve"
    assert row.scopes == ["serve"]
    assert row.page_size_limit == spec.page_size_limit


def test_static_cache_credential_allows_generator_page_size() -> None:
    assert STATIC_CACHE_SPEC.page_size_limit == 1000
