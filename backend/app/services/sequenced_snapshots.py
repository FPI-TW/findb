"""Latest-attempt grouping for sequenced minute snapshot deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import IngestionRun


@dataclass(frozen=True)
class SequencedSnapshotGroup:
    """The latest-attempt status of one coherent snapshot/update group."""

    data_date: date
    snapshot_id: str
    daily_update_id: str
    sequence_count: int | None
    latest_attempt_at: datetime
    attempt_count: int
    complete: bool
    failed: bool


async def list_sequenced_snapshot_groups(
    db: AsyncSession,
    *,
    dataset_key: str,
    source: str,
    schema_id: str,
    schema_version: int,
    data_date: date | None = None,
) -> list[SequencedSnapshotGroup]:
    """List coherent groups, reducing retries to their latest sequence attempt.

    A retry is partitioned by sequence identity and ordered by ``created_at``
    plus ``run_id``.  Consequently, an old completed attempt cannot hide a
    later failed attempt, while a later completed retry restores completeness.
    """
    criteria = [
        IngestionRun.dataset_key == dataset_key,
        IngestionRun.source == source,
        IngestionRun.schema_id == schema_id,
        IngestionRun.schema_version == schema_version,
        IngestionRun.delivery_mode == "sequenced_snapshot",
        IngestionRun.is_rerun.is_(False),
        IngestionRun.batch_data_date.is_not(None),
        IngestionRun.snapshot_id.is_not(None),
        IngestionRun.daily_update_id.is_not(None),
        IngestionRun.sequence.is_not(None),
        IngestionRun.sequence_count.is_not(None),
    ]
    if data_date is not None:
        criteria.append(IngestionRun.batch_data_date == data_date)

    latest_attempt = (
        select(
            IngestionRun.run_id.label("run_id"),
            IngestionRun.batch_data_date.label("data_date"),
            IngestionRun.snapshot_id.label("snapshot_id"),
            IngestionRun.daily_update_id.label("daily_update_id"),
            IngestionRun.sequence.label("sequence"),
            IngestionRun.sequence_count.label("sequence_count"),
            IngestionRun.status.label("status"),
            IngestionRun.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=(
                    IngestionRun.batch_data_date,
                    IngestionRun.snapshot_id,
                    IngestionRun.daily_update_id,
                    IngestionRun.sequence,
                ),
                order_by=(IngestionRun.created_at.desc(), IngestionRun.run_id.desc()),
            )
            .label("attempt_rank"),
        )
        .where(*criteria)
        .subquery("latest_sequence_attempt")
    )
    grouped = (
        select(
            latest_attempt.c.data_date,
            latest_attempt.c.snapshot_id,
            latest_attempt.c.daily_update_id,
            func.min(latest_attempt.c.sequence_count).label("sequence_count"),
            func.min(latest_attempt.c.sequence).label("minimum_sequence"),
            func.max(latest_attempt.c.sequence).label("maximum_sequence"),
            func.count(latest_attempt.c.sequence).label("attempt_count"),
            func.count(latest_attempt.c.sequence.distinct()).label("distinct_sequences"),
            func.count(latest_attempt.c.sequence_count.distinct()).label(
                "distinct_sequence_counts"
            ),
            func.bool_and(latest_attempt.c.status == "completed").label("all_completed"),
            func.bool_or(latest_attempt.c.status == "failed").label("any_failed"),
            func.max(latest_attempt.c.created_at).label("latest_attempt_at"),
        )
        .where(latest_attempt.c.attempt_rank == 1)
        .group_by(
            latest_attempt.c.data_date,
            latest_attempt.c.snapshot_id,
            latest_attempt.c.daily_update_id,
        )
        .order_by(
            latest_attempt.c.data_date.desc(),
            func.max(latest_attempt.c.created_at).desc(),
        )
    )
    rows = (await db.execute(grouped)).mappings().all()
    groups: list[SequencedSnapshotGroup] = []
    for row in rows:
        sequence_count = _as_int(row["sequence_count"])
        attempt_count = _as_int(row["attempt_count"]) or 0
        distinct_sequences = _as_int(row["distinct_sequences"]) or 0
        distinct_counts = _as_int(row["distinct_sequence_counts"]) or 0
        all_completed = bool(row["all_completed"])
        complete = bool(
            all_completed
            and sequence_count is not None
            and _as_int(row["minimum_sequence"]) == 1
            and _as_int(row["maximum_sequence"]) == sequence_count
            and attempt_count == sequence_count
            and distinct_sequences == sequence_count
            and distinct_counts == 1
        )
        groups.append(
            SequencedSnapshotGroup(
                data_date=row["data_date"],
                snapshot_id=row["snapshot_id"],
                daily_update_id=row["daily_update_id"],
                sequence_count=sequence_count,
                latest_attempt_at=row["latest_attempt_at"],
                attempt_count=attempt_count,
                complete=complete,
                failed=bool(row["any_failed"]),
            )
        )
    return groups


def _as_int(value: Any) -> int | None:
    return int(value) if value is not None else None
