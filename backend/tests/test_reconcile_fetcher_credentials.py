"""Tests for hashed Fetcher-facing credential reconciliation."""

from hashlib import sha256

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import APIKey, SourceClient
from scripts.reconcile_fetcher_credentials import (
    CALENDAR_CREDENTIAL_NAME,
    CALENDAR_HASH_ENV,
    SOURCE_SPECS,
    FetcherCredentialError,
    reconcile_fetcher_credentials_in_session,
)


def _hash(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def _hashes() -> dict[str, str]:
    values = {CALENDAR_HASH_ENV: _hash("calendar")}
    values.update({spec.env_name: _hash(spec.source_name) for spec in SOURCE_SPECS})
    return values


async def test_fetcher_credentials_are_created_and_idempotent(
    test_session: AsyncSession,
) -> None:
    created = await reconcile_fetcher_credentials_in_session(test_session, _hashes())
    unchanged = await reconcile_fetcher_credentials_in_session(test_session, _hashes())
    source_rows = list(
        (
            await test_session.execute(
                select(SourceClient).where(
                    SourceClient.name.in_([spec.name for spec in SOURCE_SPECS])
                )
            )
        )
        .scalars()
        .all()
    )
    calendar = (
        await test_session.execute(select(APIKey).where(APIKey.name == CALENDAR_CREDENTIAL_NAME))
    ).scalar_one()

    assert [row.action for row in created] == ["created"] * 4
    assert [row.action for row in unchanged] == ["unchanged"] * 4
    assert len(source_rows) == 3
    by_name = {row.name: row for row in source_rows}
    for spec in SOURCE_SPECS:
        assert by_name[spec.name].source_name == spec.source_name
        assert by_name[spec.name].allowed_datasets == list(spec.allowed_datasets)
        assert by_name[spec.name].key_hash == _hashes()[spec.env_name]
    assert calendar.kind == "serve"
    assert calendar.scopes == ["serve"]
    assert calendar.page_size_limit == 1000
    assert calendar.key_hash == _hashes()[CALENDAR_HASH_ENV]


async def test_fetcher_source_rotation_revokes_previous_identity(
    test_session: AsyncSession,
) -> None:
    hashes = _hashes()
    await reconcile_fetcher_credentials_in_session(test_session, hashes)
    hashes[SOURCE_SPECS[0].env_name] = _hash("twelve-data-replacement")

    results = await reconcile_fetcher_credentials_in_session(test_session, hashes)
    rows = list(
        (
            await test_session.execute(
                select(SourceClient)
                .where(SourceClient.name == SOURCE_SPECS[0].name)
                .order_by(SourceClient.created_at)
            )
        )
        .scalars()
        .all()
    )

    assert results[0].action == "rotated"
    assert len(rows) == 2
    assert rows[0].revoked_at is not None
    assert rows[1].rotated_from_id == rows[0].client_id


async def test_fetcher_credentials_reject_reused_hash(
    test_session: AsyncSession,
) -> None:
    hashes = _hashes()
    hashes[SOURCE_SPECS[1].env_name] = hashes[SOURCE_SPECS[0].env_name]

    with pytest.raises(FetcherCredentialError):
        await reconcile_fetcher_credentials_in_session(test_session, hashes)
