"""Persist an index family's membership history, rebuilt from everything stored.

Every reference delivery ends by rebuilding its family from all stored
snapshots and effective changes, so the order documents arrive in never
matters.  A rebuild replaces the family's membership in one transaction; the
disagreements it finds are returned as data-quality warnings and never block.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Hashable
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import Instrument, InstrumentStats
from app.models.index_reference import (
    IndexChangeEvent,
    IndexDefinition,
    IndexMembership,
    IndexMembershipBuild,
    IndexSnapshot,
    IndexSnapshotConstituent,
)
from app.services.index_reconstruction import (
    ChangeEvent,
    Finding,
    History,
    build_history,
    canonical_snapshots,
    partition_findings,
)

# Findings of one rule and index beyond this many are counted, not listed.
FINDING_SAMPLE = 20
# Index codes are exchange-specific; membership is resolved against this market.
INDEX_MARKET = "HK"


@dataclass
class RebuildResult:
    build: IndexMembershipBuild
    findings: list[Finding] = field(default_factory=list)
    unresolved_open: list[str] = field(default_factory=list)


async def lock_family(db: AsyncSession, publisher: str, series: str) -> None:
    """Serialise every write and rebuild of one family within the transaction."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"index_family:{publisher}:{series}"},
    )


async def _definitions(db: AsyncSession, publisher: str, series: str) -> list[IndexDefinition]:
    return list(
        (
            await db.scalars(
                select(IndexDefinition)
                .where(IndexDefinition.publisher == publisher, IndexDefinition.series == series)
                .order_by(IndexDefinition.code)
            )
        ).all()
    )


async def _snapshot_headers(db: AsyncSession, index_ids: list[UUID]) -> list[str]:
    rows = (
        await db.execute(
            select(IndexSnapshot.index_id, IndexSnapshot.trade_date, IndexSnapshot.archive_id)
            .where(IndexSnapshot.index_id.in_(index_ids))
            .order_by(IndexSnapshot.index_id, IndexSnapshot.trade_date)
        )
    ).all()
    return [f"{index_id}:{trade_date}:{archive_id}" for index_id, trade_date, archive_id in rows]


async def _snapshots(
    db: AsyncSession, index_ids: list[UUID]
) -> tuple[dict[UUID, dict[date, set[str]]], dict[str, str]]:
    rows = (
        await db.execute(
            select(
                IndexSnapshot.index_id,
                IndexSnapshot.trade_date,
                IndexSnapshotConstituent.symbol,
                IndexSnapshotConstituent.name_en,
            )
            .join(
                IndexSnapshotConstituent,
                IndexSnapshotConstituent.snapshot_id == IndexSnapshot.snapshot_id,
            )
            .where(IndexSnapshot.index_id.in_(index_ids))
            .order_by(IndexSnapshot.trade_date)
        )
    ).all()
    members: dict[UUID, dict[date, set[str]]] = defaultdict(lambda: defaultdict(set))
    names: dict[str, str] = {}
    for index_id, trade_date, symbol, name in rows:
        members[index_id][trade_date].add(symbol)
        if name:
            names[symbol] = name
    return members, names


async def _events(db: AsyncSession, index_ids: list[UUID]) -> list[IndexChangeEvent]:
    return list(
        (
            await db.scalars(
                select(IndexChangeEvent)
                .where(
                    IndexChangeEvent.index_id.in_(index_ids),
                    IndexChangeEvent.status == "effective",
                )
                .order_by(IndexChangeEvent.effective_date, IndexChangeEvent.source_ordinal)
            )
        ).all()
    )


async def _instruments(db: AsyncSession, symbols: set[str]) -> dict[str, tuple[UUID, date | None]]:
    """The one instrument each code names in the market, and its first EOD day."""
    if not symbols:
        return {}
    rows = (
        await db.execute(
            select(Instrument.symbol, Instrument.instrument_id, InstrumentStats.eod_first_date)
            .outerjoin(InstrumentStats, InstrumentStats.instrument_id == Instrument.instrument_id)
            .where(Instrument.market == INDEX_MARKET, Instrument.symbol.in_(symbols))
        )
    ).all()
    found: dict[str, list[tuple[UUID, date | None]]] = defaultdict(list)
    for symbol, instrument_id, first_eod in rows:
        found[symbol].append((instrument_id, first_eod))
    # A code shared by two instruments (another asset class) is left unresolved.
    return {symbol: matches[0] for symbol, matches in found.items() if len(matches) == 1}


def _resolves(valid_to: date | None, observed_from: date, first_eod: date | None) -> bool:
    """Whether the interval's code can be taken to name today's instrument.

    Codes are reused, so an interval is resolved only if it reaches the snapshot
    period or the instrument already had prices while it lasted.
    """
    if valid_to is None or valid_to > observed_from:
        return True
    return first_eod is not None and valid_to > first_eod


