"""Reconcile DB-backed machine credentials required by deployment probes.

Secrets Manager is the plaintext source of truth, while the application
database stores only credential hashes. A newly migrated database therefore
needs this bounded reconciliation before authenticated readiness checks run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.registry import APIKey
from app.services.api_keys import hash_api_key
from app.utils import utc_now, uuid7


class DeploymentCredentialError(RuntimeError):
    """Raised when a deployment credential cannot be reconciled safely."""


@dataclass(frozen=True)
class CredentialSpec:
    """Expected database identity for one deployment-owned secret."""

    env_name: str
    name: str
    kind: str
    role: str | None
    description: str
    scopes: tuple[str, ...]
    page_size_limit: int


QUEUE_HEALTH_SPEC = CredentialSpec(
    env_name="FINDB_QUEUE_HEALTH_ADMIN_API_KEY",
    name="deployment-queue-health",
    kind="admin",
    role="viewer",
    description="Deployment queue-health readiness probe",
    scopes=("admin:queue:read",),
    page_size_limit=100,
)
LOOKUP_SERVE_SPEC = CredentialSpec(
    env_name="FINDB_LOOKUP_SERVE_API_KEY",
    name="dashboard-lookup",
    kind="serve",
    role=None,
    description="Dashboard lookup Serve API credential",
    scopes=("serve",),
    page_size_limit=100,
)
STATIC_CACHE_SPEC = CredentialSpec(
    env_name="FINDB_STATIC_CACHE_SERVE_API_KEY",
    name="static-cache-generation",
    kind="serve",
    role=None,
    description="Static cache generation Serve API credential",
    scopes=("serve",),
    page_size_limit=1000,
)
DEPLOYMENT_CREDENTIAL_SPECS = (QUEUE_HEALTH_SPEC, LOOKUP_SERVE_SPEC, STATIC_CACHE_SPEC)
QUEUE_HEALTH_NAME = QUEUE_HEALTH_SPEC.name


@dataclass(frozen=True)
class ReconciliationResult:
    """Bounded result safe to print in deployment logs."""

    name: str
    action: str
    fingerprint: str


def validate_application_database_url(database_url: str) -> URL:
    """Require a PostgreSQL application connection with an explicit identity."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise DeploymentCredentialError("application database URL must use PostgreSQL")
    if not url.host or not url.database or not url.username:
        raise DeploymentCredentialError(
            "application database URL must include host, database, and username"
        )
    return url


def validate_plaintext_api_key(api_key: str) -> str:
    """Reject malformed secret values without logging their contents."""
    value = api_key.strip()
    if value != api_key or not 32 <= len(value) <= 512 or any(char.isspace() for char in value):
        raise DeploymentCredentialError("deployment API key has an unsafe format")
    return value


def _is_active(row: APIKey, now: datetime) -> bool:
    return row.revoked_at is None and (row.expires_at is None or row.expires_at > now)


def _apply_credential_policy(row: APIKey, spec: CredentialSpec, key_hash: str) -> bool:
    desired: dict[str, object] = {
        "owner": "deployment",
        "description": spec.description,
        "role": spec.role,
        "tier": "internal",
        "fingerprint": key_hash[:16],
        "scopes": list(spec.scopes),
        "rate_limit_requests": 120,
        "rate_limit_window": 60,
        "page_size_limit": spec.page_size_limit,
    }
    changed = False
    for field, value in desired.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed


