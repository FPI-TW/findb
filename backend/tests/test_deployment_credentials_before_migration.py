"""The candidate's deployment credential check runs before its own migrations.

``deploy_findb_aws.sh`` runs ``reconcile_deployment_credentials.py --check-only``
from the candidate image while the database is still at the previous release's
revision.  The check must therefore work against the schema before head, both
for keys that already exist and for keys it would create; a model column added
by the newest migration must never be read or written there.
"""

from __future__ import annotations

import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.services.api_keys import hash_api_key
from scripts.reconcile_deployment_credentials import (
    LOOKUP_SERVE_SPEC,
    QUEUE_HEALTH_SPEC,
    STATIC_CACHE_SPEC,
    reconcile_deployment_credentials,
)
from tests.migration_database import (
    BACKEND_ROOT,
    get_active_migration_database_factory,
    run_alembic,
)

KEYS = {
    QUEUE_HEALTH_SPEC.env_name: "findb_adm_before_migration_queue_health_123456",
    LOOKUP_SERVE_SPEC.env_name: "findb_srv_before_migration_dashboard_lookup_123456",
    STATIC_CACHE_SPEC.env_name: "findb_srv_before_migration_static_cache_1234567",
}


def _revision_before_head() -> str:
    scripts = ScriptDirectory.from_config(Config(str(BACKEND_ROOT / "alembic.ini")))
    (head,) = scripts.get_heads()
    previous = scripts.get_revision(head).down_revision
    assert isinstance(previous, str)
    return previous


@pytest.mark.asyncio
async def test_check_only_works_against_the_schema_before_head() -> None:
    previous = _revision_before_head()
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "credentials_before"
    ) as (
        url,
        engine,
    ):
        await run_alembic(url, previous)

        # Nothing issued yet: the check would create every key, then rolls back.
        created = await reconcile_deployment_credentials(url, KEYS, "staging", check_only=True)
        assert [result.action for result in created] == ["created", "created", "created"]

        # Keys the previous release issued: the check reads and compares them.
        for spec in (QUEUE_HEALTH_SPEC, LOOKUP_SERVE_SPEC, STATIC_CACHE_SPEC):
            key_hash = hash_api_key(KEYS[spec.env_name])
            async with engine.begin() as db:
                await db.execute(
                    text(
                        "INSERT INTO api_key (key_id, key_hash, fingerprint, kind, name, owner,"
                        " description, role, tier, scopes, rate_limit_requests,"
                        " rate_limit_window, page_size_limit, usage_count, created_at)"
                        " VALUES (:id, :hash, :fingerprint, :kind, :name, 'deployment',"
                        " :description, :role, 'internal', CAST(:scopes AS jsonb), 120, 60,"
                        " :page_size, 0, now())"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "hash": key_hash,
                        "fingerprint": key_hash[:16],
                        "kind": spec.kind,
                        "name": spec.name,
                        "description": spec.description,
                        "role": spec.role,
                        "scopes": "[" + ",".join(f'"{scope}"' for scope in spec.scopes) + "]",
                        "page_size": spec.page_size_limit,
                    },
                )
        existing = await reconcile_deployment_credentials(url, KEYS, "staging", check_only=True)
        assert [result.action for result in existing] == ["unchanged", "unchanged", "unchanged"]
