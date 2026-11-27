"""Purging a retired provider's local rows leaves every other provider untouched (#574)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.provider_work import WorkJournal
from findb_fetcher.retired_provider_purge import PurgeError, main, purge, work_ids_digest

RETIRED = ["01a0d000-0000-7000-8000-000000000001", "01a0d000-0000-7000-8000-000000000002"]
KEPT = "01a0d000-0000-7000-8000-0000000000ff"
BLOB = b"x" * 200_000


def _journal(path: Path) -> Path:
    WorkJournal(path, KEPT)
    db = sqlite3.connect(path)
    with db:
        for key, provider in zip((*RETIRED, KEPT), ("shioaji", "shioaji", "finlab")):
            db.execute(
                "INSERT INTO work_manifest VALUES(?,?)", (key, json.dumps({"members": [provider]}))
            )
            for member in range(3):
                db.execute(
                    "INSERT INTO work_delivery VALUES(?,?,?,?,?,NULL)",
                    (key, f"m{member}", BLOB, "sha", "receipt" if member else None),
                )
            db.execute("INSERT INTO work_response VALUES(?,?,?,?)", (key, "r", BLOB, "sha"))
            db.execute(
                "INSERT INTO work_response_observation VALUES(?,?,?,?,?,?)",
                (key, "r", 1.0, "native", b"o", "sha"),
            )
            db.execute("INSERT INTO work_budget VALUES(?,?,?)", (key, "op", 1))
        # Grants and probes of works Backend no longer lists still belong to the provider;
        # journals written before the Shioaji runtime was removed still hold these tables.
        db.executescript(
            "CREATE TABLE work_shioaji_grant(work_key TEXT NOT NULL, operation_key TEXT NOT NULL, "
            "grant_id TEXT NOT NULL, maximum INTEGER NOT NULL, PRIMARY KEY(work_key,operation_key,grant_id));"
            "CREATE TABLE work_shioaji_probe(probe_id INTEGER PRIMARY KEY, work_key TEXT NOT NULL, "
            "operation_key TEXT NOT NULL, grant_id TEXT NOT NULL, reserved_at REAL NOT NULL, "
            "outcome TEXT NOT NULL, diagnostic TEXT NOT NULL);"
        )
        db.execute("INSERT INTO work_shioaji_grant VALUES(?,?,?,?)", ("gone", "op", "g", 1))
        db.execute(
            "INSERT INTO work_shioaji_probe(work_key,operation_key,grant_id,reserved_at,outcome,diagnostic) "
            "VALUES(?,?,?,?,?,?)",
            ("gone", "op", "g", 1.0, "ok", "{}"),
        )
    db.close()
    return path


def _checkpoint(path: Path) -> Path:
    FullMarketState(path)
    db = sqlite3.connect(path)
    with db:
        for plan_id, provider in (("p-retired", "shioaji"), ("p-kept", "finlab")):
            db.execute(
                "INSERT INTO full_plan VALUES(?,?,?,?,?,0,0)",
                (plan_id, provider, "d", "2026-10-13", b"{}"),
            )
            db.execute("INSERT INTO full_plan_reconcile VALUES(?,1)", (plan_id,))
            db.execute("INSERT INTO full_schedule VALUES(?,0,?)", (plan_id, b"batch"))
            for member in range(4):
                key = f"{plan_id}:part:{member}"
                db.execute(
                    "INSERT INTO full_work(key,provider,plan_id,member_key,status,body) VALUES(?,?,?,?,?,?)",
                    (key, provider, plan_id, str(member), "pending", BLOB if member == 0 else None),
                )
                db.execute("INSERT INTO full_work_receipt VALUES(?,?,?)", (key, "i", "run"))
                db.execute(
                    "INSERT INTO full_work_diagnostic(key,provider,category,observed_at) VALUES(?,?,?,?)",
                    (key, provider, "source_error", 1.0),
                )
            db.execute("INSERT INTO full_refresh VALUES(?,?,?)", (provider, 1.0, b"[]"))
        # Account state is never refunded or reset, whichever provider used it.
        db.execute("INSERT INTO full_quota VALUES('shioaji:acct','2026-10-13',5,10,0)")
        db.execute(
            "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound) "
            "VALUES('op','shioaji:acct','shioaji',1.0,'settled',0)"
        )
    db.close()
    return path


def _works(tmp_path: Path, provider="shioaji", ids=RETIRED, digest=None) -> Path:
    path = tmp_path / "works.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": provider,
                "environment": "local",
                "work_ids": list(ids),
                "sha256": digest or work_ids_digest(ids),
            }
        )
    )
    return path


@pytest.fixture
def files(tmp_path):
    return _journal(tmp_path / "provider-work.sqlite3"), _checkpoint(tmp_path / "state.sqlite3")


def _rows(path: Path) -> dict[str, list]:
    db = sqlite3.connect(path)
    try:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: sorted(map(repr, db.execute(f'SELECT * FROM "{t}"'))) for t in tables}
    finally:
        db.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_digest_matches_backend_for_any_order():
    assert work_ids_digest(list(reversed(RETIRED))) == work_ids_digest(RETIRED)
    assert (
        work_ids_digest(RETIRED)
        == hashlib.sha256("".join(f"{i}\n" for i in sorted(RETIRED)).encode()).hexdigest()
    )


def test_dry_run_reports_every_table_and_writes_nothing(tmp_path, files):
    journal, state = files
    before = (_digest(journal), _digest(state))

    report = purge("shioaji", _works(tmp_path), journal=journal, state=state, apply=False)

    assert (_digest(journal), _digest(state)) == before
    journal_report, checkpoint_report = report["files"]
    assert journal_report["tables"]["work_delivery"] == {
        "rows": 6,
        "bytes": 6 * len(BLOB),
        "kept": 3,
    }
    assert journal_report["tables"]["work_shioaji_grant"]["rows"] == 1
    assert journal_report["tables"]["work_shioaji_probe"]["rows"] == 1
    # Deliveries without a receipt are counted before they go.
    assert journal_report["unreceipted"] == 2
    assert checkpoint_report["tables"]["full_work"] == {"rows": 4, "bytes": len(BLOB), "kept": 4}
    assert checkpoint_report["tables"]["full_plan"]["rows"] == 1
    assert checkpoint_report["tables"]["full_work_diagnostic"] == {"rows": 4, "bytes": 0, "kept": 4}
    assert "after" not in journal_report


def test_apply_removes_only_the_retired_provider_and_vacuum_shrinks(tmp_path, files, monkeypatch):
    journal, state = files
    monkeypatch.setenv("SQLITE_TMPDIR", str(tmp_path))
    before_journal, before_state = _rows(journal), _rows(state)
    sizes = (journal.stat().st_size, state.stat().st_size)
    inodes = (journal.stat().st_ino, state.stat().st_ino)

    report = purge(
        "shioaji", _works(tmp_path), journal=journal, state=state, apply=True, vacuum=True
    )

    after_journal, after_state = _rows(journal), _rows(state)
    for table in ("work_manifest", "work_delivery", "work_response", "work_budget"):
        assert after_journal[table] and all(KEPT in row for row in after_journal[table])
    assert after_journal["work_shioaji_grant"] == after_journal["work_shioaji_probe"] == []
    for table in ("full_plan", "full_work", "full_work_receipt", "full_work_diagnostic"):
        assert after_state[table] and all("p-retired" not in row for row in after_state[table])
    assert all("shioaji" not in row for row in after_state["full_refresh"])
    # Account history is the same rows as before.
    for table in ("full_quota", "acquisition_intent"):
        assert after_state[table] == before_state[table]
    assert len(after_journal["work_delivery"]) == len(before_journal["work_delivery"]) - 6
    assert journal.stat().st_size < sizes[0] and state.stat().st_size < sizes[1]
    assert (journal.stat().st_ino, state.stat().st_ino) == inodes
    for entry in report["files"]:
        assert entry["after"]["file_bytes"] < entry["before"]["file_bytes"]


@pytest.mark.parametrize(
    "provider,works,message",
    [
        ("finlab", {"provider": "finlab"}, "still packaged"),
        ("shioaji", {"provider": "twelve_data"}, "does not list"),
        ("shioaji", {"digest": "0" * 64}, "digest"),
        ("shioaji", {"ids": ["not-a-uuid"]}, "does not list"),
    ],
)
def test_refuses_anything_but_backend_listed_work_of_a_retired_provider(
    tmp_path, files, provider, works, message
):
    journal, state = files
    before = (_digest(journal), _digest(state))

    with pytest.raises(PurgeError, match=message):
        purge(provider, _works(tmp_path, **works), journal=journal, state=state, apply=True)
    assert (_digest(journal), _digest(state)) == before


def test_refuses_a_listed_work_whose_manifest_names_another_provider(tmp_path, files):
    journal, state = files
    db = sqlite3.connect(journal)
    with db:
        db.execute(
            "UPDATE work_manifest SET body=? WHERE work_key=?",
            (json.dumps({"provider": "finlab"}), RETIRED[0]),
        )
    db.close()

    with pytest.raises(PurgeError, match="another provider"):
        purge("shioaji", _works(tmp_path), journal=journal, state=state, apply=True)


def test_refuses_while_another_connection_writes(tmp_path, files, monkeypatch):
    journal, state = files
    # The purge waits this long for the writer before refusing.
    monkeypatch.setattr("findb_fetcher.retired_provider_purge._WRITER_TIMEOUT_SECONDS", 0.1)
    writer = sqlite3.connect(journal, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(PurgeError, match="in use"):
            purge("shioaji", _works(tmp_path), journal=journal, state=state, apply=True)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert len(_rows(journal)["work_delivery"]) == 9


def test_vacuum_needs_apply(tmp_path, files):
    journal, state = files
    with pytest.raises(PurgeError, match="--apply"):
        purge("shioaji", _works(tmp_path), journal=journal, state=state, apply=False, vacuum=True)


def test_the_command_prints_one_refusal_and_exits_non_zero(tmp_path, files, capsys):
    journal, state = files
    code = main(
        [
            "--provider",
            "finlab",
            "--works",
            str(_works(tmp_path, provider="finlab")),
            "--journal",
            str(journal),
            "--state",
            str(state),
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().err)["refused"].startswith("provider is still packaged")


def test_the_command_reports_a_dry_run_without_content(tmp_path, files, capsys):
    journal, state = files
    assert (
        main(
            ["--provider", "shioaji", "--works", str(_works(tmp_path)), "--journal", str(journal)]
            + ["--state", str(state)]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert json.loads(out)["apply"] is False
    assert "xxxx" not in out and str(uuid.UUID(RETIRED[0])) not in out


@pytest.mark.parametrize("temporary", [None, "missing"])
def test_vacuum_without_a_disk_temp_directory_is_refused_before_any_delete(
    tmp_path, files, monkeypatch, temporary
):
    # SQLite chooses its temporary directory when the module loads; only the process
    # environment reaches VACUUM, so the purge cannot fix it up itself.
    journal, state = files
    if temporary is None:
        monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
    else:
        monkeypatch.setenv("SQLITE_TMPDIR", str(tmp_path / temporary))
    before = (_digest(journal), _digest(state))

    with pytest.raises(PurgeError, match="SQLITE_TMPDIR"):
        purge("shioaji", _works(tmp_path), journal=journal, state=state, apply=True, vacuum=True)
    assert (_digest(journal), _digest(state)) == before
