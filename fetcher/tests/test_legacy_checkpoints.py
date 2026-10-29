"""Read retired checkpoint evidence without executing or rewriting old work."""

import json
import sqlite3
from contextlib import closing

import pytest

from findb_fetcher.legacy_checkpoints import read_taifex_pilot_checkpoint


def old_checkpoint(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta (identity TEXT PRIMARY KEY)")
        db.execute("INSERT INTO meta VALUES ('taifex_tw_staging_pilot_v1')")
        db.execute(
            "CREATE TABLE pilot_day (target_date TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, requests INTEGER NOT NULL DEFAULT 0, prepared TEXT, run_id TEXT, attempt_id TEXT, record_count INTEGER NOT NULL DEFAULT 0, last_outcome TEXT)"
        )
        db.execute(
            "INSERT INTO pilot_day VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "2026-10-02",
                "prepared",
                2,
                4,
                json.dumps({"original": "prepared bytes"}),
                "019a0000-0000-7000-8000-000000000001",
                "019a0000-0000-7000-8000-000000000002",
                4,
                "source_error",
            ),
        )


def test_old_checkpoint_reader_preserves_evidence_inode_and_permissions(tmp_path):
    path = tmp_path / "pilot.sqlite3"
    old_checkpoint(path)
    path.chmod(0o400)
    before = path.read_bytes(), path.stat()
    rows = read_taifex_pilot_checkpoint(path)
    assert len(rows) == 1
    assert (rows[0]["attempts"], rows[0]["requests"], rows[0]["record_count"]) == (2, 4, 4)
    assert rows[0]["run_id"] == "019a0000-0000-7000-8000-000000000001"
    assert rows[0]["attempt_id"] == "019a0000-0000-7000-8000-000000000002"
    assert rows[0]["prepared"] == json.dumps({"original": "prepared bytes"})
    assert rows[0]["last_outcome"] == "source_error"
    assert path.read_bytes() == before[0]
    assert (path.stat().st_ino, path.stat().st_mode) == (before[1].st_ino, before[1].st_mode)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("fault", ["identity", "schema", "corrupt"])
def test_invalid_checkpoint_is_rejected_without_repair(tmp_path, fault):
    path = tmp_path / "pilot.sqlite3"
    old_checkpoint(path)
    if fault == "corrupt":
        path.write_bytes(b"not SQLite")
    else:
        with sqlite3.connect(path) as db:
            if fault == "identity":
                db.execute("UPDATE meta SET identity='wrong'")
            else:
                db.execute("ALTER TABLE pilot_day RENAME COLUMN requests TO obsolete")
    original = path.read_bytes()
    with pytest.raises(ValueError):
        read_taifex_pilot_checkpoint(path)
    assert path.read_bytes() == original


def test_missing_and_symlink_checkpoints_never_create_or_follow_state(tmp_path):
    missing = tmp_path / "missing" / "state.sqlite3"
    with pytest.raises(ValueError):
        read_taifex_pilot_checkpoint(missing)
    assert not missing.parent.exists()
    original = tmp_path / "original.sqlite3"
    old_checkpoint(original)
    alias = tmp_path / "alias.sqlite3"
    alias.symlink_to(original)
    with pytest.raises(ValueError):
        read_taifex_pilot_checkpoint(alias)
    directory = tmp_path / "directory"
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        read_taifex_pilot_checkpoint(directory / original.name)


@pytest.mark.parametrize("live_wal", [False, True])
def test_wal_checkpoint_never_creates_sidecars_or_ignores_uncheckpointed_evidence(
    tmp_path, live_wal
):
    path = tmp_path / "pilot.sqlite3"
    old_checkpoint(path)
    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        if live_wal:
            writer.execute("UPDATE pilot_day SET requests=5")
            writer.commit()
        else:
            writer.close()
        before = {
            file.name: (file.read_bytes(), file.stat().st_ino, file.stat().st_mode)
            for file in tmp_path.iterdir()
        }
        assert (path.with_name(path.name + "-wal").exists()) is live_wal
        with pytest.raises(ValueError, match="WAL checkpoint requires"):
            read_taifex_pilot_checkpoint(path)
        after = {
            file.name: (file.read_bytes(), file.stat().st_ino, file.stat().st_mode)
            for file in tmp_path.iterdir()
        }
        assert after == before
