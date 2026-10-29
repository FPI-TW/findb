"""Read-only inspection of retired checkpoint evidence; never execute old work."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

_TAIFEX_IDENTITY = "taifex_tw_staging_pilot_v1"
_TAIFEX_COLUMNS = [
    "target_date",
    "status",
    "attempts",
    "requests",
    "prepared",
    "run_id",
    "attempt_id",
    "record_count",
    "last_outcome",
]


def read_taifex_pilot_checkpoint(path: Path) -> list[dict[str, Any]]:
    """Validate the original schema/identity and return retained rows unchanged."""
    if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
        raise ValueError("TAIFEX pilot checkpoint must already exist without symlinks")
    # Even mode=ro can create WAL sidecars. The original pilot used rollback
    # journaling; never ignore uncheckpointed WAL evidence via immutable=1.
    with path.open("rb") as original:
        header = original.read(20)
    if len(header) == 20 and (header[18] == 2 or header[19] == 2):
        raise ValueError("WAL checkpoint requires a consistent rollback-format SQLite backup")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        try:
            if db.execute("PRAGMA quick_check").fetchall() != [("ok",)] or db.execute(
                "SELECT identity FROM meta"
            ).fetchall() != [(_TAIFEX_IDENTITY,)]:
                raise ValueError("TAIFEX pilot checkpoint identity invalid")
            columns = [row[1] for row in db.execute("PRAGMA table_info(pilot_day)")]
            if columns != _TAIFEX_COLUMNS:
                raise ValueError("TAIFEX pilot checkpoint schema invalid")
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM pilot_day ORDER BY target_date")]
        except sqlite3.Error:
            raise ValueError("TAIFEX pilot checkpoint schema invalid") from None
