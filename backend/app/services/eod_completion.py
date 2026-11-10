"""Validate per-member EOD completion using this work's permanent member evidence.

FactSet freezes EOD v2. Twelve Data/FinLab historical Full release cohorts freeze
their feed's v1 contract and may additionally prove a no-trade member by its raw
observation, or report a member whose provider returned no series as missing.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_work import ProviderWork
from app.models.registry import DailyDeliveryMember, DeliveryMemberEvidence, DQIssue, IngestionRun
from app.schemas.eod_completion import EODCompletionEvidence


def eod_contract_version(work: ProviderWork) -> int:
    from app.services.historical_backfill import MEMBER_COHORT_PROVIDERS

    return 1 if work.kind == "historical" and work.provider in MEMBER_COHORT_PROVIDERS else 2


def _scoped_raw(work: ProviderWork, member) -> bool:
    return member.raw is not None and f"/{work.provider}/{work.dataset_key}/" in member.raw.ref


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
    cohort = version == 1
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
            or run.status not in {"completed", "completed_with_errors"}
            for run in runs
        )
    ):
        raise ValueError(
            "partial completion is restricted to terminal EOD v2 daily/historical work"
        )
    expected = {row["key"]: row for row in manifest.get("members", [])}
    if not expected or {row.member_key for row in evidence.members} != set(expected):
        raise ValueError("EOD completion must account for every frozen member exactly once")
    written = set(
        await db.scalars(
            select(DeliveryMemberEvidence.member_key).where(
                DeliveryMemberEvidence.run_id.in_([run.run_id for run in runs])
            )
        )
    )
    if not written <= set(expected):
        raise ValueError("EOD write evidence exceeds the frozen scope")
    issues = list(
        await db.scalars(
            select(DQIssue).where(
                DQIssue.run_id.in_([run.run_id for run in runs]), DQIssue.severity == "error"
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
    }