def _digest(
    definitions: list[IndexDefinition], archives: list[str], events: list[IndexChangeEvent]
) -> str:
    material = {
        "indexes": [
            (item.code, item.parent_index_id and str(item.parent_index_id)) for item in definitions
        ],
        "snapshots": archives,
        "events": [
            (
                str(event.index_id),
                event.effective_date.isoformat(),
                event.action,
                event.symbol,
                event.constituents_after,
            )
            for event in events
        ],
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


async def rebuild_family(
    db: AsyncSession, *, publisher: str, series: str, run_id: UUID | None
) -> RebuildResult:
    """Replace the family's membership with the history all stored documents imply.

    The caller holds the family lock (``lock_family``) for its transaction.  A
    family whose stored inputs are unchanged since its latest build is not
    rebuilt: a delivery that adds nothing costs no reconstruction.
    """
    definitions = await _definitions(db, publisher, series)
    ids = [item.index_id for item in definitions]
    roots = [item for item in definitions if item.parent_index_id is None]
    archives = await _snapshot_headers(db, ids)
    events = await _events(db, ids)
    digest = _digest(definitions, archives, events)
    latest = await db.scalar(
        select(IndexMembershipBuild)
        .where(IndexMembershipBuild.publisher == publisher, IndexMembershipBuild.family == series)
        .order_by(IndexMembershipBuild.created_at.desc(), IndexMembershipBuild.build_id.desc())
        .limit(1)
    )
    if latest is not None and latest.inputs_sha256 == digest:
        return RebuildResult(latest)
    if len(roots) != 1 or not archives:
        # Nothing anchors the family: no membership is claimed, not even an old one.
        await db.execute(delete(IndexMembership).where(IndexMembership.index_id.in_(ids)))
        build = IndexMembershipBuild(
            publisher=publisher,
            family=series,
            anchor_trade_date=None,
            observed_from=None,
            findings=0,
            inputs_sha256=digest,
            status="no_anchor",
            run_id=run_id,
        )
        db.add(build)
        await db.flush()
        return RebuildResult(build)
    root = roots[0]
    snapshot_sets, names = await _snapshots(db, ids)
    codes = {item.index_id: item.code for item in definitions}
    canonical, findings = canonical_snapshots(
        {codes[index_id]: days for index_id, days in snapshot_sets.items()}, root.code
    )
    grouped: dict[UUID, list[ChangeEvent]] = defaultdict(list)
    for event in events:
        grouped[event.index_id].append(
            ChangeEvent(
                event.effective_date,
                event.action,  # type: ignore[arg-type]
                event.symbol,
                event.constituents_after,
                event.event_id,
            )
        )
        if event.name_en:
            names.setdefault(event.symbol, event.name_en)
    histories: dict[UUID, History] = {}
    for item in definitions:
        history, found = build_history(
            item.code, canonical.get(item.code, {}), grouped[item.index_id]
        )
        histories[item.index_id] = history
        findings.extend(found)
    children = [item for item in definitions if item.parent_index_id == root.index_id]
    starts = [item.history_start for item in children if item.history_start is not None]
    if children and len(starts) == len(children):
        # Children may be younger than the parent; partitions are checked once all exist.
        since = max(starts)
        days = {day for child in children for day, _ in histories[child.index_id].after}
        days |= {day for day, _ in histories[root.index_id].after}
        findings.extend(
            partition_findings(
                root.code,
                histories[root.index_id],
                {child.code: histories[child.index_id] for child in children},
                [day for day in days if day >= since],
            )
        )
    anchor = max(day for days in snapshot_sets.values() for day in days)
    observed_from = min(day for days in snapshot_sets.values() for day in days)
    intervals = {index_id: history.intervals() for index_id, history in histories.items()}
    instruments = await _instruments(
        db, {interval.symbol for rows in intervals.values() for interval in rows}
    )
    await db.execute(delete(IndexMembership).where(IndexMembership.index_id.in_(ids)))
    build = IndexMembershipBuild(
        publisher=publisher,
        family=series,
        anchor_trade_date=anchor,
        observed_from=observed_from,
        findings=len(findings),
        inputs_sha256=digest,
        status="consistent",
        run_id=run_id,
    )
    db.add(build)
    await db.flush()
    unresolved: set[str] = set()
    for index_id, rows in intervals.items():
        for interval in rows:
            match = instruments.get(interval.symbol)
            resolved = match is not None and _resolves(interval.valid_to, observed_from, match[1])
            if interval.valid_to is None and not resolved:
                unresolved.add(interval.symbol)
            db.add(
                IndexMembership(
                    index_id=index_id,
                    symbol=interval.symbol,
                    instrument_id=match[0] if match is not None and resolved else None,
                    name_en=names.get(interval.symbol),
                    valid_from=interval.valid_from,
                    valid_to=interval.valid_to,
                    from_event_id=_event_id(interval.from_event),
                    to_event_id=_event_id(interval.to_event),
                    build_id=build.build_id,
                )
            )
    await db.flush()
    return RebuildResult(build, findings, sorted(unresolved))


def _event_id(key: Hashable) -> UUID | None:
    return key if isinstance(key, UUID) else None


def summarize(findings: list[Finding]) -> list[dict[str, object]]:
    """One record per rule and index: how many, and the first few in date order."""
    grouped: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        grouped[(finding.rule, finding.index_code)].append(finding)
    return [
        {
            "rule": rule,
            "index_code": index_code,
            "count": len(items),
            "sample": [dict(item.detail) for item in items[:FINDING_SAMPLE]],
        }
        for (rule, index_code), items in sorted(grouped.items())
    ]
