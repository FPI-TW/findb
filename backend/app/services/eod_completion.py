"""Validate per-member EOD completion using this work's permanent member evidence.

Every work freezes its feed's current per-member EOD contract: a provider's
``market_eod`` or an exchange's ``exchange_daily_quote``. Historical cohorts
(dated or current release) may additionally prove a no-trade member by its raw
observation, or report a member whose provider returned no series, or that every
attempt failed to fetch, as missing; such a date settles partial. Members the
provider cannot supply are unavailable and never make a date partial: a mapping
gap, and in a current-release cohort a member no release in effect on the date
listed yet (#593).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.provider_work import ProviderWork
from app.models.registry import (
    DailyDeliveryMember,
    DeliveryMemberEvidence,
    DQIssue,
    IngestionRun,
    UniverseMember,
    UniverseRelease,
)
from app.schemas.eod_completion import EODCompletionEvidence

# Contracts whose rows are one member's day each, settled member by member. An
# exchange's institutional table is its board's one member of the day.
MEMBER_EOD_SCHEMAS = frozenset(
    {"market_eod", "exchange_daily_quote", "institutional_flow_summary", "institutional_flow_stock"}
)
# Row-level DQ errors: the member's row was unusable, the delivery itself sound.
ROW_DQ_ERRORS = frozenset(
    {
        "OHLC_HIGH_CHECK",
        "OHLC_LOW_CHECK",
        "DUPLICATE_KEY",
        "DELISTED_NO_TRADE_DATE",
        "DELISTED_NO_DATE",
        "DELISTED_AFTER_DATE",
        "QUOTE_HIGH_BELOW_LOW",
        "QUOTE_TURNOVER_WITHOUT_VOLUME",
        # The whole table broke a printed relation: its board member is invalid.
        "INSTITUTIONAL_TABLE_REJECTED",
    }
)


def eod_contract(work: ProviderWork) -> dict | None:
    """The feed's current per-member EOD contract as frozen; None for other feeds."""
    from app.services.provider_metadata import catalog_feed

    feed = catalog_feed(work.provider, work.dataset_key)
    if feed is None or feed.contract.schema_id not in MEMBER_EOD_SCHEMAS:
        return None
    return {"schema_id": feed.contract.schema_id, "schema_version": feed.contract.current_version}


def _historical_cohort(work: ProviderWork) -> str | None:
    from app.services.provider_metadata import historical_cohort

    return historical_cohort(work.provider, work.dataset_key) if work.kind == "historical" else None


def _scoped_raw(work: ProviderWork, member) -> bool:
    return member.raw is not None and f"/{work.provider}/{work.dataset_key}/" in member.raw.ref


async def not_yet_listed(
    db: AsyncSession, *, provider: str, dataset_key: str, trade_date, keys: set[str]
) -> set[str]:
    """Members that no release published on or before the date listed, when one applies.

    A current-release cohort can hold a listing that started after the date. Before
    the feed's first release nothing is known about the date, so nothing is excluded.
    """
    if not keys:
        return set()
    dated = (
        UniverseRelease.dataset_key == dataset_key,
        UniverseRelease.provider == provider,
        UniverseRelease.status == "published",
        UniverseRelease.effective_date <= trade_date,
    )
    if await db.scalar(select(UniverseRelease.release_id).where(*dated).limit(1)) is None:
        return set()
    listed = set(
        await db.scalars(
            select(UniverseMember.member_key)
            .join(UniverseRelease, UniverseRelease.release_id == UniverseMember.release_id)
            .where(*dated, UniverseMember.member_key.in_(sorted(keys)))
            .distinct()
        )
    )
    return keys - listed


async def unavailable_members(
    db: AsyncSession,
    *,
    provider: str,
    dataset_key: str,
    evidence: EODCompletionEvidence,
    current_release: bool,
) -> dict[str, str]:
    """Members the provider cannot supply for the date, with the reason."""
    unavailable = {
        row.member_key: "mapping_gap" for row in evidence.members if row.outcome == "mapping_gap"
    }
    if current_release:
        missing = {row.member_key for row in evidence.members if row.outcome == "missing"}
        for key in await not_yet_listed(
            db,
            provider=provider,
            dataset_key=dataset_key,
            trade_date=evidence.trade_date,
            keys=missing,
        ):
            unavailable[key] = "not_yet_trading"
    return unavailable


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
    contract = eod_contract(work)
    # A historical release cohort (dated or current) may prove no-trade by its scoped
    # raw, and may leave a member it could not fetch as a gap: its date then settles
    # partial instead of holding every later request of the provider.
    cohort = _historical_cohort(work) in {"dated_release", "current_release"}
    if (
        work.kind not in {"daily", "historical"}
        or contract is None
        or manifest.get("contract") != contract
        or evidence.schema_id != contract["schema_id"]
        or evidence.schema_version != contract["schema_version"]
        or evidence.trade_date != work.trade_date
        or any(
            run.source != work.provider
            or run.dataset_key != work.dataset_key
            or run.batch_data_date != work.trade_date
            or run.schema_id != contract["schema_id"]
            or run.schema_version != contract["schema_version"]
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
    if any(issue.issue_type not in ROW_DQ_ERRORS for issue in issues):
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
    unavailable = await unavailable_members(
        db,
        provider=work.provider,
        dataset_key=work.dataset_key,
        evidence=evidence,
        current_release=_historical_cohort(work) == "current_release",
    )
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
        elif member.outcome == "invalid" and key in invalid:
            gaps.add(key)
        elif cohort and member.outcome == "missing" and member.raw is None:
            # No series was served, or every attempt failed; the member stays a gap.
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
    gaps -= set(unavailable)
    # A Fetcher that cannot tell a not-yet-listed member, or one released before
    # mapping gaps became unavailable, may still report such a date partial.
    if outcome == "completed" and gaps or outcome == "partial" and not (gaps or unavailable):
        raise ValueError("partial requires gaps; completed requires full coverage")
    return {
        "eod": evidence.model_dump(mode="json"),
        "data": sorted(written),
        "no_data": sorted(no_data - written),
        "gaps": sorted(gaps),
        "unavailable": dict(sorted(unavailable.items())),
        # Served symbols written outside the frozen list; never part of expected.
        "unlisted": sorted(unlisted),
    }
