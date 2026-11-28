"""Rebuild an index family's membership history from snapshots and published changes.

Two sources describe the same membership differently:

- a daily constituent report (snapshot) lists the members after every change
  effective on or before its day, under the code each member trades as that day;
- a change workbook lists each effective addition and deletion under the
  member's permanent code, back to the publisher's earliest recorded change.

Snapshots are authoritative where they exist.  A temporary counter (codes
02900-02999, used while a stock consolidates or splits) is mapped back to the
permanent code it stands in for, by the same-day swap the snapshots show or,
failing that, by the company name it keeps; a change listed under a counter
code is mapped by name the same way.  A placeholder is not a member: the
``99999`` code, or a detached security (a share distributed in specie, listed
until it is sold) whichever code it is printed under.  Before the earliest snapshot the
membership is derived by rolling it back through the published changes: the
members before a change date are the members after it, less that date's
additions, plus its deletions.  Where the sources disagree the history keeps the
snapshot and reports a finding; nothing here rejects data or touches a database.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

PLACEHOLDER_SYMBOLS = frozenset({"99999"})
# A share distributed in specie is listed, unweighted, until it can be sold; the
# report has printed one under 99999 and then under a code another stock had used.
PLACEHOLDER_NAME_PREFIX = "detached sec"


def is_temporary_counter(symbol: str) -> bool:
    """HKEX reserves 02900-02999 for counters that trade only during a share change."""
    return "02900" <= symbol <= "02999"


@dataclass(frozen=True)
class Finding:
    """A disagreement between the sources, kept as a data-quality warning."""

    rule: str
    index_code: str
    detail: Mapping[str, object]


def _plain(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)  # type: ignore[type-var]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _finding(rule: str, index_code: str, **detail: object) -> Finding:
    return Finding(rule, index_code, {key: _plain(value) for key, value in detail.items()})


@dataclass(frozen=True)
class ChangeEvent:
    effective_date: date
    action: Literal["add", "delete"]
    symbol: str
    constituents_after: int
    key: Hashable = None
    name: str | None = None


@dataclass(frozen=True)
class Interval:
    """Membership over ``[valid_from, valid_to)``; ``valid_from`` None is before history."""

    symbol: str
    valid_from: date | None
    valid_to: date | None
    from_event: Hashable = None
    to_event: Hashable = None


@dataclass
class History:
    """Members before the earliest known change, and after each change date."""

    before: frozenset[str]
    after: list[tuple[date, frozenset[str]]] = field(default_factory=list)
    # The first snapshot day: membership before it is derived from the changes.
    observed_from: date | None = None
    events: dict[tuple[date, str, str], Hashable] = field(default_factory=dict)
    # Codes deleted and added again on one date: one membership ends, another begins.
    replaced: set[tuple[date, str]] = field(default_factory=set)

    def members_on(self, day: date) -> frozenset[str]:
        """Members on ``day``: after every change effective on or before it."""
        position = bisect_right(self.after, day, key=lambda item: item[0])
        return self.after[position - 1][1] if position else self.before

    def intervals(self) -> list[Interval]:
        open_from: dict[str, tuple[date | None, Hashable]] = {
            symbol: (None, None) for symbol in self.before
        }
        previous = self.before
        result: list[Interval] = []

        def close(symbol: str, day: date) -> None:
            start, from_event = open_from.pop(symbol)
            result.append(
                Interval(symbol, start, day, from_event, self.events.get((day, symbol, "delete")))
            )

        for changed, members in self.after:
            replaced = {symbol for day, symbol in self.replaced if day == changed}
            for symbol in sorted((previous - members) | (replaced & previous & members)):
                close(symbol, changed)
            for symbol in sorted((members - previous) | (replaced & previous & members)):
                open_from[symbol] = (changed, self.events.get((changed, symbol, "add")))
            previous = members
        for symbol, (start, from_event) in sorted(open_from.items()):
            result.append(Interval(symbol, start, None, from_event, None))
        return sorted(result, key=lambda item: (item.symbol, item.valid_from or date.min))


def name_key(name: str) -> str:
    """A company name compared without case or spacing."""
    return " ".join(name.split()).casefold()


def is_placeholder(symbol: str, name: str | None = None) -> bool:
    """A listed row that is not a member: the placeholder code or a detached security."""
    return symbol in PLACEHOLDER_SYMBOLS or (
        name is not None and name_key(name).startswith(PLACEHOLDER_NAME_PREFIX)
    )


def permanent_codes_by_name(listed: Iterable[tuple[str, str | None]]) -> dict[str, str]:
    """The one permanent code each company name is listed under, keyed by name.

    A temporary counter trades under the company's own name, so the name finds
    the code it stands in for.  A name listed under several permanent codes
    (codes are reused, names repeat) decides nothing.
    """
    codes: dict[str, set[str]] = defaultdict(set)
    for symbol, name in listed:
        if name and not is_temporary_counter(symbol) and not is_placeholder(symbol, name):
            codes[name_key(name)].add(symbol)
    return {key: next(iter(found)) for key, found in codes.items() if len(found) == 1}


def _by_name(name: str | None, by_name: Mapping[str, str]) -> str | None:
    return by_name.get(name_key(name)) if name else None


def with_permanent_codes(
    index_code: str, events: Iterable[ChangeEvent], by_name: Mapping[str, str]
) -> tuple[list[ChangeEvent], list[Finding]]:
    """Changes listed under a temporary counter, moved to the code the name keeps.

    The change workbook lists a change under the code the stock traded as that
    day; a counter it was added or deleted under would otherwise never meet the
    permanent code the snapshots list.  A counter change whose name finds no
    permanent code is kept as listed and reported.
    """
    findings: list[Finding] = []
    result: list[ChangeEvent] = []
    for event in events:
        if is_temporary_counter(event.symbol):
            permanent = _by_name(event.name, by_name)
            if permanent is None:
                findings.append(
                    _finding(
                        "temporary_counter_change_unpaired",
                        index_code,
                        day=event.effective_date,
                        symbol=event.symbol,
                    )
                )
            else:
                event = replace(event, symbol=permanent)
        result.append(event)
    return result, findings


def canonical_snapshots(
    snapshots: Mapping[str, Mapping[date, Iterable[str]]],
    root: str,
    names: Mapping[tuple[str, date], str] | None = None,
    by_name: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[date, frozenset[str]]], list[Finding]]:
    """Family snapshots under permanent codes, without placeholders.

    ``names`` (by code and day) also tells a detached security from a member.

    Each run of days a temporary counter is in the family's root index is paired
    with the permanent code it replaces: the code that leaves the root the day
    the run starts, or joins it the day after the run ends.  A code is reused for
    other stocks later, so every run is paired on its own.  When several codes
    swap on one day a pair must also share the child index.  A run no swap
    shows, such as one already under way at the first snapshot, is paired by the
    name it is listed under (``names``, by code and day) through ``by_name``;
    a run that still cannot be paired is left out and reported.
    """
    findings: list[Finding] = []
    cleaned = {
        code: {
            day: frozenset(
                symbol
                for symbol in members
                if not is_placeholder(symbol, (names or {}).get((symbol, day)))
            )
            for day, members in days.items()
        }
        for code, days in snapshots.items()
    }
    root_snapshots = cleaned.get(root, {})
    days = sorted(root_snapshots)
    children = {code: values for code, values in cleaned.items() if code != root}

    def child_of(symbol: str, day: date) -> frozenset[str]:
        return frozenset(code for code, values in children.items() if symbol in values.get(day, ()))

    def partner_on(counter: str, seen: date, other: date, permanent: set[str]) -> str | None:
        candidates = [
            symbol
            for symbol in permanent
            if len(permanent) == 1 or child_of(symbol, other) == child_of(counter, seen)
        ]
        return candidates[0] if len(candidates) == 1 else None

    counters = sorted(
        {
            symbol
            for members in root_snapshots.values()
            for symbol in members
            if is_temporary_counter(symbol)
        }
    )
    mapping: dict[tuple[str, date], str] = {}
    for counter in counters:
        position = 0
        while position < len(days):
            if counter not in root_snapshots[days[position]]:
                position += 1
                continue
            start = position
            while position + 1 < len(days) and counter in root_snapshots[days[position + 1]]:
                position += 1
            end = position
            position += 1
            partner = None
            if start > 0:
                leaving = {
                    symbol
                    for symbol in root_snapshots[days[start - 1]] - root_snapshots[days[start]]
                    if not is_temporary_counter(symbol)
                }
                partner = partner_on(counter, days[start], days[start - 1], leaving)
            if partner is None and end + 1 < len(days):
                joining = {
                    symbol
                    for symbol in root_snapshots[days[end + 1]] - root_snapshots[days[end]]
                    if not is_temporary_counter(symbol)
                }
                partner = partner_on(counter, days[end], days[end + 1], joining)
            run = days[start : end + 1]
            if partner is None and by_name and names:
                partner = _by_name(names.get((counter, days[start])), by_name)
            if partner is None:
                findings.append(
                    _finding("temporary_counter_unpaired", root, day=run[0], symbol=counter)
                )
                continue
            for day in run:
                mapping[(counter, day)] = partner
    for code, values in cleaned.items():
        for day, members in values.items():
            values[day] = frozenset(
                mapping.get((symbol, day), "") if is_temporary_counter(symbol) else symbol
                for symbol in members
            ) - {""}
    return cleaned, findings


def build_history(
    index_code: str,
    snapshots: Mapping[date, frozenset[str]],
    events: Sequence[ChangeEvent],
) -> tuple[History, list[Finding]]:
    """The membership history of one index; empty without a snapshot."""
    findings: list[Finding] = []
    grouped: dict[date, list[ChangeEvent]] = defaultdict(list)
    for event in events:
        grouped[event.effective_date].append(event)
    replaced = set()
    for day, rows in grouped.items():
        added = {row.symbol for row in rows if row.action == "add"}
        deleted = {row.symbol for row in rows if row.action == "delete"}
        replaced |= {(day, symbol) for symbol in added & deleted}
    keyed: dict[tuple[date, str, str], Hashable] = {
        (event.effective_date, event.symbol, event.action): event.key for event in events
    }
    if not snapshots:
        return History(before=frozenset(), events=keyed, replaced=replaced), findings
    observed = sorted(snapshots)
    first = observed[0]
    after: dict[date, frozenset[str]] = {}
    previous = snapshots[first]
    after[first] = previous
    for day in observed[1:]:
        members = snapshots[day]
        if members != previous:
            after[day] = members
            rows = grouped.get(day, [])
            listed_add = {row.symbol for row in rows if row.action == "add"}
            listed_delete = {row.symbol for row in rows if row.action == "delete"}
            unlisted = {
                "added": (members - previous) - listed_add,
                "deleted": (previous - members) - listed_delete,
            }
            if unlisted["added"] or unlisted["deleted"]:
                findings.append(
                    _finding("snapshot_change_not_listed", index_code, day=day, **unlisted)
                )
        previous = members
    for day, rows in grouped.items():
        if first < day <= observed[-1] and day not in after:
            listed = (
                {(row.action, row.symbol) for row in rows}
                - {("add", symbol) for day_, symbol in replaced if day_ == day}
                - {("delete", symbol) for day_, symbol in replaced if day_ == day}
            )
            if listed:
                findings.append(
                    _finding(
                        "listed_change_not_in_snapshots",
                        index_code,
                        day=day,
                        changes=sorted(listed),
                    )
                )
    # Changes after the latest snapshot are applied as listed, then reconciled by later snapshots.
    current = snapshots[observed[-1]]
    for day in sorted(day for day in grouped if day > observed[-1]):
        rows = grouped[day]
        current = (current - {row.symbol for row in rows if row.action == "delete"}) | {
            row.symbol for row in rows if row.action == "add"
        }
        after[day] = current
    # Before the first snapshot: roll back through the listed changes, keeping what is known.
    current = snapshots[first]
    first_change = min((day for day in grouped if day <= first), default=None)
    for day in sorted((day for day in grouped if day <= first), reverse=True):
        rows = grouped[day]
        added = {row.symbol for row in rows if row.action == "add"}
        deleted = {row.symbol for row in rows if row.action == "delete"}
        kept = added & deleted
        if len(current) != rows[0].constituents_after:
            findings.append(
                _finding(
                    "count_after_change",
                    index_code,
                    day=day,
                    stated=rows[0].constituents_after,
                    derived=len(current),
                )
            )
        not_member = (added - kept) - current
        still_member = (deleted - kept) & current
        if not_member or still_member:
            findings.append(
                _finding(
                    "change_contradicts_members",
                    index_code,
                    day=day,
                    added_not_member=not_member,
                    deleted_still_member=still_member,
                )
            )
        if day < first:
            after[day] = current
        current = (current - (added - kept)) | (deleted - kept)
    history = History(
        before=current if first_change is not None else snapshots[first],
        after=sorted(after.items()),
        observed_from=first,
        events=keyed,
        replaced=replaced,
    )
    if first_change is None:
        # No change is listed up to the first snapshot: nothing earlier is known.
        history.after = [(day, members) for day, members in history.after if day >= first]
    return history, findings


def partition_findings(
    root_code: str,
    root: History,
    children: Mapping[str, History],
    days: Iterable[date],
) -> list[Finding]:
    """Days a parent's members are not split exactly once among its children."""
    findings: list[Finding] = []
    for day in sorted(set(days)):
        members = root.members_on(day)
        seen: dict[str, list[str]] = defaultdict(list)
        for code, child in children.items():
            for symbol in child.members_on(day):
                seen[symbol].append(code)
        outside = {symbol for symbol in seen if symbol not in members}
        missing = members - set(seen)
        repeated = {symbol for symbol, codes in seen.items() if len(codes) > 1}
        if outside or missing or repeated:
            findings.append(
                _finding(
                    "children_do_not_partition_parent",
                    root_code,
                    day=day,
                    outside_parent=outside,
                    in_no_child=missing,
                    in_several_children=repeated,
                )
            )
    return findings
