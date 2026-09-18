"""Reconcile Fetcher-facing DB credentials from operator-computed key hashes.

Fetcher secrets remain isolated from the FinDB instance role. A trusted
operator computes SHA-256 hashes without logging plaintext and supplies only
those hashes to this one-shot application-role command.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.registry import APIKey, SourceClient
from app.utils import utc_now, uuid7


class FetcherCredentialError(RuntimeError):
    """Raised when Fetcher credential state cannot be reconciled safely."""


def validate_application_database_url(database_url: str) -> URL:
    """Require a PostgreSQL application connection with an explicit identity."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise FetcherCredentialError("application database URL must use PostgreSQL")
    if not url.host or not url.database or not url.username:
        raise FetcherCredentialError(
            "application database URL must include host, database, and username"
        )
    return url


@dataclass(frozen=True)
class SourceCredentialSpec:
    env_name: str
    name: str
    source_name: str
    description: str
    allowed_datasets: tuple[str, ...]


SOURCE_SPECS = (
    SourceCredentialSpec(
        env_name="FETCHER_TWELVE_DATA_SOURCE_CLIENT_KEY_HASH",
        name="fetcher-twelve-data",
        source_name="twelve_data",
        description="Production Twelve Data Fetcher Source client",
        allowed_datasets=("us_equity_eod",),
    ),
    SourceCredentialSpec(
        env_name="FETCHER_FINLAB_SOURCE_CLIENT_KEY_HASH",
        name="fetcher-finlab",
        source_name="finlab",
        description="Production FinLab Fetcher Source client",
        allowed_datasets=("tw_equity_eod",),
    ),
    SourceCredentialSpec(
        env_name="FETCHER_SHIOAJI_SOURCE_CLIENT_KEY_HASH",
        name="fetcher-shioaji",
        source_name="shioaji",
        description="Production Shioaji Fetcher Source client",
        allowed_datasets=("tw_equity_minute", "tw_etf_minute"),
    ),
)
CALENDAR_HASH_ENV = "FETCHER_CALENDAR_SERVE_API_KEY_HASH"
CALENDAR_CREDENTIAL_NAME = "fetcher-calendar"


@dataclass(frozen=True)
class ReconciliationResult:
    name: str
    action: str
    fingerprint: str


