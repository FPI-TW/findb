"""Shioaji time limits have explicit clocks; no functional SDK/ASGI latency."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("elapsed,allowed", [(0.049, True), (0.050, False)])
def test_dispatch_grant_expires_at_50ms(monkeypatch, elapsed, allowed):
    from findb_fetcher import acquisition

    monkeypatch.setattr(acquisition, "time", SimpleNamespace(monotonic=lambda: 100 + elapsed))
    grant = acquisition.Authorization(100, 30, 30)
    assert grant.valid() is allowed


@pytest.mark.parametrize("elapsed,allowed", [(59.999, False), (60.001, True)])
def test_checkpoint_rolling_50_request_limit_uses_explicit_utc_clock(tmp_path, elapsed, allowed):
    from findb_fetcher.shioaji_scheduler import load_manifest
    from findb_fetcher.shioaji_staging_state import ShioajiStagingState

    manifest = load_manifest(
        Path(__file__).parents[2] / "fetcher/configs/shioaji_tw50_2026_09_21.v2.json"
    )
    rows = [row for row in manifest["sequences"] if row["dataset_key"] == "tw_equity_minute"]
    now = datetime(2026, 10, 28, 12, 0, 30, tzinfo=timezone.utc)
    state = ShioajiStagingState(tmp_path / "rolling-limit.sqlite3")
    try:
        state.ensure(
            "time-limit", "2026-10-22", manifest["universe_id"], "daily", rows, "2026-10-28"
        )
        for row in rows:
            ok, attempt = state.acquire_attempt(
                "time-limit",
                row["symbol"],
                now,
                max_requests=50,
                rolling_seconds=60,
                max_attempts=3,
            )
            assert ok and attempt == 1
            state.finish_attempt(
                "time-limit", row["symbol"], attempt, "failed", "ACQUISITION_FAILED", retryable=True
            )
        ok, _ = state.acquire_attempt(
            "time-limit",
            rows[0]["symbol"],
            now + timedelta(seconds=elapsed),
            max_requests=50,
            rolling_seconds=60,
            max_attempts=3,
        )
        assert ok is allowed
    finally:
        state.close()
