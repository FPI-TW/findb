"""Regression coverage for the canonical Source credential scope repair."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PRIOR_REVISION = "e5f6a7b8c9d0"
REPAIR_REVISION = "f1a2b3c4d5e6"


async def _run_alembic(database_url: str, revision: str, *, command: str = "upgrade") -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", command, revision],
        cwd=BACKEND_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_scope_repair_upgrade_is_exact_and_downgrade_is_conservative() -> None:
    """Only active canonical empty scopes change, and rollback never revokes them."""

    now = datetime.now(timezone.utc).replace(microsecond=0)
    rows = [
        ("twelve-active-empty", "twelve_data", [], None, None, ["us_equity_eod"]),
        ("finlab-active-empty", "finlab", [], None, None, ["tw_equity_eod"]),
        ("twelve-revoked", "twelve_data", [], now, None, []),
        ("finlab-expired", "finlab", [], None, now - timedelta(days=1), []),
        ("unknown-empty", "unknown_provider", [], None, None, []),
        ("shioaji-empty", "shioaji", [], None, None, []),
        ("twelve-null", "twelve_data", None, None, None, None),
        (
            "finlab-object",
            "finlab",
            {"dataset": "tw_equity_eod"},
            None,
            None,
            {"dataset": "tw_equity_eod"},
        ),
        ("twelve-scalar", "twelve_data", "us_equity_eod", None, None, "us_equity_eod"),
        ("finlab-nonempty", "finlab", ["tw_equity_eod"], None, None, ["tw_equity_eod"]),
        ("twelve-mixed-case", "Twelve_Data", [], None, None, []),
        ("finlab-spaced", "finlab ", [], None, None, []),
    ]

    async with get_active_migration_database_factory().clone(
        PRIOR_REVISION, "findb_scope_repair"
    ) as (database_url, target_engine):
        async with target_engine.begin() as connection:
            for index, (name, source_name, scope, revoked_at, expires_at, _expected) in enumerate(
                rows,
                start=1,
            ):
                await connection.execute(
                    text(
                        """
                        INSERT INTO source_client (
                            client_id, name, owner, description, source_name, key_hash,
                            fingerprint, allowed_datasets, rate_limit_requests,
                            rate_limit_window, created_at, updated_at, revoked_at,
                            expires_at, usage_count
                        ) VALUES (
                            :client_id, :name, :owner, :description, :source_name, :key_hash,
                            :fingerprint, CAST(:scope AS jsonb), :rate_limit_requests,
                            :rate_limit_window, :created_at, :updated_at, :revoked_at,
                            :expires_at, :usage_count
                        )
                        """
                    ),
                    {
                        "client_id": uuid4(),
                        "name": name,
                        "owner": "migration-test-owner",
                        "description": "preserve operator description",
                        "source_name": source_name,
                        "key_hash": f"{index:064x}",
                        "fingerprint": f"{index:024x}",
                        "scope": None if scope is None else json.dumps(scope),
                        "rate_limit_requests": 100 + index,
                        "rate_limit_window": 60 + index,
                        "created_at": now - timedelta(days=index),
                        "updated_at": now - timedelta(hours=index),
                        "revoked_at": revoked_at,
                        "expires_at": expires_at,
                        "usage_count": index,
                    },
                )

        query = text(
            """
            SELECT name, owner, description, source_name, key_hash, fingerprint,
                   allowed_datasets, rate_limit_requests, rate_limit_window,
                   created_at, updated_at, revoked_at, expires_at, usage_count
            FROM source_client
            ORDER BY name
            """
        )
        async with target_engine.connect() as connection:
            before = {
                row["name"]: dict(row) for row in (await connection.execute(query)).mappings()
            }

        await _run_alembic(database_url, REPAIR_REVISION)

        async with target_engine.connect() as connection:
            after_upgrade = {
                row["name"]: dict(row) for row in (await connection.execute(query)).mappings()
            }

        expected_scopes = {
            name: expected for name, _source, _scope, _revoked, _expires, expected in rows
        }
        for name, previous in before.items():
            updated = after_upgrade[name]
            for field, value in previous.items():
                if field != "allowed_datasets":
                    assert updated[field] == value, (name, field)
            assert updated["allowed_datasets"] == expected_scopes[name]

        await _run_alembic(database_url, PRIOR_REVISION, command="downgrade")
        async with target_engine.connect() as connection:
            after_downgrade = {
                row["name"]: dict(row) for row in (await connection.execute(query)).mappings()
            }
        assert after_downgrade == after_upgrade
