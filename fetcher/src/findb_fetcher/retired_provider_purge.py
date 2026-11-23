"""Purge a retired provider's rows from the host's provider-work journal and Full checkpoint.

Closed-work retention removes only the caller's own work, as Source answers it for
the caller's credential. A retired provider has no runtime or credential left, so
its rows would stay forever. The operator passes the closed work IDs that Backend
listed (``backend/scripts/retired_provider_works.py``); Backend keeps every record.

Dry-run unless ``--apply``. Account governor state (quota, usage, intents,
identities) is never touched: used quota is not refunded. Every consumer on the
host must be stopped first; ``--apply`` refuses a file another connection writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from findb_fetcher.execution_metadata import provider_ids

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9_]*$")
# Journal tables whose rows belong to one work, and the column holding bytes.
_JOURNAL_WORK_TABLES = {
    "work_manifest": "body",
    "work_delivery": "body",
    "work_response": "body",
    "work_response_observation": "body",
    "work_budget": None,
    "work_cooldown": None,
    "work_job": "prepared",
    "work_source_error": None,
}
# Checkpoint tables, and how their rows belong to the provider's plans and work.
_CHECKPOINT_WORK_TABLES = {
    "full_work_receipt": "key",
    "full_empty_outcome": "work_key",
    "full_member_repair": "work_key",
}
_CHECKPOINT_PLAN_TABLES = (
    "full_plan_reconcile",
    "full_provider_outage",
    "full_no_data_trip",
    "full_no_data_round",
)
_CHECKPOINT_PROVIDER_TABLES = (
    "full_refresh",
    "full_cursor",
    "full_date_prerequisite",
    "full_universe_refreshed",
    "checkpoint_retention",
)
_WRITER_TIMEOUT_SECONDS = 5


class PurgeError(RuntimeError):
    """The purge is refused; nothing was written."""


def work_ids_digest(work_ids: Sequence[str]) -> str:
    """SHA-256 of the sorted IDs, one per line, as ``retired_provider_works.py`` prints it."""
    return hashlib.sha256(
        "".join(f"{work_id}\n" for work_id in sorted(work_ids)).encode()
    ).hexdigest()


def load_works(path: Path, provider: str) -> list[str]:
    try:
        listed = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise PurgeError("works file is unreadable") from exc
    work_ids = listed.get("work_ids") if isinstance(listed, dict) else None
    if (
        listed.get("version") != 1
        or listed.get("provider") != provider
        or not isinstance(work_ids, list)
        or not all(isinstance(work_id, str) and _UUID.fullmatch(work_id) for work_id in work_ids)
        or len(set(work_ids)) != len(work_ids)
    ):
        raise PurgeError("works file does not list this provider's work IDs")
    if listed.get("sha256") != work_ids_digest(work_ids):
        raise PurgeError("works file digest does not match its work IDs")
    return sorted(work_ids)


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _targets(db: sqlite3.Connection, kind: str, provider: str, work_ids: Sequence[str]):
    """``(table, WHERE clause, parameters, bytes column)`` for every row to delete.

    Plan and work identities are resolved once, before anything is deleted, so the
    clauses stay the same while dependent tables go first.
    """
    tables = _tables(db)
    if kind == "journal":
        ids = json.dumps(list(work_ids))
        for table, column in _JOURNAL_WORK_TABLES.items():
            if table in tables:
                yield table, "work_key IN (SELECT value FROM json_each(?))", (ids,), column
        # A provider's own tables (Shioaji grants and probes) hold nothing else.
        for table in sorted(tables):
            if table.startswith(f"work_{provider}_"):
                yield table, "1", (), None
        if "work_retention" in tables:
            yield "work_retention", "provider=?", (provider,), None
        return
    plans = json.dumps(
        [
            row[0]
            for row in db.execute("SELECT plan_id FROM full_plan WHERE provider=?", (provider,))
        ]
    )
    keys = json.dumps(
        [
            row[0]
            for row in db.execute(
                "SELECT key FROM full_work WHERE provider=? "
                "OR plan_id IN (SELECT value FROM json_each(?))",
                (provider, plans),
            )
        ]
    )
    for table, column in _CHECKPOINT_WORK_TABLES.items():
        if table in tables:
            yield table, f"{column} IN (SELECT value FROM json_each(?))", (keys,), None
    if "full_work_diagnostic" in tables:
        # Diagnostics name their provider and their work; either one is enough.
        yield (
            "full_work_diagnostic",
            "provider=? OR key IN (SELECT value FROM json_each(?))",
            (provider, keys),
            None,
        )
    yield "full_work", "key IN (SELECT value FROM json_each(?))", (keys,), "body"
    for table in _CHECKPOINT_PLAN_TABLES:
        if table in tables:
            yield table, "plan_id IN (SELECT value FROM json_each(?))", (plans,), None
    if "full_schedule" in tables:
        yield "full_schedule", "scope IN (SELECT value FROM json_each(?))", (plans,), "batch"
    for table in _CHECKPOINT_PROVIDER_TABLES:
        if table in tables:
            yield table, "provider=?", (provider,), None
    yield "full_plan", "plan_id IN (SELECT value FROM json_each(?))", (plans,), "body"


def _check_manifests(db: sqlite3.Connection, provider: str, work_ids: Sequence[str]) -> None:
    """A listed work whose frozen manifest names another provider is not this purge's."""
    if "work_manifest" not in _tables(db):
        return
    for (body,) in db.execute(
        "SELECT body FROM work_manifest WHERE work_key IN (SELECT value FROM json_each(?))",
        (json.dumps(list(work_ids)),),
    ):
        try:
            named = json.loads(body).get("provider")
        except (ValueError, AttributeError):
            continue
        if named is not None and named != provider:
            raise PurgeError("a listed work belongs to another provider")


