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
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
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
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
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


def test_wait_without_deliver_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    assert production_backfill_cli.main([*_args(), "--wait"]) == 2
    assert "production_backfill_failed" in capsys.readouterr().err


@pytest.mark.parametrize("dataset,calls", [("tw_equity_minute", 50), ("tw_etf_minute", 3)])
@pytest.mark.parametrize("deliver", [False, True])
def test_shioaji_plan_uses_production_sequences_and_only_creates_after_preview(
    monkeypatch, capsys, dataset, calls, deliver
):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.delenv("FETCHER_SHIOAJI_PRODUCTION_MANIFEST", raising=False)
    monkeypatch.setenv("FINDB_ADMIN_API_URL", "https://findb.example")
    monkeypatch.setenv("FINDB_BACKFILL_ADMIN_API_KEY", "secret")
    requests = []

    def handler(request):
        requests.append(request)
        assert json.loads(request.content)["dataset_key"] == dataset
        if request.url.path.endswith("/preview"):
            return httpx.Response(200, json={"scope_valid": True, "days": [{"valid": True}]})
        return httpx.Response(202, json={"request_id": "shioaji-request", "status": "queued"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        args = [*_args("shioaji", dataset), *(["--deliver"] if deliver else [])]
        assert production_backfill_cli.main(args, client=client) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["estimated_provider_calls"] == calls
    assert output["estimated_rows"] is None
    assert [request.url.path for request in requests] == [
        "/api/v1/admin/historical-backfills/preview",
        *(["/api/v1/admin/historical-backfills"] if deliver else []),
    ]


@pytest.mark.parametrize("failure", ["dataset", "environment", "manifest", "preview"])
def test_shioaji_plan_rejects_invalid_scope_without_creating(monkeypatch, capsys, failure):
    monkeypatch.setenv("APP_ENVIRONMENT", "staging" if failure == "environment" else "production")
    monkeypatch.delenv("FETCHER_SHIOAJI_PRODUCTION_MANIFEST", raising=False)
    if failure == "manifest":
        monkeypatch.setenv(
            "FETCHER_SHIOAJI_PRODUCTION_MANIFEST", str(SCHEDULE.parent / "shioaji_tw_pilot.v1.json")
        )
    monkeypatch.setenv("FINDB_ADMIN_API_URL", "https://findb.example")
    monkeypatch.setenv("FINDB_BACKFILL_ADMIN_API_KEY", "secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"scope_valid": False, "days": []})

    dataset = "tw_equity_eod" if failure == "dataset" else "tw_equity_minute"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert (
            production_backfill_cli.main([*_args("shioaji", dataset), "--deliver"], client=client)
            == 2
        )
    assert len(requests) == (1 if failure == "preview" else 0)
    assert "secret" not in capsys.readouterr().err


def test_taifex_preview_uses_shared_catalog_and_unknown_rows(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "FETCHER_FULL_MARKET_CONFIG", str(SCHEDULE.parent / "full_market.production.v1.json")
    )
    monkeypatch.setenv("FINDB_ADMIN_API_URL", "https://findb.example")
    monkeypatch.setenv("FINDB_BACKFILL_ADMIN_API_KEY", "secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"scope_valid": True, "days": [{"valid": True}, {"valid": True}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert production_backfill_cli.main(_args("taifex", "tw_futures_eod"), client=client) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["estimated_provider_calls"] == 10
    assert output["estimated_rows"] is None
    assert len(requests) == 1 and requests[0].url.path.endswith("/preview")


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "cancelled"])
def test_factset_wait_reports_terminal_gaps_without_polling_until_timeout(
    monkeypatch, capsys, status
):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv(
        "FETCHER_FULL_MARKET_CONFIG", str(SCHEDULE.parent / "full_market.production.v1.json")
    )
    monkeypatch.setenv("FINDB_ADMIN_API_URL", "https://findb.example")
    monkeypatch.setenv("FINDB_BACKFILL_ADMIN_API_KEY", "secret")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/preview"):
            return httpx.Response(200, json={"scope_valid": True, "days": [{"valid": True}]})
        if request.method == "POST":
            return httpx.Response(202, json={"request_id": "factset-request", "status": "queued"})
        return httpx.Response(
            200, json={"data": [{"request_id": "factset-request", "status": status}]}
        )

    def no_sleep(_):
        pytest.fail("terminal request must not keep polling")

    monkeypatch.setattr(production_backfill_cli.time, "sleep", no_sleep)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        code = production_backfill_cli.main(
            [*_args("factset", "hk_equity_eod"), "--deliver", "--wait"], client=client
        )
    assert code == (0 if status == "completed" else 3)
    output = json.loads(capsys.readouterr().out)
    assert output["terminal"]["status"] == status
    assert output["estimated_provider_calls"] == 1 and output["estimated_rows"] is None
    assert len(requests) == 3
