"""
Inventory and clean PostgreSQL copies that outlive their recovery purpose.

``inspect`` is read-only. ``cleanup`` is a dry run unless ``--apply`` is given,
and every category requires an explicit age in days; there is no default
retention. Cleanup never deletes provider-work rows: it only clears the body of
prepared deliveries whose work is terminal and whose every delivery has a
receipt on a terminal run, keeping descriptors, digests and receipts.
"""

import argparse
import asyncio
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, delete, exists, func, not_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.canonical import CalendarImportBatch
from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkPrepared,
    ProviderWorkReceipt,
)
from app.models.registry import AdminSession, IngestionRun
from app.services.provider_work import TERMINAL_RUNS
from app.utils import utc_now

# Work states written by settle(); only a non-terminal attempt can re-queue work.
TERMINAL_WORK = ("completed", "partial", "failed", "cancelled", "calendar_closed")
AGE_BUCKETS = ((7, "0-7d"), (30, "7-30d"), (90, "30-90d"))


def _terminal_receipt(prepared=ProviderWorkPrepared):
    return exists(
        select(ProviderWorkReceipt.run_id)
        .join(IngestionRun, IngestionRun.run_id == ProviderWorkReceipt.run_id)
        .where(
            ProviderWorkReceipt.work_id == prepared.work_id,
            ProviderWorkReceipt.member_key == prepared.delivery_key,
            IngestionRun.status.in_(TERMINAL_RUNS),
        )
    )


def _recoverable_work():
    """Work that may still replay its prepared bytes and must keep every body."""
    other = ProviderWorkPrepared.__table__.alias("other_prepared")
    return (
        ProviderWork.state.not_in(TERMINAL_WORK)
        | exists(
            select(ProviderWorkAttempt.attempt_id).where(
                ProviderWorkAttempt.work_id == ProviderWork.work_id,
                ProviderWorkAttempt.state != "settled",
            )
        )
        # An unaccepted or manually waived delivery keeps the whole work for review.
        | exists(
            select(other.c.delivery_key).where(
                other.c.work_id == ProviderWork.work_id, not_(_terminal_receipt(other.c))
            )
        )
    )


def _eligible_prepared(cutoff):
    return and_(
        ProviderWorkPrepared.body.is_not(None),
        ProviderWork.work_id == ProviderWorkPrepared.work_id,
        ProviderWork.completed_at < cutoff,
        not_(_recoverable_work()),
    )


async def inspect(db: AsyncSession) -> dict[str, Any]:
    now = utc_now()
    age_days = func.extract("epoch", now - ProviderWork.completed_at) / 86400
    bucket = case(
        *[(age_days < days, label) for days, label in AGE_BUCKETS],
        else_="90d+",
    )
    work_keys = (
        ProviderWork.provider,
        ProviderWork.state,
        case((ProviderWork.completed_at.is_(None), "open"), else_=bucket).label("age"),
        case((_recoverable_work(), "keep"), else_="eligible").label("recovery"),
    )
    prepared_rows = (
        await db.execute(
            select(
                *work_keys,
                func.count(func.distinct(ProviderWork.work_id)),
                func.count(),
                func.count(ProviderWorkPrepared.body),
                func.coalesce(func.sum(func.octet_length(ProviderWorkPrepared.body)), 0),
            )
            .join(ProviderWorkPrepared, ProviderWorkPrepared.work_id == ProviderWork.work_id)
            .group_by(*work_keys)
            .order_by(*work_keys)
        )
    ).all()
    expired = (CalendarImportBatch.expires_at < now).label("expired")
    batches = (
        await db.execute(
            select(
                CalendarImportBatch.status,
                expired,
                func.count(),
                func.coalesce(func.sum(func.pg_column_size(CalendarImportBatch.candidate_rows)), 0),
            )
            .group_by(CalendarImportBatch.status, expired)
            .order_by(CalendarImportBatch.status, expired)
        )
    ).all()
    session_state = case(
        (AdminSession.revoked_at.is_not(None), "revoked"),
        (AdminSession.expires_at <= now, "expired"),
        else_="active",
    ).label("state")
    sessions = (
        await db.execute(
            select(
                session_state,
                func.count(),
                func.min(func.coalesce(AdminSession.revoked_at, AdminSession.expires_at)),
            )
            .group_by(session_state)
            .order_by(session_state)
        )
    ).all()
    return {
        "inspected_at": now.isoformat(),
        "provider_work_prepared": [
            {
                "provider": provider,
                "work_state": state,
                "completed_age": age,
                "recovery": recovery,
                "works": works,
                "rows": rows,
                "rows_with_body": with_body,
                "body_bytes": int(body_bytes),
            }
            for provider, state, age, recovery, works, rows, with_body, body_bytes in prepared_rows
        ],
        "calendar_import_batch": [
            {"status": status, "expired": bool(expired), "rows": rows, "candidate_bytes": int(size)}
            for status, expired, rows, size in batches
        ],
        "admin_session": [
            {
                "state": state,
                "rows": rows,
                "oldest_end": oldest.isoformat() if oldest else None,
            }
            for state, rows, oldest in sessions
        ],
    }


