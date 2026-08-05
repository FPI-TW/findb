"""
Admin service for canonical data corrections.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import InstrumentStats, MarketDataEOD
from app.models.correction import CanonicalCorrection
from app.models.raw import RawMarketPayload
from app.models.registry import DQIssue, IngestionRun
from app.schemas.admin import PatchEODRequest, ResolveDQIssueRequest
from app.utils import utc_now, uuid7


class RecordNotFoundError(ValueError):
    """Raised when the target canonical record does not exist."""


class NoChangesError(ValueError):
    """Raised when a PATCH request would produce no actual changes."""


class AlreadyResolvedError(ValueError):
    """Raised when attempting to resolve an already-resolved DQ issue."""


class InvalidCorrectionError(ValueError):
    """Raised when a correction would violate blocking data-quality rules."""


@dataclass(frozen=True)
class DQIssueView:
    """Bounded, joined projection used by the Admin DQ list endpoint."""

    id: UUID
    run_id: UUID | None
    instrument_id: UUID | None
    trade_date: date | None
    issue_type: str
    severity: str
    description: str | None
    source: str | None
    provider: str | None
    dataset_key: str | None
    schema_id: str | None
    schema_version: int | None
    raw_payload_id: UUID | None
    raw_available: bool
    fetched_at: datetime | None
    request_key: str | None
    batch_data_date: date | None
    policy_detail: dict[str, Any] | None
    resolved: bool
    resolved_at: datetime | None
    created_at: datetime


_CORRECTABLE_EOD_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_ticks",
    "turnover",
}


def _mask_key(key: str) -> str:
    """Return first 4 chars + **** for display."""
    if len(key) <= 4:
        return "****"
    return key[:4] + "****"


def build_correction_actor(api_key: object) -> str:
    """Persist a non-reversible identifier instead of the raw admin API key."""
    if hasattr(api_key, "actor_type"):
        actor_type = getattr(api_key, "actor_type")
        actor_id = getattr(api_key, "actor_id", None) or getattr(api_key, "display_name")
        if actor_type == "break_glass":
            return f"key_fp:{actor_id}"
        display_name = getattr(api_key, "display_name")
        return f"{actor_type}:{actor_id}:{display_name}"[:200]
    api_key = str(api_key)
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"key_fp:{digest}"


def present_correction_actor(value: str) -> str:
    """Show fingerprints as-is while masking legacy raw API keys."""
    if value.startswith("key_fp:"):
        return value
    if value.startswith(("user:", "machine:")):
        return value
    return _mask_key(value)


def _normalize_decimal(value: Decimal) -> str:
    """Serialize Decimal to a consistent string, removing unnecessary trailing zeros."""
    normalized = value.normalize()
    # avoid scientific notation (e.g. 1E+2)
    return format(normalized, "f")


def _snapshot(obj: Any, fields: set[str]) -> dict[str, Any]:
    """Capture specified attributes of an ORM object as a serialisable dict."""
    result: dict[str, Any] = {}
    for field in sorted(fields):
        value = getattr(obj, field, None)
        if isinstance(value, Decimal):
            result[field] = _normalize_decimal(value)
        elif isinstance(value, (datetime, date)):
            result[field] = value.isoformat()
        elif isinstance(value, UUID):
            result[field] = str(value)
        else:
            result[field] = value
    return result


def eod_record_id(instrument_id: UUID, trade_date: date) -> UUID:
    digest = hashlib.md5(
        f"market_data_eod:{instrument_id}:{trade_date.isoformat()}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return UUID(digest)


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values with numeric equality for Decimal types."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except Exception:
            pass
    return a == b


def _validate_blocking_dq_rules(eod: MarketDataEOD) -> None:
    """Apply blocking EOD DQ rules after patch values are merged."""
    blocking_messages: list[str] = []
    if eod.high is not None and eod.open is not None and eod.close is not None:
        max_oc = max(eod.open, eod.close)
        if eod.high < max_oc:
            blocking_messages.append(f"High ({eod.high}) is less than max(open, close) ({max_oc})")

    if eod.low is not None and eod.open is not None and eod.close is not None:
        min_oc = min(eod.open, eod.close)
        if eod.low > min_oc:
            blocking_messages.append(f"Low ({eod.low}) is greater than min(open, close) ({min_oc})")

    if eod.volume is not None and eod.volume < 0:
        blocking_messages.append(f"Volume ({eod.volume}) is negative")

    if not blocking_messages:
        return

    raise InvalidCorrectionError("; ".join(blocking_messages))


async def patch_eod_record(
    db: AsyncSession,
    instrument_id: UUID,
    trade_date: date,
    patch: PatchEODRequest,
    api_key: object,
) -> tuple[CanonicalCorrection, MarketDataEOD]:
    """
    Apply a partial update to a MarketDataEOD record and write an audit row.

    Returns:
        (correction, eod) tuple on success.
    Raises:
        RecordNotFoundError: if no EOD record matches instrument_id + trade_date.
        NoChangesError: if the patch contains no OHLCV fields or values are identical.
    """
    stmt = select(MarketDataEOD).where(
        MarketDataEOD.instrument_id == instrument_id,
        MarketDataEOD.trade_date == trade_date,
    )
    result = await db.execute(stmt)
    eod = result.scalar_one_or_none()
    if eod is None:
        raise RecordNotFoundError(
            f"EOD record not found for instrument {instrument_id} on {trade_date}"
        )

    changed_fields = patch.model_fields_set & _CORRECTABLE_EOD_FIELDS
    if not changed_fields:
        raise NoChangesError("No OHLCV fields provided in the patch request")

    original_values = {f: getattr(eod, f) for f in changed_fields}
    before = _snapshot(eod, changed_fields)

    for field in changed_fields:
        setattr(eod, field, getattr(patch, field))

    if all(_values_equal(original_values[f], getattr(eod, f)) for f in changed_fields):
        raise NoChangesError(
            "No changes detected — submitted values are identical to current values"
        )

    _validate_blocking_dq_rules(eod)

    after = _snapshot(eod, changed_fields)

    correction = CanonicalCorrection(
        id=uuid7(),
        table_name="market_data_eod",
        record_id=eod_record_id(instrument_id, trade_date),
        instrument_id=instrument_id,
        trade_date=trade_date,
        corrected_by=build_correction_actor(api_key),
        correction_reason=patch.correction_reason,
        before_snapshot=before,
        after_snapshot=after,
        created_at=utc_now(),
    )
    db.add(correction)
    await _update_stats_after_eod_patch(db, eod)
    await db.commit()

    return correction, eod


async def _update_stats_after_eod_patch(db: AsyncSession, eod: MarketDataEOD) -> None:
    stats = InstrumentStats.__table__
    now = utc_now()
    stmt = insert(InstrumentStats).values(
        instrument_id=eod.instrument_id,
        first_trade_date=eod.trade_date,
        latest_trade_date=eod.trade_date,
        latest_price=eod.close,
        updated_at=now,
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=["instrument_id"],
        set_={
            "first_trade_date": func.least(stats.c.first_trade_date, excluded.first_trade_date),
            "latest_trade_date": case(
                (
                    stats.c.latest_trade_date.is_(None)
                    | (excluded.latest_trade_date >= stats.c.latest_trade_date),
                    excluded.latest_trade_date,
                ),
                else_=stats.c.latest_trade_date,
            ),
            "latest_price": case(
                (
                    stats.c.latest_trade_date.is_(None)
                    | (excluded.latest_trade_date >= stats.c.latest_trade_date),
                    excluded.latest_price,
                ),
                else_=stats.c.latest_price,
            ),
            "updated_at": now,
        },
    )
    await db.execute(stmt)


async def resolve_dq_issue(
    db: AsyncSession,
    issue_id: UUID,
    request: ResolveDQIssueRequest,
    api_key: object,
) -> tuple[CanonicalCorrection, DQIssue]:
    """
    Mark a DQ issue as resolved and write an audit row.

    Returns:
        (correction, issue) tuple on success.
    Raises:
        RecordNotFoundError: if the DQ issue does not exist.
        AlreadyResolvedError: if the issue is already resolved.
    """
    issue = await db.get(DQIssue, issue_id)
    if issue is None:
        raise RecordNotFoundError(f"DQ issue {issue_id} not found")

    if issue.resolved:
        raise AlreadyResolvedError(f"DQ issue {issue_id} is already resolved")

    before: dict[str, Any] = {"resolved": False, "resolved_at": None}

    issue.resolved = True
    issue.resolved_at = utc_now()

    after: dict[str, Any] = {
        "resolved": True,
        "resolved_at": issue.resolved_at.isoformat(),
    }

    correction = CanonicalCorrection(
        id=uuid7(),
        table_name="dq_issue",
        record_id=issue_id,
        instrument_id=issue.instrument_id,
        trade_date=None,
        corrected_by=build_correction_actor(api_key),
        correction_reason=request.correction_reason,
        before_snapshot=before,
        after_snapshot=after,
        created_at=utc_now(),
    )
    db.add(correction)
    await db.commit()

    return correction, issue


_POLICY_EVIDENCE_KEYS = {
    "record_count",
    "minimum_record_count",
    "maximum_count_drop_ratio",
    "fetched_at",
    "maximum_fetch_age_hours",
    "allowed_clock_skew_minutes",
    "batch_data_date",
    "maximum_row_date",
    "data_date",
    "calendar_market",
    "evaluation_date",
}
_POLICY_CODES = {
    "BATCH_RECORD_COUNT_DROP",
    "STALE_PAYLOAD",
    "LATEST_DATE_MISSING",
    "CALENDAR_UNAVAILABLE",
}
_POLICY_ACTIONS = {"warn", "reject"}
_MAX_POLICY_DETAIL_BYTES = 8_192
_MAX_POLICY_STRING = 200
_MAX_POLICY_VIOLATIONS = 8
_MAX_POLICY_COUNTS = 8


def _bounded_policy_scalar(value: Any) -> str | int | float | bool | None:
    """Keep only finite scalar evidence with a strict string bound."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_MAX_POLICY_STRING]
    return None


