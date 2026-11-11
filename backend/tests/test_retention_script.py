"""PostgreSQL retention tooling only clears bytes no recovery path can replay (#494)."""

from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.config import Settings
from app.models.canonical import CalendarImportBatch
from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkPrepared,
    ProviderWorkReceipt,
)
from app.models.registry import AdminSession, AdminUser, DatasetRegistry, IngestionRun
from app.services import provider_work as queue
from app.utils import utc_now, uuid7
from scripts import retention

OLD = timedelta(days=40)


async def _work(db, key, *, state="completed", age=OLD, receipts=None, attempt="settled"):
    """One work with two prepared deliveries; ``receipts`` maps key -> run status."""
    receipts = {"m1": "completed", "m2": "completed"} if receipts is None else receipts
    work = ProviderWork(
        environment=queue.environment(),
        provider="factset",
        dataset_key="hk_equity_eod",
        kind="daily",
        stable_key=key,
        state=state,
        completed_at=utc_now() - age if state in retention.TERMINAL_WORK else None,
    )
    db.add(work)
    await db.flush()
    row = ProviderWorkAttempt(work_id=work.work_id, state=attempt)
    db.add(row)
    await db.flush()
    for member in ("m1", "m2"):
        db.add(
            ProviderWorkPrepared(
                work_id=work.work_id,
                delivery_key=member,
                descriptor={"sha256": f"{key}-{member}"},
                body=f'{{"member":"{member}"}}',
            )
        )
        if member in receipts:
            run = IngestionRun(
                run_id=uuid7(),
                dataset_key="hk_equity_eod",
                source="factset",
                status=receipts[member],
            )
            db.add(run)
            await db.flush()
            db.add(
                ProviderWorkReceipt(
                    run_id=run.run_id,
                    work_id=work.work_id,
                    attempt_id=row.attempt_id,
                    member_key=member,
                    payload_sha256="0" * 64,
                )
            )
    await db.flush()
    return work


async def _bodies(db) -> dict[str, int]:
    rows = await db.execute(
        select(ProviderWork.stable_key, func.count(ProviderWorkPrepared.body))
        .join(ProviderWorkPrepared, ProviderWorkPrepared.work_id == ProviderWork.work_id)
        .group_by(ProviderWork.stable_key)
    )
    return dict(rows.all())


@pytest.mark.asyncio
async def test_prepared_bodies_clear_only_for_settled_fully_received_old_work(test_session):
    test_session.add(
        DatasetRegistry(dataset_key="hk_equity_eod", name="HK", market="HK", asset_class="equity")
    )
    await test_session.flush()
    await _work(test_session, "eligible")
    await _work(test_session, "partial-old", state="partial")
    await _work(test_session, "recent", age=timedelta(days=2))
    await _work(test_session, "still-open", state="queued", attempt="open")
    await _work(test_session, "waived", state="failed", receipts={"m1": "completed"})
    await _work(test_session, "run-pending", receipts={"m1": "completed", "m2": "queued"})
    await _work(test_session, "attempt-open", attempt="draining")
    # A dry run rolls back its session, so the fixtures must already be committed.
    await test_session.commit()
    before = await _bodies(test_session)

    report = await retention.inspect(test_session)
    eligible = [row for row in report["provider_work_prepared"] if row["recovery"] == "eligible"]
    assert sum(row["works"] for row in eligible) == 3  # includes "recent"; age is not applied
    assert sum(row["body_bytes"] for row in report["provider_work_prepared"]) > 0

    dry = await retention.cleanup(
        test_session, prepared_body_days=30, import_batch_days=None, session_days=None, apply=False
    )
    assert dry["prepared_bodies"]["rows"] == 4
    assert await _bodies(test_session) == before

    applied = await retention.cleanup(
        test_session, prepared_body_days=30, import_batch_days=None, session_days=None, apply=True
    )
    assert applied["prepared_bodies"]["rows"] == 4
    assert await _bodies(test_session) == {**before, "eligible": 0, "partial-old": 0}
    # Descriptors and receipts are retained as permanent evidence.
    assert await test_session.scalar(select(func.count()).select_from(ProviderWorkPrepared)) == 14
    assert await test_session.scalar(select(func.count()).select_from(ProviderWorkReceipt)) == 13

    again = await retention.cleanup(
        test_session, prepared_body_days=30, import_batch_days=None, session_days=None, apply=True
    )
    assert again["prepared_bodies"]["rows"] == 0