def validate_key_hash(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise FetcherCredentialError("credential hash must be lowercase SHA-256")
    return value


def _is_active(row: SourceClient | APIKey, now: datetime) -> bool:
    return row.revoked_at is None and (row.expires_at is None or row.expires_at > now)


def _apply_source_policy(
    row: SourceClient,
    spec: SourceCredentialSpec,
    key_hash: str,
    now: datetime,
) -> bool:
    desired: dict[str, object] = {
        "owner": "deployment",
        "description": spec.description,
        "source_name": spec.source_name,
        "fingerprint": key_hash[:16],
        "allowed_datasets": list(spec.allowed_datasets),
        "rate_limit_requests": 120,
        "rate_limit_window": 60,
    }
    changed = False
    for field, value in desired.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    if changed:
        row.updated_at = now
    return changed


async def reconcile_source_credential(
    db: AsyncSession,
    key_hash_value: str,
    spec: SourceCredentialSpec,
    now: datetime,
) -> ReconciliationResult:
    key_hash = validate_key_hash(key_hash_value)
    rows = list(
        (
            await db.execute(
                select(SourceClient)
                .where((SourceClient.name == spec.name) | (SourceClient.key_hash == key_hash))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    hash_matches = [row for row in rows if row.key_hash == key_hash]
    active_named = [row for row in rows if row.name == spec.name and _is_active(row, now)]
    if len(hash_matches) > 1 or len(active_named) > 1:
        raise FetcherCredentialError("Fetcher Source credential state is ambiguous")
    if hash_matches:
        row = hash_matches[0]
        if row.name != spec.name or row.source_name != spec.source_name:
            raise FetcherCredentialError("Fetcher Source hash is used by another identity")
        if not _is_active(row, now) or row.expires_at is not None:
            raise FetcherCredentialError("Fetcher Source credential is revoked or expiring")
        if active_named and active_named[0].client_id != row.client_id:
            raise FetcherCredentialError("another Fetcher Source credential is active")
        changed = _apply_source_policy(row, spec, key_hash, now)
        return ReconciliationResult(spec.name, "updated" if changed else "unchanged", key_hash[:16])

    rotated_from_id = None
    if active_named:
        previous = active_named[0]
        previous.revoked_at = now
        previous.updated_at = now
        rotated_from_id = previous.client_id
    db.add(
        SourceClient(
            client_id=uuid7(),
            name=spec.name,
            owner="deployment",
            description=spec.description,
            source_name=spec.source_name,
            key_hash=key_hash,
            fingerprint=key_hash[:16],
            allowed_datasets=list(spec.allowed_datasets),
            rate_limit_requests=120,
            rate_limit_window=60,
            created_at=now,
            updated_at=now,
            rotated_from_id=rotated_from_id,
        )
    )
    return ReconciliationResult(
        spec.name, "rotated" if rotated_from_id else "created", key_hash[:16]
    )


def _apply_calendar_policy(row: APIKey, key_hash: str) -> bool:
    desired: dict[str, object] = {
        "owner": "deployment",
        "description": "Fetcher published-calendar Serve credential",
        "role": None,
        "tier": "internal",
        "fingerprint": key_hash[:16],
        "scopes": ["serve"],
        "rate_limit_requests": 120,
        "rate_limit_window": 60,
        "page_size_limit": 1000,
    }
    changed = False
    for field, value in desired.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed


async def reconcile_calendar_credential(
    db: AsyncSession,
    key_hash_value: str,
    now: datetime,
) -> ReconciliationResult:
    key_hash = validate_key_hash(key_hash_value)
    rows = list(
        (
            await db.execute(
                select(APIKey)
                .where((APIKey.name == CALENDAR_CREDENTIAL_NAME) | (APIKey.key_hash == key_hash))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    hash_matches = [row for row in rows if row.key_hash == key_hash]
    active_named = [
        row for row in rows if row.name == CALENDAR_CREDENTIAL_NAME and _is_active(row, now)
    ]
    if len(hash_matches) > 1 or len(active_named) > 1:
        raise FetcherCredentialError("Fetcher calendar credential state is ambiguous")
    if hash_matches:
        row = hash_matches[0]
        if row.name != CALENDAR_CREDENTIAL_NAME or row.kind != "serve":
            raise FetcherCredentialError("Fetcher calendar hash is used by another identity")
        if not _is_active(row, now) or row.expires_at is not None:
            raise FetcherCredentialError("Fetcher calendar credential is revoked or expiring")
        if active_named and active_named[0].key_id != row.key_id:
            raise FetcherCredentialError("another Fetcher calendar credential is active")
        changed = _apply_calendar_policy(row, key_hash)
        return ReconciliationResult(
            CALENDAR_CREDENTIAL_NAME,
            "updated" if changed else "unchanged",
            key_hash[:16],
        )

    rotated_from_id = None
    if active_named:
        previous = active_named[0]
        previous.revoked_at = now
        rotated_from_id = previous.key_id
    db.add(
        APIKey(
            key_id=uuid7(),
            key_hash=key_hash,
            fingerprint=key_hash[:16],
            kind="serve",
            name=CALENDAR_CREDENTIAL_NAME,
            owner="deployment",
            description="Fetcher published-calendar Serve credential",
            role=None,
            tier="internal",
            scopes=["serve"],
            rate_limit_requests=120,
            rate_limit_window=60,
            page_size_limit=1000,
            usage_count=0,
            created_at=now,
            rotated_from_id=rotated_from_id,
        )
    )
    return ReconciliationResult(
        CALENDAR_CREDENTIAL_NAME,
        "rotated" if rotated_from_id else "created",
        key_hash[:16],
    )


async def reconcile_fetcher_credentials_in_session(
    db: AsyncSession,
    hashes_by_env: dict[str, str],
) -> list[ReconciliationResult]:
    normalized = {name: validate_key_hash(value) for name, value in hashes_by_env.items()}
    required = {CALENDAR_HASH_ENV, *(spec.env_name for spec in SOURCE_SPECS)}
    if set(normalized) != required or len(set(normalized.values())) != len(required):
        raise FetcherCredentialError("Fetcher credential hashes are missing or reused")
    now = utc_now()
    results = [
        await reconcile_source_credential(db, normalized[spec.env_name], spec, now)
        for spec in SOURCE_SPECS
    ]
    results.append(await reconcile_calendar_credential(db, normalized[CALENDAR_HASH_ENV], now))
    await db.commit()
    return results


async def reconcile_fetcher_credentials(
    application_database_url: str,
    hashes_by_env: dict[str, str],
) -> list[ReconciliationResult]:
    url = validate_application_database_url(application_database_url)
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            return await reconcile_fetcher_credentials_in_session(db, hashes_by_env)
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-database-url", default=os.getenv("APPLICATION_DATABASE_URL"))
    for env_name in (CALENDAR_HASH_ENV, *(spec.env_name for spec in SOURCE_SPECS)):
        parser.add_argument(
            f"--{env_name.lower().replace('_', '-')}",
            dest=env_name,
            default=os.getenv(env_name),
        )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    hashes_by_env = {
        env_name: getattr(args, env_name)
        for env_name in (CALENDAR_HASH_ENV, *(spec.env_name for spec in SOURCE_SPECS))
    }
    if not args.application_database_url or not all(hashes_by_env.values()):
        print("fetcher_credentials=failed reason=input_missing", file=sys.stderr)
        return 1
    try:
        results = asyncio.run(
            reconcile_fetcher_credentials(args.application_database_url, hashes_by_env)
        )
    except (FetcherCredentialError, SQLAlchemyError, OSError):
        print("fetcher_credentials=failed reason=reconciliation_failed", file=sys.stderr)
        return 1
    summary = ",".join(f"{row.name}:{row.action}:{row.fingerprint}" for row in results)
    print(f"fetcher_credentials=ready credentials={summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