def _bounded_policy_evidence(value: Any) -> dict[str, str | int | float | bool | None]:
    if not isinstance(value, dict):
        return {}
    return {
        key: bounded
        for key, raw in value.items()
        if key in _POLICY_EVIDENCE_KEYS
        and isinstance(key, str)
        and (bounded := _bounded_policy_scalar(raw)) is not None
    }


def _bounded_policy_detail(value: Any) -> dict[str, Any] | None:
    """Shape aggregate policy evidence without returning arbitrary raw JSON."""
    if not isinstance(value, dict):
        return None
    candidate = value.get("delivery_policy")
    if isinstance(candidate, dict):
        value = candidate
    violations = value.get("violations")
    if not isinstance(violations, list):
        return None

    bounded_violations: list[dict[str, Any]] = []
    for item in violations[:_MAX_POLICY_VIOLATIONS]:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        action = item.get("action")
        reason = item.get("reason")
        if not isinstance(code, str) or code not in _POLICY_CODES:
            continue
        if not isinstance(action, str) or action not in _POLICY_ACTIONS:
            continue
        if not isinstance(reason, str):
            reason = ""
        bounded_violations.append(
            {
                "code": code,
                "action": action,
                "reason": reason[:_MAX_POLICY_STRING],
                "observed": _bounded_policy_evidence(item.get("observed")),
                "expected": _bounded_policy_evidence(item.get("expected")),
            }
        )

    primary_code = value.get("primary_code")
    if not isinstance(primary_code, str) or primary_code not in _POLICY_CODES:
        primary_code = None
    baseline_status = value.get("baseline_status")
    if baseline_status not in {"disabled", "cold_start", "active", "not_applicable"}:
        baseline_status = None
    raw_counts = value.get("baseline_counts")
    baseline_counts = (
        [item for item in raw_counts if isinstance(item, int) and item >= 0][:_MAX_POLICY_COUNTS]
        if isinstance(raw_counts, list)
        else []
    )
    violation_count = len(violations)
    baseline_count = len(raw_counts) if isinstance(raw_counts, list) else 0
    detail: dict[str, Any] = {
        "primary_code": primary_code,
        "violations": bounded_violations,
        "violation_count": violation_count,
        "violations_truncated": violation_count > _MAX_POLICY_VIOLATIONS,
        "baseline_status": baseline_status,
        "baseline_counts": baseline_counts,
        "baseline_count": baseline_count,
        "baseline_counts_truncated": baseline_count > _MAX_POLICY_COUNTS,
    }
    threshold = value.get("effective_count_threshold")
    if isinstance(threshold, int) and threshold >= 0:
        detail["effective_count_threshold"] = threshold
    # A malformed or unexpectedly large aggregate is represented by a safe
    # minimal detail rather than being allowed to approach the response cap.
    try:
        if (
            len(json.dumps(detail, ensure_ascii=False, separators=(",", ":")))
            > _MAX_POLICY_DETAIL_BYTES
        ):
            return {
                "primary_code": primary_code,
                "violations": [],
                "violation_count": violation_count,
                "violations_truncated": True,
                "baseline_status": baseline_status,
                "baseline_counts": [],
                "baseline_count": baseline_count,
                "baseline_counts_truncated": True,
            }
    except (TypeError, ValueError):
        return None
    return detail


