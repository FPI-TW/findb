"""Each Full consumer's checkpoint is split from the host-wide file at release (#691)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from findb_fetcher import checkpoint_split
from findb_fetcher.checkpoint_split import SELECTORS, CheckpointSplitError, split
from findb_fetcher.full_market_state import FullMarketState

CONFIG = Path(__file__).resolve().parents[1] / "configs/bootstrap.local.v1.json"
NOW = 1_795_000_000.0


def _plan(provider: str, day: str) -> dict:
    return {
        "plan_id": f"{provider}-{day}",
        "dataset_key": f"{provider}_eod",
        "trade_date": day,
        "parts": [{"work_item_id": f"{provider}-{day}-part", "member_keys": ["AAA", "BBB"]}],
    }


def _seed(state: FullMarketState, provider: str) -> None:
    for day in ("2026-11-20", "2026-11-21"):
        state.record_plan(_plan(provider, day), provider)
    key = f"{provider}-2026-11-20-part:AAA"
    state.finish(key, status="complete", receipt="run-1", now=NOW)
    state.save_sequence_receipt(key, f"{provider}-idem", "run-1")
    state.record_diagnostic(key, provider=provider, category="source_error", now=NOW)
    state.rate_limited(account=provider, window="2026-11-20", until=NOW + 60)
    state.schedule_save(f"{provider}-2026-11-21", cursor=3, batch={"members": ["BBB"]})
    state.record_universe_refreshed(provider, f"{provider}_eod", now=NOW)
    with state.connection() as db:
        db.execute(
            "INSERT INTO full_cursor VALUES(?,?,?)", (provider, f"{provider}_eod", "2026-11-21")
        )
        db.execute(
            "INSERT INTO full_quota(account,window,requests,bytes) VALUES(?,?,1,0)",
            (provider, "2026-11-20"),
        )
        db.execute(
            "INSERT INTO full_no_data_round VALUES(?,?,?)", (f"{provider}-2026-11-21", 1, NOW)
        )
        db.execute(
            "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound) "
            "VALUES(?,?,?,?,?,?)",
            (f"{provider}-op", provider, provider, NOW, "completed", NOW),
        )
        db.execute(
            "INSERT INTO acquisition_usage(operation_id,observed_at,bytes,limit_bytes,remaining_bytes) "
            "VALUES(?,?,?,?,?)",
            (f"{provider}-op", NOW, 10, 100, 90),
        )


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    path = tmp_path / "full" / "state.sqlite3"
    state = FullMarketState(path)
    state.bind_profile("full_market")
    _seed(state, "finlab")
    _seed(state, "fmp")
    with state.connection() as db:
        db.execute("INSERT INTO account_ledger_retention VALUES(1,?)", (NOW,))
    return path


def _files(folder: Path) -> list[str]:
    """Files besides schema locks and the read-only WAL index of the host-wide file."""
    return sorted(
        path.name
        for path in folder.iterdir()
        if not path.name.endswith(".lock")
        and path.name not in {"state.sqlite3-wal", "state.sqlite3-shm"}
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()


def test_every_checkpoint_table_is_assigned(tmp_path: Path) -> None:
    state = FullMarketState(tmp_path / "state.sqlite3")
    state.bind_profile("full_market")
    with sqlite3.connect(state.path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == set(SELECTORS)


def test_split_copies_only_the_providers_rows_and_never_writes_the_host_wide_file(
    legacy: Path,
) -> None:
    before = _sha(legacy)
    target = legacy.with_name("state-finlab.sqlite3")

    result = split(legacy, target, "finlab")

    assert result["status"] == "split"
    assert _sha(legacy) == before
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert _files(legacy.parent) == ["state-finlab.sqlite3", "state.sqlite3"]
    for table in (
        "full_plan",
        "full_work",
        "full_work_receipt",
        "full_schedule",
        "full_quota",
        "full_account_cooldown",
    ):
        rows = _rows(target, table)
        assert rows and all("fmp" not in repr(row) for row in rows), table
        assert len(rows) == result["rows"][table]
    assert [row[1] for row in _rows(target, "full_work")] == ["finlab"] * 4
    assert _rows(target, "acquisition_usage") == [("finlab-op", NOW, 10, 100, 90, None, None)]
    assert _rows(target, "execution_identity") == [(1, "full_market")]
    assert _rows(target, "account_ledger_retention") == []
    # Copied diagnostics keep their identifiers.
    with sqlite3.connect(legacy) as db:
        expected = db.execute(
            "SELECT * FROM full_work_diagnostic WHERE provider='finlab'"
        ).fetchall()
    assert _rows(target, "full_work_diagnostic") == expected
    # The new file is a complete checkpoint the runtime opens as it is.
    split_state, shared_state = FullMarketState(target), FullMarketState(legacy)
    health = split_state.health("finlab")
    expected_health = shared_state.health("finlab")
    health.pop("observed_at"), expected_health.pop("observed_at")
    assert health == expected_health
    assert split_state.schedule_cursor("finlab-2026-11-21") == 3


def test_a_new_provider_starts_with_an_empty_split(legacy: Path) -> None:
    target = legacy.with_name("state-hkex.sqlite3")

    result = split(legacy, target, "hkex")

    assert result["rows"].pop("execution_identity") == 1
    assert set(result["rows"].values()) == {0}
    assert _rows(target, "full_work") == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("CREATE TABLE full_future (provider TEXT)", "legacy_table_unknown"),
        ("ALTER TABLE full_cursor ADD COLUMN later TEXT", "legacy_column_unknown"),
    ],
)
def test_split_refuses_rows_it_does_not_know_and_leaves_nothing(
    legacy: Path, change: str, reason: str
) -> None:
    with sqlite3.connect(legacy) as db:
        db.execute(change)
    target = legacy.with_name("state-finlab.sqlite3")

    with pytest.raises(CheckpointSplitError) as refused:
        split(legacy, target, "finlab")

    assert refused.value.reason == reason
    assert _files(legacy.parent) == ["state.sqlite3"]


def test_a_copy_that_differs_is_refused(legacy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = checkpoint_split._Digest.row
    calls = {"n": 0}

    def drop_one(self, table, row):
        calls["n"] += 1
        if calls["n"] == 3:
            row = (*row[:-1], "changed")
        original(self, table, row)

    monkeypatch.setattr(checkpoint_split._Digest, "row", drop_one)
    target = legacy.with_name("state-finlab.sqlite3")

    with pytest.raises(CheckpointSplitError) as refused:
        split(legacy, target, "finlab")

    assert refused.value.reason == "copy_mismatch"
    assert not target.exists()


def test_split_never_replaces_an_existing_file(legacy: Path) -> None:
    target = legacy.with_name("state-finlab.sqlite3")
    target.write_bytes(b"retained checkpoint")

    with pytest.raises(CheckpointSplitError) as refused:
        split(legacy, target, "finlab")

    assert refused.value.reason == "target_exists"
    assert target.read_bytes() == b"retained checkpoint"


def test_split_records_where_its_rows_came_from(legacy: Path) -> None:
    target = legacy.with_name("state-finlab.sqlite3")
    result = split(legacy, target, "finlab")

    (origin,) = _rows(target, "checkpoint_origin")
    assert origin[:3] == (1, "finlab", str(legacy))
    assert origin[4] == result["rows_sha256"]
    assert json.loads(origin[5]) == result["rows"]


def test_the_host_wide_file_is_never_its_own_split(legacy: Path) -> None:
    with pytest.raises(CheckpointSplitError) as refused:
        split(legacy, legacy, "finlab")
    assert refused.value.reason == "target_is_legacy"
    with pytest.raises(CheckpointSplitError) as refused:
        split(legacy.with_name("missing.sqlite3"), legacy.with_name("state-x.sqlite3"), "x")
    assert refused.value.reason == "legacy_missing"


def _cli(legacy: Path, target: Path, provider: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "findb_fetcher.full_market_cli",
            "--config",
            str(CONFIG),
            "--provider",
            provider,
            "--execution-profile",
            "full_market",
            "--state-path",
            str(target),
            "--split-checkpoint",
            "--legacy-state-path",
            str(legacy),
        ],
        env={
            "PATH": os.environ["PATH"],
            "APP_ENVIRONMENT": "local",
            "FETCHER_ACCOUNT_STATE_PATH": str(legacy.parent.parent / "account/governor.sqlite3"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_splits_once_and_registers_the_host_lock(legacy: Path) -> None:
    target = legacy.with_name("state-finlab.sqlite3")

    first = _cli(legacy, target, "finlab")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["status"] == "split"
    assert (legacy.parent.parent / "account/provider-work-local-finlab.lock").exists()

    second = _cli(legacy, target, "finlab")
    assert second.returncode == 1
    assert json.loads(second.stdout) == {"status": "refused", "reason": "target_exists"}


def test_cli_needs_both_split_arguments(legacy: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "findb_fetcher.full_market_cli",
            "--provider",
            "finlab",
            "--split-checkpoint",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "--legacy-state-path" in result.stderr


def test_default_checkpoint_is_the_providers_own_file() -> None:
    assert checkpoint_split.provider_path(
        Path("/var/lib/findb-full-market"), "twelve_data"
    ) == Path("/var/lib/findb-full-market/state-twelve_data.sqlite3")
    with pytest.raises(CheckpointSplitError):
        checkpoint_split.provider_path(Path("/x"), "../fmp")
