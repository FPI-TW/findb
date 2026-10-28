"""Read-only Admin queries for universe releases and member differences."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import UniverseMember, UniverseRelease

_MEMBER_FIELDS = (
    "member_key",
    "symbol",
    "provider_symbol",
    "exchange",
    "currency",
    "asset_class",
    "classification",
    "contract_code",
    "product_code",
    "contract_month",
    "session",
    "mapping_status",
)


def present_universe_release(
    release: UniverseRelease,
    *,
    comparison_release_id: UUID | None = None,
) -> dict[str, Any]:
    return {
        "release_id": release.release_id,
        "dataset_key": release.dataset_key,
        "provider": release.provider,
        "effective_date": release.effective_date,
        "observed_at": release.observed_at,
        "status": release.status,
        "member_count": release.member_count,
        "member_sha256": release.member_sha256,
        "semantic_member_sha256": release.semantic_member_sha256,
        "change_count": release.change_count,
        "change_ratio": float(release.change_ratio),
        "created_at": release.created_at,
        "published_at": release.published_at,
        "published_by": release.published_by,
        "approval_note": release.approval_note,
        "comparison_release_id": comparison_release_id,
    }


async def _comparison_release(db: AsyncSession, release: UniverseRelease) -> UniverseRelease | None:
    return (
        await db.scalars(
            select(UniverseRelease)
            .where(
                UniverseRelease.dataset_key == release.dataset_key,
                UniverseRelease.provider == release.provider,
                UniverseRelease.status == "published",
                UniverseRelease.release_id != release.release_id,
                UniverseRelease.effective_date <= release.effective_date,
            )
            .order_by(
                UniverseRelease.effective_date.desc(),
                UniverseRelease.created_at.desc(),
                UniverseRelease.release_id.desc(),
            )
            .limit(1)
        )
    ).one_or_none()


async def list_universe_releases(
    db: AsyncSession,
    *,
    status: str | None = None,
    dataset_key: str | None = None,
    provider: str | None = None,
    effective_date: date | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    conditions = []
    if status is not None:
        conditions.append(UniverseRelease.status == status)
    if dataset_key is not None:
        conditions.append(UniverseRelease.dataset_key == dataset_key)
    if provider is not None:
        conditions.append(UniverseRelease.provider == provider.strip().lower())
    if effective_date is not None:
        conditions.append(UniverseRelease.effective_date == effective_date)
    order = (
        UniverseRelease.effective_date.desc(),
        UniverseRelease.created_at.desc(),
        UniverseRelease.release_id.desc(),
    )
    total = int(
        await db.scalar(select(func.count()).select_from(UniverseRelease).where(*conditions)) or 0
    )
    releases = list(
        await db.scalars(
            select(UniverseRelease)
            .where(*conditions)
            .order_by(*order)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    rows = []
    for release in releases:
        comparison = await _comparison_release(db, release)
        rows.append(
            present_universe_release(
                release,
                comparison_release_id=comparison.release_id if comparison else None,
            )
        )
    return rows, total


def _present_member(member: UniverseMember) -> dict[str, Any]:
    return {field: getattr(member, field) for field in _MEMBER_FIELDS}


async def get_universe_release_diff(db: AsyncSession, release_id: UUID) -> dict[str, Any] | None:
    release = await db.get(UniverseRelease, release_id)
    if release is None:
        return None
    comparison = await _comparison_release(db, release)
    current_members = {
        member.member_key: _present_member(member)
        for member in await db.scalars(
            select(UniverseMember)
            .where(UniverseMember.release_id == release.release_id)
            .order_by(UniverseMember.member_key)
        )
    }
    previous_members = (
        {
            member.member_key: _present_member(member)
            for member in await db.scalars(
                select(UniverseMember)
                .where(UniverseMember.release_id == comparison.release_id)
                .order_by(UniverseMember.member_key)
            )
        }
        if comparison is not None
        else {}
    )
    added_keys = sorted(current_members.keys() - previous_members.keys())
    removed_keys = sorted(previous_members.keys() - current_members.keys())
    common_keys = sorted(current_members.keys() & previous_members.keys())
    changed = []
    unchanged_count = 0
    for key in common_keys:
        before = previous_members[key]
        after = current_members[key]
        changed_fields = sorted(
            field for field in _MEMBER_FIELDS if before.get(field) != after.get(field)
        )
        if changed_fields:
            changed.append(
                {
                    "member_key": key,
                    "changed_fields": changed_fields,
                    "before": before,
                    "after": after,
                }
            )
        else:
            unchanged_count += 1
    return {
        "release": present_universe_release(
            release,
            comparison_release_id=comparison.release_id if comparison else None,
        ),
        "comparison_release": (
            present_universe_release(comparison) if comparison is not None else None
        ),
        "added": [current_members[key] for key in added_keys],
        "removed": [previous_members[key] for key in removed_keys],
        "changed": changed,
        "unchanged_count": unchanged_count,
    }
