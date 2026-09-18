"""Tests for DB-backed deployment credential reconciliation."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.registry import APIKey
from app.services.api_keys import hash_api_key
from app.utils import utc_now, uuid7
from scripts.reconcile_deployment_credentials import (
    LOOKUP_SERVE_SPEC,
    QUEUE_HEALTH_NAME,
    QUEUE_HEALTH_SPEC,
    STATIC_CACHE_SPEC,
    CredentialSpec,
    legacy_names_for_spec,
    reconcile_deployment_credentials,
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


async def test_queue_health_credential_adopts_exact_target_legacy_identity(
    test_session: AsyncSession,
) -> None:
    plaintext = "findb_adm_staging_queue_health_credential_123456"
    row = APIKey(
        key_id=uuid7(),
        key_hash=hash_api_key(plaintext),
        fingerprint=hash_api_key(plaintext)[:16],
        kind="admin",
        name="staging queue health",
        owner="tyler",
        role="viewer",
        tier="internal",
        scopes=["admin:queue:read"],
        rate_limit_requests=120,
        rate_limit_window=60,
        page_size_limit=100,
        usage_count=0,
        created_at=utc_now(),
    )
    test_session.add(row)
    await test_session.commit()

    result = await reconcile_machine_credential(
        test_session,
        plaintext,
        QUEUE_HEALTH_SPEC,
        legacy_names=legacy_names_for_spec(QUEUE_HEALTH_SPEC, "staging"),
    )
    await test_session.commit()

    assert result.action == "adopted"
    assert row.name == QUEUE_HEALTH_NAME
    assert row.owner == "deployment"
    assert row.description == QUEUE_HEALTH_SPEC.description


async def test_queue_health_credential_rejects_other_target_legacy_identity(
    test_session: AsyncSession,
) -> None:
    from scripts.reconcile_deployment_credentials import DeploymentCredentialError

    plaintext = "findb_adm_production_queue_health_credential_123"
    test_session.add(
        APIKey(
            key_id=uuid7(),
            key_hash=hash_api_key(plaintext),
            fingerprint=hash_api_key(plaintext)[:16],
            kind="admin",
            name="production queue health",
            owner="deployment",
            role="viewer",
            tier="internal",
            scopes=["admin:queue:read"],
            rate_limit_requests=120,
            rate_limit_window=60,
            page_size_limit=100,
            usage_count=0,
            created_at=utc_now(),
        )
    )
    await test_session.commit()

    with pytest.raises(DeploymentCredentialError, match="api_key_used_by_another_identity"):
        await reconcile_machine_credential(
            test_session,
            plaintext,
            QUEUE_HEALTH_SPEC,
            legacy_names=legacy_names_for_spec(QUEUE_HEALTH_SPEC, "staging"),
        )


@pytest.mark.parametrize(
    ("spec", "legacy_name", "plaintext"),
    [
        (
            LOOKUP_SERVE_SPEC,
            "staging lookup",
            "findb_srv_staging_lookup_legacy_credential_123456",
        ),
        (
            STATIC_CACHE_SPEC,
            "staging static cache",
            "findb_srv_staging_static_cache_legacy_credential_123",
        ),
    ],
)
async def test_serve_credential_adopts_exact_target_legacy_identity(
    test_session: AsyncSession,
    spec: CredentialSpec,
    legacy_name: str,
    plaintext: str,
) -> None:
    row = APIKey(
        key_id=uuid7(),
        key_hash=hash_api_key(plaintext),
        fingerprint=hash_api_key(plaintext)[:16],
        kind="serve",
        name=legacy_name,
        owner="tyler",
        role=None,
        tier="internal",
        scopes=["serve"],
        rate_limit_requests=120,
        rate_limit_window=60,
        page_size_limit=1000,
        usage_count=0,
        created_at=utc_now(),
    )
    test_session.add(row)
    await test_session.commit()

    result = await reconcile_machine_credential(
        test_session,
        plaintext,
        spec,
        legacy_names=legacy_names_for_spec(spec, "staging"),
    )
    await test_session.commit()

    assert result.action == "adopted"
    assert row.name == spec.name
    assert row.owner == "deployment"
    assert row.description == spec.description


async def test_check_only_adopts_legacy_identity_then_rolls_back(
    test_session: AsyncSession,
    test_engine: AsyncEngine,
) -> None:
    queue_key = "findb_adm_staging_queue_health_check_only_123456"
    test_session.add(
        APIKey(
            key_id=uuid7(),
            key_hash=hash_api_key(queue_key),
            fingerprint=hash_api_key(queue_key)[:16],
            kind="admin",
            name="staging queue health",
            owner="tyler",
            role="viewer",
            tier="internal",
            scopes=["admin:queue:read"],
            rate_limit_requests=120,
            rate_limit_window=60,
            page_size_limit=100,
            usage_count=0,
            created_at=utc_now(),
        )
    )
    await test_session.commit()

    results = await reconcile_deployment_credentials(
        test_engine.url.render_as_string(hide_password=False),
        {
            "FINDB_QUEUE_HEALTH_ADMIN_API_KEY": queue_key,
            "FINDB_LOOKUP_SERVE_API_KEY": "findb_srv_lookup_check_only_credential_123456",
            "FINDB_STATIC_CACHE_SERVE_API_KEY": (
                "findb_srv_static_cache_check_only_credential_123456"
            ),
        },
        "staging",
        check_only=True,
    )

    test_session.expire_all()
    rows = list((await test_session.execute(select(APIKey))).scalars().all())
    assert [result.action for result in results] == ["adopted", "created", "created"]
    assert len(rows) == 1
    assert rows[0].name == "staging queue health"
    assert rows[0].owner == "tyler"


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
