from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from findb_fetcher import execution_metadata, production_backfill_cli

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"


@pytest.fixture(autouse=True)
def descriptor_universes(monkeypatch):
    monkeypatch.setattr(execution_metadata, "CONFIGS_DIR", CONFIGS_DIR)
    monkeypatch.setenv("FETCHER_HISTORICAL_SCHEDULE_FILE", "/absent/daily.json")
    monkeypatch.setenv("FETCHER_BOOTSTRAP_CONFIG", "/absent/full-market.json")


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


def test_taifex_preview_uses_shared_catalog_and_unknown_rows(monkeypatch, capsys):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("FETCHER_BOOTSTRAP_CONFIG", "/absent/full-market.json")
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
    monkeypatch.setenv("FETCHER_BOOTSTRAP_CONFIG", "/absent/full-market.json")
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


@pytest.mark.parametrize(
    "provider,dataset",
    [
        ("twelve_data", "us_equity_eod"),
        ("finlab", "tw_equity_eod"),
        ("factset", "hk_equity_eod"),
        ("taifex", "tw_futures_eod"),
    ],
)
def test_backend_preview_denial_never_creates_request(monkeypatch, capsys, provider, dataset):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("FINDB_ADMIN_API_URL", "https://findb.example")
    monkeypatch.setenv("FINDB_BACKFILL_ADMIN_API_KEY", "secret")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"scope_valid": False, "days": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert (
            production_backfill_cli.main([*_args(provider, dataset), "--deliver"], client=client)
            == 2
        )
    assert [r.url.path for r in calls] == ["/api/v1/admin/historical-backfills/preview"]
    assert "production_backfill_failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "provider,dataset",
    [("twelve_data", "hk_equity_eod"), ("taifex", "us_equity_eod")],
)
def test_planner_cannot_expand_historical_scope(monkeypatch, provider, dataset):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("unsupported scope dispatched"))
    ) as client:
        assert production_backfill_cli.main(_args(provider, dataset), client=client) == 2


def test_planner_estimates_etf_release_cohort_without_a_static_universe(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    # The Backend freezes the ETF Full release; FinLab reads five shared tables per date.
    assert production_backfill_cli._scope("finlab", "tw_etf_eod") == (5, None)


def test_planner_requires_production_and_rejects_retired_schedule_option(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    assert production_backfill_cli.main(_args()) == 2
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    assert production_backfill_cli.main([*_args(), "--schedule-file", "/absent/schedule.json"]) == 2
