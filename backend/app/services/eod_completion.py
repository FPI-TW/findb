"""Validate per-member EOD completion using this work's permanent member evidence.

Every work freezes its feed's current EOD contract. Historical dated-release
cohorts may additionally prove a no-trade member by its raw observation, or
report a member whose provider returned no series as missing.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_work import ProviderWork
from app.models.registry import DailyDeliveryMember, DeliveryMemberEvidence, DQIssue, IngestionRun
from app.schemas.eod_completion import EODCompletionEvidence


def eod_contract_version(work: ProviderWork) -> int | None:
    """The feed's current market_eod version; None for any other feed's work."""
    from app.services.provider_metadata import catalog_feed

    feed = catalog_feed(work.provider, work.dataset_key)
    if feed is None or feed.contract.schema_id != "market_eod":
        return None
    return feed.contract.current_version


def dated_release_work(work: ProviderWork) -> bool:
    """Historical work of a feed whose dates each freeze their own Full release."""
    from app.services.provider_metadata import historical_cohort

    return (
        work.kind == "historical"
        and historical_cohort(work.provider, work.dataset_key) == "dated_release"
    )


def _scoped_raw(work: ProviderWork, member) -> bool:
    return member.raw is not None and f"/{work.provider}/{work.dataset_key}/" in member.raw.ref


async def _outside_runs(
    db: AsyncSession, work: ProviderWork, runs: list[IngestionRun], expected: set[str]
) -> set[UUID]:
    """Runs whose prepared body names no frozen member: unlisted or supplement rows."""
    from app.models.provider_work import ProviderWorkReceipt
    from app.services.provider_work import prepared_descriptors

    if not runs:
        return set()
    rows = await db.execute(
        select(ProviderWorkReceipt.run_id, ProviderWorkReceipt.member_key).where(
            ProviderWorkReceipt.work_id == work.work_id,
            ProviderWorkReceipt.run_id.in_([run.run_id for run in runs]),
        )
    )
    keys: dict[UUID, str] = {run_id: key for run_id, key in rows}
    descriptors = await prepared_descriptors(db, work.work_id)
    return {
        run_id
        for run_id, key in keys.items()
        if (descriptor := descriptors.get(key)) is not None
        and descriptor.get("canonical_members")
        and not set(descriptor["canonical_members"]) & expected
    }


async def validate_eod_completion(
    db: AsyncSession,
    work: ProviderWork,
    runs: list[IngestionRun],
    evidence: EODCompletionEvidence,
    *,
    outcome: str,
) -> dict:
    """No run counts, pre-existing canonical prices, or caller data claims prove writes."""
    manifest = work.manifest or {}
    version = eod_contract_version(work)
    cohort = dated_release_work(work)
    if (
        work.kind not in {"daily", "historical"}
        or manifest.get("contract") != {"schema_id": "market_eod", "schema_version": version}
        or evidence.schema_version != version
        or evidence.trade_date != work.trade_date
        or any(
            run.source != work.provider
            or run.dataset_key != work.dataset_key
            or run.batch_data_date != work.trade_date
            or run.schema_id != "market_eod"
            or run.schema_version != version
            for run in runs
        )
    ):
        raise ValueError(
            "partial completion is restricted to terminal EOD v2 daily/historical work"
        )
    expected = {row["key"]: row for row in manifest.get("members", [])}
    # A run carrying only unlisted or supplement rows never decides the members:
    # it may fail or raise any DQ error, and its writes are still recorded.
    outside = await _outside_runs(db, work, runs, set(expected))
    member_runs = [run for run in runs if run.run_id not in outside]
    if any(run.status not in {"completed", "completed_with_errors"} for run in member_runs):
        raise ValueError(
            "partial completion is restricted to terminal EOD v2 daily/historical work"
        )
    if not expected or {row.member_key for row in evidence.members} != set(expected):
        raise ValueError("EOD completion must account for every frozen member exactly once")
    written = set(
        await db.scalars(
            select(DeliveryMemberEvidence.member_key).where(
                DeliveryMemberEvidence.run_id.in_([run.run_id for run in runs])
            )
        )
    )
    unlisted = written - set(expected)
    if unlisted:
        from app.services.provider_metadata import delivers_unlisted
        from app.services.provider_work import supplementary_keys

        # Admission already bound each served symbol as unlisted or as a requested
        # later-release member; a list published since then must not strand them.
        if not (
            unlisted <= supplementary_keys(work)
            or delivers_unlisted(work.provider, work.dataset_key)
        ):
            raise ValueError("EOD write evidence exceeds the frozen scope")
    written -= unlisted
    issues = list(
        await db.scalars(
            select(DQIssue).where(
                DQIssue.run_id.in_([run.run_id for run in member_runs]),
                DQIssue.severity == "error",
            )
        )
    )
    if any(
        issue.issue_type
        not in {
            "OHLC_HIGH_CHECK",
            "OHLC_LOW_CHECK",
            "DUPLICATE_KEY",
            "DELISTED_NO_TRADE_DATE",
            "DELISTED_NO_DATE",
            "DELISTED_AFTER_DATE",
        }
        for issue in issues
    ):
        raise ValueError("non-data normalization failures cannot settle partial")
    invalid = {
        str((issue.raw_data or {}).get("symbol", "")).strip().upper()
        for issue in issues
        if issue.trade_date and issue.trade_date.date() == work.trade_date
    }
    no_data: set[str] = set()
    if work.kind == "daily" and manifest.get("plan_id"):
        no_data = set(
            await db.scalars(
                select(DailyDeliveryMember.member_key).where(
                    DailyDeliveryMember.plan_id == UUID(manifest["plan_id"]),
                    DailyDeliveryMember.outcome == "no_data",
                    DailyDeliveryMember.outcome_evidence.is_not(None),
                )
            )
        )
    gaps: set[str] = set()
    for member in evidence.members:
        key = member.member_key
        if key in written:
            if member.outcome != "data":
                raise ValueError("actual EOD writes must be reported as data")
        elif member.outcome == "data":
            raise ValueError("EOD data claim lacks this work's actual-write evidence")
        elif member.outcome == "no_data":
            if key not in no_data and not (cohort and _scoped_raw(work, member)):
                raise ValueError("EOD no-data claim lacks durable scoped evidence")
            no_data.add(key)
        elif member.outcome == "mapping_gap":
            if expected[key].get("mapping_status") != "gap":
                raise ValueError("EOD mapping gap differs from frozen universe")
            gaps.add(key)
        elif member.outcome == "invalid" and key in invalid:
            gaps.add(key)
        elif cohort and member.outcome == "missing" and member.raw is None:
            # The provider served no series for the member; it stays a Full plan gap.
            gaps.add(key)
        elif _scoped_raw(work, member) and (
            member.outcome == "invalid"
            and member.reason == "row_structure_invalid"
            or member.outcome == "missing"
            and member.reason == "no_source_row"
        ):
            gaps.add(key)
        else:
            raise ValueError("EOD gap claim lacks matching raw or normalization evidence")
    if (outcome == "partial") != bool(gaps):
        raise ValueError("partial requires gaps; completed requires full coverage")
    return {
        "eod": evidence.model_dump(mode="json"),
        "data": sorted(written),
        "no_data": sorted(no_data - written),
        "gaps": sorted(gaps),
        # Served symbols written outside the frozen list; never part of expected.
        "unlisted": sorted(unlisted),
    }