async def reconcile_machine_credential(
    db: AsyncSession,
    plaintext_api_key: str,
    spec: CredentialSpec,
) -> ReconciliationResult:
    """Create or rotate one dedicated deployment machine credential."""
    plaintext = validate_plaintext_api_key(plaintext_api_key)
    key_hash = hash_api_key(plaintext)
    now = utc_now()
    rows = list(
        (
            await db.execute(
                select(APIKey)
                .where((APIKey.name == spec.name) | (APIKey.key_hash == key_hash))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    hash_matches = [row for row in rows if row.key_hash == key_hash]
    active_named = [row for row in rows if row.name == spec.name and _is_active(row, now)]

    if len(hash_matches) > 1 or len(active_named) > 1:
        raise DeploymentCredentialError("deployment credential state is ambiguous")

    if hash_matches:
        row = hash_matches[0]
        if row.name != spec.name or row.kind != spec.kind:
            raise DeploymentCredentialError(
                "deployment API key is already used by another identity"
            )
        if not _is_active(row, now) or row.expires_at is not None:
            raise DeploymentCredentialError("deployment credential is revoked or expiring")
        if active_named and active_named[0].key_id != row.key_id:
            raise DeploymentCredentialError("another deployment credential is active")
        changed = _apply_credential_policy(row, spec, key_hash)
        return ReconciliationResult(
            name=spec.name,
            action="updated" if changed else "unchanged",
            fingerprint=key_hash[:16],
        )

    rotated_from_id = None
    if active_named:
        previous = active_named[0]
        previous.revoked_at = now
        rotated_from_id = previous.key_id

    row = APIKey(
        key_id=uuid7(),
        key_hash=key_hash,
        fingerprint=key_hash[:16],
        kind=spec.kind,
        name=spec.name,
        owner="deployment",
        description=spec.description,
        role=spec.role,
        tier="internal",
        scopes=list(spec.scopes),
        rate_limit_requests=120,
        rate_limit_window=60,
        page_size_limit=spec.page_size_limit,
        usage_count=0,
        created_at=now,
        rotated_from_id=rotated_from_id,
    )
    db.add(row)
    return ReconciliationResult(
        name=spec.name,
        action="rotated" if rotated_from_id else "created",
        fingerprint=key_hash[:16],
    )


async def reconcile_queue_health_credential(
    db: AsyncSession,
    plaintext_api_key: str,
) -> ReconciliationResult:
    """Compatibility helper for focused queue-health reconciliation tests."""
    result = await reconcile_machine_credential(db, plaintext_api_key, QUEUE_HEALTH_SPEC)
    await db.commit()
    return result


async def reconcile_deployment_credentials(
    application_database_url: str,
    plaintext_by_env: dict[str, str],
) -> list[ReconciliationResult]:
    """Open one application-role transaction and reconcile deployment credentials."""
    url = validate_application_database_url(application_database_url)
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            results = [
                await reconcile_machine_credential(db, plaintext_by_env[spec.env_name], spec)
                for spec in DEPLOYMENT_CREDENTIAL_SPECS
            ]
            await db.commit()
            return results
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--application-database-url",
        default=os.getenv("APPLICATION_DATABASE_URL"),
        help="application connection URL; defaults to APPLICATION_DATABASE_URL",
    )
    parser.add_argument(
        "--queue-health-api-key",
        default=os.getenv("FINDB_QUEUE_HEALTH_ADMIN_API_KEY"),
        help="queue-health key; defaults to FINDB_QUEUE_HEALTH_ADMIN_API_KEY",
    )
    parser.add_argument(
        "--lookup-serve-api-key",
        default=os.getenv("FINDB_LOOKUP_SERVE_API_KEY"),
        help="lookup Serve key; defaults to FINDB_LOOKUP_SERVE_API_KEY",
    )
    parser.add_argument(
        "--static-cache-serve-api-key",
        default=os.getenv("FINDB_STATIC_CACHE_SERVE_API_KEY"),
        help="static-cache Serve key; defaults to FINDB_STATIC_CACHE_SERVE_API_KEY",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plaintext_by_env = {
        "FINDB_QUEUE_HEALTH_ADMIN_API_KEY": args.queue_health_api_key,
        "FINDB_LOOKUP_SERVE_API_KEY": args.lookup_serve_api_key,
        "FINDB_STATIC_CACHE_SERVE_API_KEY": args.static_cache_serve_api_key,
    }
    if not args.application_database_url or not all(plaintext_by_env.values()):
        print("deployment_credentials=failed reason=credential_input_missing", file=sys.stderr)
        return 1
    try:
        results = asyncio.run(
            reconcile_deployment_credentials(
                args.application_database_url,
                plaintext_by_env,
            )
        )
    except (DeploymentCredentialError, SQLAlchemyError, OSError):
        print("deployment_credentials=failed reason=reconciliation_failed", file=sys.stderr)
        return 1
    summary = ",".join(f"{result.name}:{result.action}:{result.fingerprint}" for result in results)
    print(f"deployment_credentials=ready credentials={summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
