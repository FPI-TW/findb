"""Each provider journals its works in its own file; the host-wide file only drains (#691)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from findb_fetcher import provider_work
from findb_fetcher.provider_work import (
    WorkJournal,
    journal_path,
    prune_closed_journal,
    work_journal_path,
)

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()
OLD = "2026-11-12T00:00:00+00:00"
SHARED_WORK = "019a0000-0000-7000-8000-000000000011"
OWN_WORK = "019a0000-0000-7000-8000-000000000012"
OTHER_WORK = "019a0000-0000-7000-8000-000000000013"


class _Source:
    def __init__(self, closed: dict[str, str], work: dict[str, Any] | None = None) -> None:
        self.closed = closed
        self.work = work

    def request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == "provider-work/claim":
            return {"work": self.work}
        assert (method, path) == ("POST", "provider-work/closed")
        return {
            "data": [
                {"work_id": key, "stable_key": key, "state": "completed", "completed_at": at}
                for key, at in self.closed.items()
                if key in body["work_ids"]
            ]
        }


def _journal_work(path: Path, key: str) -> None:
    journal = WorkJournal(path, key)
    journal.freeze({"members": [{"key": "m"}]})
    with journal.connection() as db:
        db.execute(
            "INSERT INTO work_delivery VALUES(?,?,?,?,?,?)",
            (key, "m", b"{}", "0" * 64, "run", None),
        )
        db.execute("INSERT INTO work_response VALUES(?,?,?,?)", (key, "r", b"{}", "0" * 64))


def _keys(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute(f"SELECT work_key FROM {table}")}


def test_each_provider_journals_new_works_in_its_own_file(tmp_path: Path) -> None:
    assert journal_path(tmp_path, "finlab") == tmp_path / "provider-work-finlab.sqlite3"
    assert journal_path(tmp_path) == tmp_path / "provider-work.sqlite3"

    assert work_journal_path("finlab", OWN_WORK, root=tmp_path) == journal_path(tmp_path, "finlab")
    assert work_journal_path("fmp", OTHER_WORK, root=tmp_path) == journal_path(tmp_path, "fmp")
    # Choosing a file never creates the host-wide one.
    assert not journal_path(tmp_path).exists()


@pytest.mark.parametrize("name", ["", "FinLab", "../finlab", "fin lab", "a" * 51])
def test_provider_journal_names_are_bounded(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        journal_path(tmp_path, name)


def test_work_already_in_the_host_wide_journal_keeps_it(tmp_path: Path) -> None:
    shared = journal_path(tmp_path)
    _journal_work(shared, SHARED_WORK)
    # A work that only reserved an attempt budget is journaled there too.
    with WorkJournal(shared, OTHER_WORK).connection() as db:
        db.execute("INSERT INTO work_budget VALUES(?,?,?)", (OTHER_WORK, "op", 1))

    assert work_journal_path("finlab", SHARED_WORK, root=tmp_path) == shared
    assert work_journal_path("finlab", OTHER_WORK, root=tmp_path) == shared
    assert work_journal_path("finlab", OWN_WORK, root=tmp_path) == journal_path(tmp_path, "finlab")


def test_claimed_work_opens_the_work_journal_it_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    opened: list[Path] = []

    class Session:
        settled = True

        def __init__(self, source: Any, provider: str, work: dict, journal: WorkJournal) -> None:
            opened.append(journal.path)

        def publish_cooldown(self) -> None:
            pass

        def stop(self) -> None:
            pass

    monkeypatch.setattr(provider_work, "ProviderWorkSession", Session)
    _journal_work(journal_path(tmp_path), SHARED_WORK)
    for work_id in (SHARED_WORK, OWN_WORK):
        source = _Source({}, {"work_id": work_id})
        with provider_work.claimed_work(source, "finlab", root=tmp_path) as session:
            assert session is not None

    assert opened == [journal_path(tmp_path), journal_path(tmp_path, "finlab")]


def test_retention_covers_the_provider_file_and_the_draining_host_wide_file(
    tmp_path: Path,
) -> None:
    shared, own = journal_path(tmp_path), journal_path(tmp_path, "twelve_data")
    _journal_work(shared, SHARED_WORK)
    _journal_work(own, OWN_WORK)
    source = _Source({SHARED_WORK: OLD, OWN_WORK: OLD})

    removed = prune_closed_journal(source, "twelve_data", root=tmp_path, now=NOW)

    assert removed is not None
    assert {key: removed[key] for key in ("works", "unreceipted", "responses")} == {
        "works": 2,
        "unreceipted": 0,
        "responses": 2,
    }
    assert removed["file_bytes"] >= shared.stat().st_size
    for path in (shared, own):
        assert not _keys(path, "work_delivery") and not _keys(path, "work_manifest")
    # Each file keeps its own once-a-day claim.
    assert prune_closed_journal(source, "twelve_data", root=tmp_path, now=NOW + 60) is None
    with sqlite3.connect(own) as db:
        assert db.execute("SELECT provider FROM work_retention").fetchall() == [("twelve_data",)]


def test_retention_skips_a_provider_without_its_own_file_yet(tmp_path: Path) -> None:
    _journal_work(journal_path(tmp_path), SHARED_WORK)

    removed = prune_closed_journal(_Source({SHARED_WORK: OLD}), "fmp", root=tmp_path, now=NOW)

    assert removed is not None and removed["works"] == 1
    assert not journal_path(tmp_path, "fmp").exists()
