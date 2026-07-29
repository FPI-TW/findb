"""Focused coverage for named Admin identity and unified credentials."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def configure_explicit_break_glass(admin_headers: dict):
    from app.api.deps import settings

    original = settings.ADMIN_BREAK_GLASS_API_KEY
    settings.ADMIN_BREAK_GLASS_API_KEY = admin_headers["X-API-Key"]
    yield
    settings.ADMIN_BREAK_GLASS_API_KEY = original


async def _bootstrap(client: AsyncClient, admin_headers: dict) -> dict:
    response = await client.post(
        "/api/v1/admin/auth/bootstrap",
        headers=admin_headers,
        json={
            "username": "Root.Owner",
            "display_name": "Root Owner",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_bootstrap_requires_explicit_break_glass_and_one_credential(
    client: AsyncClient, admin_headers: dict
):
    from app.api.deps import settings

    settings.ADMIN_BREAK_GLASS_API_KEY = ""
    legacy_only = await client.post(
        "/api/v1/admin/auth/bootstrap",
        headers=admin_headers,
        json={
            "username": "legacy-owner",
            "display_name": "Legacy Owner",
            "password": "correct horse battery staple",
        },
    )
    assert legacy_only.status_code == 403

    settings.ADMIN_BREAK_GLASS_API_KEY = "explicit-break-glass"
    dual = await client.post(
        "/api/v1/admin/auth/bootstrap",
        headers={
            "X-API-Key": "explicit-break-glass",
            "Authorization": "Bearer another-credential",
        },
        json={
            "username": "dual-owner",
            "display_name": "Dual Owner",
            "password": "correct horse battery staple",
        },
    )
    assert dual.status_code == 400


@pytest.mark.asyncio
async def test_bootstrap_session_dual_credential_and_last_owner_protection(
    client: AsyncClient, admin_headers: dict
):
    session = await _bootstrap(client, admin_headers)
    bearer = {"Authorization": f"Bearer {session['access_token']}"}

    me = await client.get("/api/v1/admin/auth/me", headers=bearer)
    assert me.status_code == 200
    assert me.json()["username"] == "root.owner"
    assert me.json()["role"] == "owner"

    duplicate = await client.post(
        "/api/v1/admin/auth/bootstrap",
        headers=admin_headers,
        json={
            "username": "other",
            "display_name": "Other",
            "password": "correct horse battery staple",
        },
    )
    assert duplicate.status_code == 409

    dual = await client.get(
        "/api/v1/admin/credentials",
        headers={**bearer, **admin_headers},
    )
    assert dual.status_code == 400

    downgrade = await client.patch(
        f"/api/v1/admin/users/{session['user']['user_id']}",
        headers=bearer,
        json={"role": "operator"},
    )
    assert downgrade.status_code == 409
    assert "last active owner" in downgrade.json()["detail"]


@pytest.mark.asyncio
async def test_rbac_password_change_gate_and_unified_credential_lifecycle(
    client: AsyncClient, admin_headers: dict, test_session: AsyncSession
):
    from app.models.registry import AdminAuditEvent

    owner_session = await _bootstrap(client, admin_headers)
    owner = {"Authorization": f"Bearer {owner_session['access_token']}"}

    created_user = await client.post(
        "/api/v1/admin/users",
        headers=owner,
        json={
            "username": "viewer",
            "display_name": "Read Only",
            "role": "viewer",
        },
    )
    assert created_user.status_code == 200
    temp_password = created_user.json()["temporary_password"]

    login = await client.post(
        "/api/v1/admin/auth/login",
        json={"username": "VIEWER", "password": temp_password},
    )
    assert login.status_code == 200
    viewer = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/admin/auth/me", headers=viewer)).status_code == 200
    assert (await client.get("/api/v1/admin/credentials", headers=viewer)).status_code == 403

    changed = await client.post(
        "/api/v1/admin/auth/change-password",
        headers=viewer,
        json={
            "current_password": temp_password,
            "new_password": "new correct horse battery staple",
        },
    )
    assert changed.status_code == 200
    relogin = await client.post(
        "/api/v1/admin/auth/login",
        json={"username": "viewer", "password": "new correct horse battery staple"},
    )
    viewer = {"Authorization": f"Bearer {relogin.json()['access_token']}"}
    assert (await client.get("/api/v1/admin/credentials", headers=viewer)).status_code == 200
    assert (
        await client.post(
            "/api/v1/admin/credentials",
            headers=viewer,
            json={"kind": "serve", "owner": "nope"},
        )
    ).status_code == 403

    source_created = await client.post(
        "/api/v1/admin/credentials",
        headers=owner,
        json={
            "kind": "source",
            "name": "twelve-data-fetcher",
            "owner": "data-platform",
            "source_name": "Twelve Data",
            "allowed_datasets": ["us_equity_eod"],
        },
    )
    assert source_created.status_code == 200
    assert source_created.json()["api_key"].startswith("findb_src_")
    assert source_created.json()["data"]["owner"] == "data-platform"
    assert source_created.json()["data"]["policies"]["source_name"] == "twelve data"

    provider_wide_source = await client.post(
        "/api/v1/admin/credentials",
        headers=owner,
        json={
            "kind": "source",
            "name": "finlab-fetcher",
            "owner": "data-platform",
            "source_name": "finlab",
            "allowed_datasets": None,
        },
    )
    assert provider_wide_source.status_code == 200
    assert provider_wide_source.json()["data"]["scopes"] is None
    assert provider_wide_source.json()["data"]["policies"]["allowed_datasets"] is None

    created = await client.post(
        "/api/v1/admin/credentials",
        headers=owner,
        json={
            "kind": "serve",
            "name": "lookup",
            "owner": "platform",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        },
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["api_key"].startswith("findb_srv_")
    assert payload["data"]["status"] == "expiring"
    assert "api_key" not in payload["data"]

    credential_id = payload["data"]["id"]
    rotated = await client.post(
        f"/api/v1/admin/credentials/serve/{credential_id}/rotate",
        headers=owner,
    )
    assert rotated.status_code == 200
    assert rotated.json()["api_key"] != payload["api_key"]
    assert rotated.json()["data"]["rotated_from_id"] == credential_id
    assert rotated.json()["data"]["expires_at"] is None
    assert rotated.json()["data"]["status"] == "active"

    revoked = await client.delete(
        f"/api/v1/admin/credentials/serve/{credential_id}",
        headers=owner,
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    overview = await client.get("/api/v1/admin/credentials/overview", headers=owner)
    assert overview.status_code == 200
    assert overview.json()["counts"]["revoked"] >= 1
    legacy = await client.get(
        "/api/v1/admin/credentials?kind=legacy",
        headers=owner,
    )
    assert legacy.status_code == 200
    assert legacy.json()["data"]
    assert all(item["kind"] == "legacy" for item in legacy.json()["data"])
    assert all("legacy_kind" in item["policies"] for item in legacy.json()["data"])
    audit_rows = list(
        (
            await test_session.execute(
                select(AdminAuditEvent).where(AdminAuditEvent.resource_id == credential_id)
            )
        )
        .scalars()
        .all()
    )
    assert {row.action for row in audit_rows} == {"create", "revoke"}
    assert all(row.actor_type == "user" for row in audit_rows)
    assert all(row.actor_display_name == "Root Owner" for row in audit_rows)


@pytest.mark.asyncio
async def test_admin_machine_key_role_and_prefix(client: AsyncClient, admin_headers: dict):
    owner_session = await _bootstrap(client, admin_headers)
    owner = {"Authorization": f"Bearer {owner_session['access_token']}"}
    created = await client.post(
        "/api/v1/admin/credentials",
        headers=owner,
        json={
            "kind": "admin",
            "name": "automation",
            "owner": "platform",
            "role": "viewer",
        },
    )
    assert created.status_code == 200
    assert created.json()["api_key"].startswith("findb_adm_")
    machine = {"X-API-Key": created.json()["api_key"]}
    assert (await client.get("/api/v1/admin/credentials", headers=machine)).status_code == 200
    assert (
        await client.post(
            "/api/v1/admin/credentials",
            headers=machine,
            json={"kind": "serve", "owner": "blocked"},
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_invalid_key_is_counted_without_logging_key_material(
    client: AsyncClient, admin_headers: dict, caplog
):
    owner_session = await _bootstrap(client, admin_headers)
    owner = {"Authorization": f"Bearer {owner_session['access_token']}"}
    secret = "must-never-appear-in-security-events"
    rejected = await client.get(
        "/api/v1/admin/credentials",
        headers={"X-API-Key": secret},
    )
    assert rejected.status_code == 403
    assert secret not in caplog.text
    overview = await client.get("/api/v1/admin/credentials/overview", headers=owner)
    assert overview.json()["auth_failure_counts"]["admin"] >= 1


def test_credential_expiry_is_utc_and_must_be_future():
    from pydantic import ValidationError

    from app.schemas.admin import CredentialCreateRequest

    normalized = CredentialCreateRequest(
        kind="serve",
        owner="test",
        expires_at=datetime.now() + timedelta(days=1),
    )
    assert normalized.expires_at is not None
    assert normalized.expires_at.tzinfo is not None
    with pytest.raises(ValidationError):
        CredentialCreateRequest(
            kind="serve",
            owner="test",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_usage_flush_preserves_newest_timestamp_across_out_of_order_flushes(
    test_session: AsyncSession, monkeypatch
):
    from app.services import credential_usage
    from app.services.api_keys import create_api_key

    credential_usage.reset_usage_state()
    row, _ = await create_api_key(
        test_session,
        owner="usage-test",
        tier="standard",
        scopes=["serve"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
    )
    newer = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    older = newer - timedelta(minutes=5)
    monkeypatch.setattr(credential_usage, "utc_now", lambda: newer)
    credential_usage.record_usage("serve", row.key_id, "/newer", 200)
    await credential_usage.flush_usage(test_session)
    monkeypatch.setattr(credential_usage, "utc_now", lambda: older)
    credential_usage.record_usage("serve", row.key_id, "/older", 200)
    await credential_usage.flush_usage(test_session)
    await test_session.refresh(row)
    assert row.usage_count == 2
    assert row.last_used_at == newer
    credential_usage.reset_usage_state()


@pytest.mark.asyncio
async def test_usage_flush_failure_requeues_events(test_session: AsyncSession):
    from app.services import credential_usage
    from app.services.api_keys import create_api_key

    class FailingSession:
        async def execute(self, _statement):
            raise RuntimeError("database unavailable")

        async def rollback(self):
            return None

    credential_usage.reset_usage_state()
    row, _ = await create_api_key(
        test_session,
        owner="retry-test",
        tier="standard",
        scopes=["serve"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
    )
    credential_usage.record_usage("serve", row.key_id, "/retry", 503)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await credential_usage.flush_usage(FailingSession())  # type: ignore[arg-type]
    assert await credential_usage.flush_usage(test_session) == 1
    await test_session.refresh(row)
    assert row.usage_count == 1
    credential_usage.reset_usage_state()


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_credential_mutation(
    client: AsyncClient,
    admin_headers: dict,
    test_session: AsyncSession,
    monkeypatch,
):
    from app.api.v1 import admin as admin_api
    from app.models.registry import APIKey

    owner_session = await _bootstrap(client, admin_headers)
    owner = {"Authorization": f"Bearer {owner_session['access_token']}"}

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(admin_api, "record_admin_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await client.post(
            "/api/v1/admin/credentials",
            headers=owner,
            json={"kind": "serve", "name": "atomic", "owner": "atomic-test"},
        )
    await test_session.rollback()
    rows = (
        await test_session.execute(select(APIKey).where(APIKey.owner == "atomic-test"))
    ).scalars()
    assert list(rows) == []


@pytest.mark.asyncio
async def test_concurrent_owner_downgrades_keep_one_active_owner(test_engine):
    from app.models.registry import AdminUser
    from app.services.admin_identity import create_user, update_user

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as setup:
        first = await create_user(
            setup,
            username="owner-one",
            display_name="Owner One",
            password="correct horse battery staple",
            role="owner",
        )
        second = await create_user(
            setup,
            username="owner-two",
            display_name="Owner Two",
            password="correct horse battery staple",
            role="owner",
        )

    async def downgrade(user_id):
        async with maker() as session:
            row = await session.get(AdminUser, user_id)
            assert row is not None
            try:
                await update_user(session, row, role="operator")
                return True
            except ValueError:
                await session.rollback()
                return False

    results = await asyncio.gather(downgrade(first.user_id), downgrade(second.user_id))
    assert sorted(results) == [False, True]
    async with maker() as verify:
        active_owners = (
            await verify.execute(
                select(AdminUser).where(
                    AdminUser.role == "owner",
                    AdminUser.is_active.is_(True),
                )
            )
        ).scalars()
        assert len(list(active_owners)) == 1
