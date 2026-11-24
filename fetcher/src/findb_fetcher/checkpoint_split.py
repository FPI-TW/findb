"""Split one provider's rows out of the host-wide Full checkpoint (#691).

Full consumers used to share ``state.sqlite3``. Each now owns
``state-<provider>.sqlite3`` in the same directory. The release helper splits a
provider while its consumer is stopped and the installed release still runs on
the host-wide file: the provider's rows are copied into a new file, the copy is
compared with the host-wide file table by table, and the row counts and a digest
of the copied rows are recorded in the new file. The host-wide file is opened
read-only and stays as the backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_state import FullMarketState

_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,49}")
_PLANS = "SELECT plan_id FROM full_plan WHERE provider=:provider"
_WORK = f"SELECT key FROM full_work WHERE provider=:provider OR plan_id IN ({_PLANS})"
_INTENTS = "SELECT operation_id FROM acquisition_intent WHERE provider=:provider"
# How each checkpoint table's rows belong to one provider. The runtime records
# the checkpoint's own quota, pacing, cooldown and capacity rows under the
# provider name. ``None`` is never copied: the ledger retention claim belongs to
# the account governor file. A table missing here refuses the split, so a new
# checkpoint table cannot be left behind silently.
SELECTORS: dict[str, str | None] = {
    "full_plan": "provider=:provider",
    "full_work": f"key IN ({_WORK})",
    "full_work_receipt": f"key IN ({_WORK})",
    "full_empty_outcome": f"work_key IN ({_WORK})",
    "full_member_repair": f"work_key IN ({_WORK})",
    "full_work_diagnostic": f"provider=:provider OR key IN ({_WORK})",
    "full_plan_reconcile": f"plan_id IN ({_PLANS})",
    "full_provider_outage": f"plan_id IN ({_PLANS})",
    "full_no_data_trip": f"plan_id IN ({_PLANS})",
    "full_no_data_round": f"plan_id IN ({_PLANS})",
    "full_schedule": f"scope IN ({_PLANS})",
    "full_refresh": "provider=:provider",
    "full_cursor": "provider=:provider",
    "full_date_prerequisite": "provider=:provider",
    "full_universe_refreshed": "provider=:provider",
    "checkpoint_retention": "provider=:provider",
    "full_account_identity": "provider=:provider",
    "full_quota": "account=:provider",
    "full_pacing": "account=:provider",
    "full_account_cooldown": "account=:provider",
    "full_capacity_violation": "account=:provider",
    "full_capacity_repair": "account=:provider",
    "account_alias_audit": "account=:provider",
    "acquisition_intent": "provider=:provider",
    "acquisition_usage": f"operation_id IN ({_INTENTS})",
    "execution_identity": "1",
    "account_ledger_retention": None,
}
ORIGIN_TABLE = "checkpoint_origin"
_BATCH = 500


class CheckpointSplitError(RuntimeError):
    """The split is refused and nothing was written; ``reason`` is a fixed code safe to print."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def provider_path(directory: Path, provider: str) -> Path:
    if _PROVIDER.fullmatch(provider) is None:
        raise CheckpointSplitError("provider_invalid")
    return directory / f"state-{provider}.sqlite3"


def _read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
        raise CheckpointSplitError("legacy_missing")
    db = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None
    )
    db.execute("PRAGMA temp_store=MEMORY")
    # One read transaction: other providers' consumers may still write the file.
    db.execute("BEGIN")
    db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return db


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return sorted(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))