def _unproven(db: sqlite3.Connection, kind: str, provider: str, work_ids: Sequence[str]) -> int:
    """Local bodies that never got a receipt; they are deleted too, but counted."""
    tables = _tables(db)
    if kind == "journal":
        if "work_delivery" not in tables:
            return 0
        return int(
            db.execute(
                "SELECT COUNT(*) FROM work_delivery WHERE receipt IS NULL "
                "AND work_key IN (SELECT value FROM json_each(?))",
                (json.dumps(list(work_ids)),),
            ).fetchone()[0]
        )
    return int(
        db.execute(
            "SELECT COUNT(*) FROM full_work w WHERE w.provider=? AND w.body IS NOT NULL "
            "AND w.receipt IS NULL AND NOT EXISTS (SELECT 1 FROM full_work_receipt r WHERE r.key=w.key)",
            (provider,),
        ).fetchone()[0]
    )


def _size(db: sqlite3.Connection) -> dict[str, int]:
    page = db.execute("PRAGMA page_size").fetchone()[0]
    return {
        "file_bytes": db.execute("PRAGMA page_count").fetchone()[0] * page,
        "free_bytes": db.execute("PRAGMA freelist_count").fetchone()[0] * page,
    }


def _count(db: sqlite3.Connection, table: str, where: str, params: tuple) -> int:
    return int(db.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}', params).fetchone()[0])