async def list_dq_issues(
    db: AsyncSession,
    resolved: Optional[bool],
    instrument_id: Optional[UUID],
    severity: Optional[str],
    page: int,
    page_size: int,
) -> tuple[list[DQIssueView], int]:
    """Return a paginated joined DQ projection with bounded policy evidence."""
    filters = []
    if resolved is not None:
        filters.append(DQIssue.resolved == resolved)
    if instrument_id is not None:
        filters.append(DQIssue.instrument_id == instrument_id)
    if severity is not None:
        filters.append(DQIssue.severity == severity)

    count_stmt = select(func.count(DQIssue.id)).where(*filters)
    total = (await db.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    # Paginate the issue/run relation first.  Joining raw rows here would make
    # one issue occupy multiple SQL rows when a legacy run has more than one
    # raw payload, causing duplicates or skipped issues across pages.
    rows_stmt = (
        select(DQIssue, IngestionRun)
        .outerjoin(IngestionRun, IngestionRun.run_id == DQIssue.run_id)
        .where(*filters)
        .order_by(DQIssue.created_at.desc(), DQIssue.id.desc())
        .offset(offset)
        .limit(page_size)
    )
    joined_rows = (await db.execute(rows_stmt)).all()
    now = utc_now()

    # Look up raw payloads only for the bounded page.  Exact run linkage wins;
    # the run-id fallback is used only for legacy rows with no raw_payload_id,
    # and its newest payload is selected deterministically.
    exact_run_ids: dict[UUID, UUID] = {
        run.raw_payload_id: run.run_id
        for _, run in joined_rows
        if run is not None and run.raw_payload_id is not None
    }
    fallback_run_ids = {
        run.run_id for _, run in joined_rows if run is not None and run.raw_payload_id is None
    }
    raw_by_run: dict[UUID, RawMarketPayload] = {}
    if exact_run_ids or fallback_run_ids:
        raw_filters = []
        if exact_run_ids:
            raw_filters.append(RawMarketPayload.raw_payload_id.in_(exact_run_ids))
        if fallback_run_ids:
            raw_filters.append(RawMarketPayload.run_id.in_(fallback_run_ids))
        raw_stmt = (
            select(RawMarketPayload)
            .where(or_(*raw_filters))
            .order_by(RawMarketPayload.created_at.desc(), RawMarketPayload.raw_payload_id.desc())
        )
        raw_rows = list((await db.execute(raw_stmt)).scalars().all())
        for raw_row in raw_rows:
            exact_run_id = exact_run_ids.get(raw_row.raw_payload_id)
            if exact_run_id is not None:
                # The exact raw_payload_id is authoritative even when expired.
                raw_by_run[exact_run_id] = raw_row
            elif raw_row.run_id in fallback_run_ids and raw_row.run_id not in raw_by_run:
                raw_by_run[raw_row.run_id] = raw_row

    views: list[DQIssueView] = []
    for issue, run in joined_rows:
        raw = raw_by_run.get(run.run_id) if run is not None else None
        raw_available = bool(raw is not None and raw.expire_at > now)
        raw_payload_id = run.raw_payload_id if run is not None else None
        if raw is not None:
            raw_payload_id = raw.raw_payload_id
        fetched_at = raw.fetched_at if raw is not None and raw_available else None
        source = run.source if run is not None else (raw.source if raw else None)
        dataset_key = run.dataset_key if run is not None else (raw.dataset_key if raw else None)
        schema_id = run.schema_id if run is not None else (raw.schema_id if raw else None)
        schema_version = (
            run.schema_version if run is not None else (raw.schema_version if raw else None)
        )
        request_key = run.request_key if run is not None else (raw.request_key if raw else None)
        run_id = issue.run_id or (run.run_id if run is not None else None)
        trade_date = (
            issue.trade_date.date() if isinstance(issue.trade_date, datetime) else issue.trade_date
        )
        policy_evidence = None
        if issue.issue_type == "INGRESS_DELIVERY_POLICY_WARNING":
            policy_evidence = _bounded_policy_detail(
                run.policy_details
                if run is not None and run.policy_details is not None
                else issue.raw_data
            )
        views.append(
            DQIssueView(
                id=issue.id,
                run_id=run_id,
                instrument_id=issue.instrument_id,
                trade_date=trade_date,
                issue_type=issue.issue_type,
                severity=issue.severity,
                description=issue.description[:1_000] if issue.description else None,
                source=source,
                provider=source,
                dataset_key=dataset_key,
                schema_id=schema_id,
                schema_version=schema_version,
                raw_payload_id=raw_payload_id,
                raw_available=raw_available,
                fetched_at=fetched_at,
                request_key=request_key,
                batch_data_date=run.batch_data_date if run is not None else None,
                policy_detail=policy_evidence,
                resolved=issue.resolved,
                resolved_at=issue.resolved_at,
                created_at=issue.created_at,
            )
        )
    return views, total


async def list_corrections(
    db: AsyncSession,
    table_name: Optional[str],
    instrument_id: Optional[UUID],
    page: int,
    page_size: int,
) -> tuple[list[CanonicalCorrection], int]:
    """
    Return a paginated, newest-first list of correction audit records.

    Returns:
        (rows, total_count) tuple.
    """
    base_filter = []
    if table_name is not None:
        base_filter.append(CanonicalCorrection.table_name == table_name)
    if instrument_id is not None:
        base_filter.append(CanonicalCorrection.instrument_id == instrument_id)

    count_stmt = select(func.count(CanonicalCorrection.id)).where(*base_filter)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    rows_stmt = (
        select(CanonicalCorrection)
        .where(*base_filter)
        .order_by(CanonicalCorrection.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows_result = await db.execute(rows_stmt)
    rows = list(rows_result.scalars().all())

    return rows, total
