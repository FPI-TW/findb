from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from findb_fetcher import production_backfill_cli

SCHEDULE = Path(__file__).resolve().parents[1] / "configs" / "daily_scheduler.production.v3.json"


def _args(provider: str = "twelve_data", dataset: str = "us_equity_eod") -> list[str]:
    return [
        "--provider",
        provider,
        "--dataset-key",
        dataset,
        "--start-date",
        "2026-09-14",
        "--end-date",
        "2026-09-15",
        "--schedule-file",
        str(SCHEDULE),
    ]


def test_dry_run_is_default_and_estimates_calls_without_creating(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEPLOYMENT_TARGET", "production")
    monkeypatch.setenv("FINDB_ADMIN_API_URL", "https://findb.example")
    monkeypatch.setenv("FINDB_BACKFILL_ADMIN_API_KEY", "secret")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "scope_valid": True,
                "days": [
                    {"trade_date": "2026-09-14", "valid": True, "reason": None},
                    {"trade_date": "2026-09-15", "valid": True, "reason": None},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert production_backfill_cli.main(_args(), client=client) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run"
    assert output["estimated_provider_calls"] == 202
    assert output["estimated_rows"] == 202
    assert [request.url.path for request in requests] == [
        "/api/v1/admin/historical-backfills/preview"
    ]


def test_deliver_creates_only_after_successful_preview(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEPLOYMENT_TARGET", "production")
    monkeypatch.setenv("FINDB_ADMIN_API_URL", "https://findb.example")
    monkeypatch.setenv("FINDB_BACKFILL_ADMIN_API_KEY", "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/preview"):
            return httpx.Response(
                200,
                json={
                    "scope_valid": True,
                    "days": [{"trade_date": "2026-09-14", "valid": True, "reason": None}],
                },
            )
        return httpx.Response(202, json={"request_id": "0199-request", "status": "queued"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert (
            production_backfill_cli.main(
                [*_args("finlab", "tw_equity_eod"), "--deliver"], client=client
            )
            == 0
        )

    output = json.loads(capsys.readouterr().out)
    assert output["estimated_provider_calls"] == 5
    assert output["estimated_rows"] == 50
    assert output["created"]["status"] == "queued"


def test_shioaji_history_and_wait_without_deliver_fail_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEPLOYMENT_TARGET", "production")
    assert production_backfill_cli.main([*_args(), "--wait"]) == 2
    assert production_backfill_cli.main(_args("shioaji", "tw_equity_minute")) == 2
    assert "production_backfill_failed" in capsys.readouterr().err