async def cleanup(
    db: AsyncSession,
    *,
    prepared_body_days: int | None,
    import_batch_days: int | None,
    session_days: int | None,
    apply: bool,
) -> dict[str, Any]:
    """Count (and with ``apply`` clear) only the categories given an explicit age."""
    now = utc_now()
    result: dict[str, Any] = {"applied": apply}
    if prepared_body_days is not None:
        eligible = _eligible_prepared(now - timedelta(days=prepared_body_days))
        count, size = (
            await db.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(func.octet_length(ProviderWorkPrepared.body)), 0),
                ).where(eligible)
            )
        ).one()
        result["prepared_bodies"] = {"rows": count, "body_bytes": int(size)}
        if apply and count:
            await db.execute(
                update(ProviderWorkPrepared)
                .where(eligible)
                .values(body=None)
                .execution_options(synchronize_session=False)
            )
    if import_batch_days is not None:
        stale = and_(
            CalendarImportBatch.status == "previewed",
            CalendarImportBatch.expires_at < now - timedelta(days=import_batch_days),
        )
        count = await db.scalar(select(func.count()).where(stale))
        result["expired_import_previews"] = {"rows": count}
        if apply and count:
            await db.execute(delete(CalendarImportBatch).where(stale))
    if session_days is not None:
        cutoff = now - timedelta(days=session_days)
        ended = func.coalesce(AdminSession.revoked_at, AdminSession.expires_at) < cutoff
        count = await db.scalar(select(func.count()).where(ended))
        result["ended_admin_sessions"] = {"rows": count}
        if apply and count:
            await db.execute(delete(AdminSession).where(ended))
    if apply:
        await db.commit()
    else:
        await db.rollback()
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="read-only size and age inventory")
    clean = commands.add_parser("cleanup", help="dry run unless --apply")
    clean.add_argument("--prepared-body-days", type=int)
    clean.add_argument("--import-batch-days", type=int)
    clean.add_argument("--session-days", type=int)
    clean.add_argument("--apply", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        async with async_sessionmaker(engine, class_=AsyncSession)() as db:
            if args.command == "inspect":
                return await inspect(db)
            return await cleanup(
                db,
                prepared_body_days=args.prepared_body_days,
                import_batch_days=args.import_batch_days,
                session_days=args.session_days,
                apply=args.apply,
            )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "cleanup":
        days = (args.prepared_body_days, args.import_batch_days, args.session_days)
        if all(value is None for value in days):
            _parser().error("cleanup needs at least one explicit --*-days value")
        if any(value is not None and value < 1 for value in days):
            _parser().error("retention days must be at least 1")
    print(json.dumps(asyncio.run(_run(args)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