@pytest.mark.asyncio
async def test_only_expired_previews_and_long_ended_sessions_are_removed(test_session):
    now = utc_now()
    for status, expires in (
        ("previewed", now - timedelta(days=10)),
        ("previewed", now - timedelta(hours=1)),
        ("applied", now - timedelta(days=10)),
    ):
        test_session.add(
            CalendarImportBatch(
                market="TW",
                year=2026,
                input_format="csv",
                candidate_rows=[{"trade_date": "2026-01-01"}],
                summary={},
                status=status,
                expires_at=expires,
            )
        )
    user = AdminUser(
        username="retention-owner",
        display_name="Retention",
        role="owner",
        password_hash="x",
    )
    test_session.add(user)
    await test_session.flush()
    for expires, revoked in (
        (now - timedelta(days=100), None),
        (now + timedelta(hours=1), now - timedelta(days=100)),
        (now - timedelta(days=1), None),
        (now + timedelta(hours=1), None),
    ):
        test_session.add(
            AdminSession(
                user_id=user.user_id,
                token_hash=uuid7().hex,
                expires_at=expires,
                revoked_at=revoked,
            )
        )
    await test_session.commit()

    report = await retention.inspect(test_session)
    assert {row["state"]: row["rows"] for row in report["admin_session"]} == {
        "active": 1,
        "expired": 2,
        "revoked": 1,
    }

    result = await retention.cleanup(
        test_session, prepared_body_days=None, import_batch_days=7, session_days=90, apply=True
    )
    assert result == {
        "applied": True,
        "expired_import_previews": {"rows": 1},
        "ended_admin_sessions": {"rows": 2},
    }
    statuses = (await test_session.scalars(select(CalendarImportBatch.status))).all()
    assert sorted(statuses) == ["applied", "previewed"]
    assert await test_session.scalar(select(func.count()).select_from(AdminSession)) == 2


def test_cleanup_requires_an_explicit_positive_age(capsys):
    for argv in (["cleanup"], ["cleanup", "--apply"], ["cleanup", "--session-days", "0"]):
        with pytest.raises(SystemExit) as exited:
            retention.main(argv)
        assert exited.value.code == 2
    assert "explicit --*-days" in capsys.readouterr().err


def test_scheduled_ages_default_to_the_owner_decision_and_must_be_positive():
    settings = Settings(_env_file=None)
    assert (
        settings.PREPARED_BODY_RETENTION_DAYS,
        settings.CALENDAR_IMPORT_PREVIEW_RETENTION_DAYS,
        settings.ADMIN_SESSION_RETENTION_DAYS,
    ) == (7, 1, 30)
    for field in (
        "PREPARED_BODY_RETENTION_DAYS",
        "CALENDAR_IMPORT_PREVIEW_RETENTION_DAYS",
        "ADMIN_SESSION_RETENTION_DAYS",
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: 0})


@pytest.mark.asyncio
async def test_scheduled_cleanup_applies_configured_ages(test_session, test_engine):
    now = utc_now()
    test_session.add(
        DatasetRegistry(dataset_key="hk_equity_eod", name="HK", market="HK", asset_class="equity")
    )
    await test_session.flush()
    await _work(test_session, "past-week", age=timedelta(days=8))
    await _work(test_session, "within-week", age=timedelta(days=6))
    for expires in (now - timedelta(days=2), now - timedelta(hours=12)):
        test_session.add(
            CalendarImportBatch(
                market="TW",
                year=2026,
                input_format="csv",
                candidate_rows=[],
                summary={},
                status="previewed",
                expires_at=expires,
            )
        )
    user = AdminUser(
        username="retention-scheduled",
        display_name="Retention",
        role="owner",
        password_hash="x",
    )
    test_session.add(user)
    await test_session.flush()
    for ended in (now - timedelta(days=31), now - timedelta(days=29)):
        test_session.add(
            AdminSession(user_id=user.user_id, token_hash=uuid7().hex, expires_at=ended)
        )
    await test_session.commit()

    result = await retention.scheduled_cleanup(
        database_url=test_engine.url.render_as_string(hide_password=False)
    )
    test_session.expire_all()

    assert result["applied"] is True
    assert result["prepared_bodies"]["rows"] == 2
    assert result["expired_import_previews"] == {"rows": 1}
    assert result["ended_admin_sessions"] == {"rows": 1}
    assert await _bodies(test_session) == {"past-week": 0, "within-week": 2}
    assert await test_session.scalar(select(func.count()).select_from(CalendarImportBatch)) == 1
    assert await test_session.scalar(select(func.count()).select_from(AdminSession)) == 1