def _plan(legacy: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """``(table, columns)`` copied for every provider, in a fixed order."""
    tables = {
        row[0]
        for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables - SELECTORS.keys():
        raise CheckpointSplitError("legacy_table_unknown")
    return [
        (table, _columns(legacy, table)) for table in sorted(tables) if SELECTORS[table] is not None
    ]


def _rows(
    db: sqlite3.Connection, table: str, columns: list[str], where: str, provider: str
) -> Iterator[tuple[Any, ...]]:
    names = ",".join(f'"{column}"' for column in columns)
    cursor = db.execute(
        f'SELECT {names} FROM "{table}" WHERE {where} ORDER BY rowid', {"provider": provider}
    )
    while batch := cursor.fetchmany(_BATCH):
        yield from batch


class _Digest:
    def __init__(self) -> None:
        self.hash = hashlib.sha256()
        self.counts: dict[str, int] = {}

    def table(self, table: str, columns: list[str]) -> None:
        self.hash.update(f"{table}\0{','.join(columns)}\n".encode())
        self.counts[table] = 0

    def row(self, table: str, row: tuple[Any, ...]) -> None:
        self.hash.update(repr(row).encode() + b"\n")
        self.counts[table] += 1


def _discard(path: Path) -> None:
    for name in (path.name, path.name + "-wal", path.name + "-shm", path.name + "-journal"):
        (path.parent / name).unlink(missing_ok=True)
    # FullMarketState takes its schema lock next to the file it opens.
    for lock in path.parent.glob(path.name + ".*.lock"):
        lock.unlink(missing_ok=True)


def split(legacy: Path, target: Path, provider: str) -> dict[str, Any]:
    """Create ``target`` from the provider's rows of ``legacy``; never writes ``legacy``."""
    if _PROVIDER.fullmatch(provider) is None:
        raise CheckpointSplitError("provider_invalid")
    if target.resolve() == legacy.resolve():
        raise CheckpointSplitError("target_is_legacy")
    if target.exists() or target.is_symlink():
        raise CheckpointSplitError("target_exists")
    temporary = target.with_name(f".{target.name}.split-{os.getpid()}")
    _discard(temporary)
    with closing(_read_only(legacy)) as source:
        plan = _plan(source)
        try:
            FullMarketState(temporary)
            with closing(sqlite3.connect(temporary, timeout=30, isolation_level=None)) as db:
                # The file is renamed into place alone, so it must not keep a WAL.
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                copied = _Digest()
                for table, columns in plan:
                    if not _columns(db, table):
                        # Created on first use (execution_identity); keep its definition.
                        (ddl,) = source.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        ).fetchone()
                        db.execute(ddl)
                    missing = set(columns) - set(_columns(db, table))
                    if missing:
                        raise CheckpointSplitError("legacy_column_unknown")
                    names = ",".join(f'"{column}"' for column in columns)
                    insert = (
                        f'INSERT INTO "{table}"({names}) VALUES({",".join("?" * len(columns))})'
                    )
                    copied.table(table, columns)
                    batch: list[tuple[Any, ...]] = []
                    for row in _rows(source, table, columns, str(SELECTORS[table]), provider):
                        copied.row(table, row)
                        batch.append(row)
                        if len(batch) == _BATCH:
                            db.executemany(insert, batch)
                            batch.clear()
                    db.executemany(insert, batch)
                written = _Digest()
                for table, columns in plan:
                    written.table(table, columns)
                    for row in _rows(db, table, columns, "1", provider):
                        written.row(table, row)
                if (
                    written.counts != copied.counts
                    or written.hash.hexdigest() != copied.hash.hexdigest()
                ):
                    raise CheckpointSplitError("copy_mismatch")
                db.execute(
                    f"CREATE TABLE {ORIGIN_TABLE} (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "provider TEXT NOT NULL, legacy_path TEXT NOT NULL, split_at REAL NOT NULL, "
                    "rows_sha256 TEXT NOT NULL, row_counts TEXT NOT NULL)"
                )
                db.execute(
                    f"INSERT INTO {ORIGIN_TABLE} VALUES(1,?,?,?,?,?)",
                    (
                        provider,
                        str(legacy),
                        time.time(),
                        copied.hash.hexdigest(),
                        json.dumps(copied.counts, sort_keys=True),
                    ),
                )
                db.execute("COMMIT")
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise CheckpointSplitError("target_integrity")
            descriptor = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            _discard(temporary)
    return {
        "status": "split",
        "provider": provider,
        "rows": copied.counts,
        "rows_sha256": copied.hash.hexdigest(),
    }