def purge_file(
    path: Path,
    kind: str,
    provider: str,
    work_ids: Sequence[str],
    *,
    apply: bool,
    vacuum: bool = False,
) -> dict[str, Any]:
    """Report, and with ``apply`` delete, the provider's rows in one SQLite file."""
    if not path.is_file():
        raise PurgeError(f"{kind} file does not exist")
    uri = f"file:{path}?mode={'rw' if apply else 'ro'}"
    db = sqlite3.connect(uri, uri=True, timeout=_WRITER_TIMEOUT_SECONDS, isolation_level=None)
    try:
        if apply:
            # Debian's SQLite zeroes freed pages and keeps them in a temporary file
            # until the statement ends; the container's /tmp is far too small.
            db.execute("PRAGMA secure_delete=OFF")
            try:
                db.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError as exc:
                raise PurgeError(f"{kind} is in use; stop every consumer first") from exc
        else:
            db.execute("BEGIN")
        if kind == "journal":
            _check_manifests(db, provider, work_ids)
        report: dict[str, Any] = {
            "file": kind,
            "before": _size(db),
            "unreceipted": _unproven(db, kind, provider, work_ids),
            "tables": {},
        }
        targets = list(_targets(db, kind, provider, work_ids))
        totals = {table: _count(db, table, "1", ()) for table, *_ in targets}
        for table, where, params, column in targets:
            entry = report["tables"].setdefault(table, {"rows": 0, "bytes": 0})
            entry["rows"] += _count(db, table, where, params)
            if column:
                entry["bytes"] += int(
                    db.execute(
                        f'SELECT COALESCE(SUM(LENGTH("{column}")),0) FROM "{table}" WHERE {where}',
                        params,
                    ).fetchone()[0]
                )
        for table, entry in report["tables"].items():
            entry["kept"] = totals[table] - entry["rows"]
        if not apply:
            db.execute("ROLLBACK")
            return report
        for table, where, params, _ in targets:
            db.execute(f'DELETE FROM "{table}" WHERE {where}', params)
        for table, where, params, _ in targets:
            if _count(db, table, where, params):
                raise PurgeError(f"{table} still holds rows of the provider")
        for table, entry in report["tables"].items():
            if _count(db, table, "1", ()) != entry["kept"]:
                raise PurgeError(f"{table} lost rows of other providers")
        db.execute("COMMIT")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PurgeError(f"{kind} failed its integrity check after the purge")
        if vacuum:
            # In place, so the file keeps its inode, owner and mode.
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        report["after"] = _size(db)
        return report
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def purge(
    provider: str,
    works: Path,
    *,
    journal: Path,
    state: Path,
    apply: bool,
    vacuum: bool = False,
) -> dict[str, Any]:
    if not _PROVIDER.fullmatch(provider):
        raise PurgeError("provider name is invalid")
    if provider in provider_ids():
        raise PurgeError("provider is still packaged; only a retired provider can be purged")
    if vacuum and not apply:
        raise PurgeError("--vacuum needs --apply")
    if vacuum:
        # SQLite picks its temporary directory when the module loads, so only the
        # process environment reaches VACUUM; the container's /tmp is 16 MiB.
        temporary = os.environ.get("SQLITE_TMPDIR")
        if not temporary or not os.path.isdir(temporary) or not os.access(temporary, os.W_OK):
            raise PurgeError("--vacuum needs SQLITE_TMPDIR set to a writable directory on disk")
    work_ids = load_works(works, provider)
    # Both files are checked before either is changed.
    for path, kind in ((journal, "journal"), (state, "checkpoint")):
        purge_file(path, kind, provider, work_ids, apply=False)
    return {
        "provider": provider,
        "apply": apply,
        "works": len(work_ids),
        "files": [
            purge_file(path, kind, provider, work_ids, apply=apply, vacuum=vacuum)
            for path, kind in ((journal, "journal"), (state, "checkpoint"))
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--works", type=Path, required=True, help="retired_provider_works.py JSON")
    parser.add_argument("--journal", type=Path, required=True, help="provider-work.sqlite3")
    parser.add_argument("--state", type=Path, required=True, help="Full checkpoint state.sqlite3")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--vacuum", action="store_true", help="shrink both files after --apply")
    args = parser.parse_args(argv)
    try:
        result = purge(
            args.provider,
            args.works,
            journal=args.journal,
            state=args.state,
            apply=args.apply,
            vacuum=args.vacuum,
        )
    except PurgeError as exc:
        print(json.dumps({"provider": args.provider, "refused": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
