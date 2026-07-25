from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from findb_fetcher import twelve_data_cli
from findb_fetcher.providers.twelve_data import TwelveDataResponseError
from findb_fetcher.twelve_data_universe import SymbolExecution, UniverseExecution

ATTEMPT_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a91")
RUN_ID = UUID("019f98b5-ee0b-7b16-9a28-d8e2ed638a92")


def _request() -> dict[str, Any]:
    return {
        "dataset_key": "us_equity_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": "twelve_data",
        "request_key": "twelve_data:us_equity_eod:AAPL:2024-01-02:2024-01-05:v1",
        "idempotency_key": "twelve_data:us_equity_eod:AAPL:2024-01-02:2024-01-05:v1",
        "fetched_at": "2024-01-06T12:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2024-01-05",
                "delivery_mode": "backfill",
                "declared_record_count": 1,
                "coverage_start_date": "2024-01-02",
                "coverage_end_date": "2024-01-05",
            },
            "data": [{"symbol": "AAPL", "trade_date": "2024-01-05", "close": "185.64"}],
        },
    }


def _args(*extra: str) -> list[str]:
    return ["--symbol", "AAPL", *extra]


class _FakeSourceClient:
    statuses: list[SimpleNamespace] = []
    constructed = 0
    delivered_deadline: float | None = None

    def __init__(self, _config: object, _registry: object) -> None:
        type(self).constructed += 1

    def __enter__(self) -> "_FakeSourceClient":
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def prepare(self, request: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(
            dataset_key=request["dataset_key"],
            schema_id=request["schema_id"],
            schema_version=request["schema_version"],
        )

    def deliver(self, _prepared: object, *, deadline: float | None = None) -> SimpleNamespace:
        type(self).delivered_deadline = deadline
        return SimpleNamespace(
            attempt_id=ATTEMPT_ID,
            run_id=RUN_ID,
            status="queued",
            schema_id="market_eod",
            schema_version=1,
        )

    def get_run_status(
        self,
        _run_id: UUID,
        *,
        expected: object,
        deadline: float,
    ) -> SimpleNamespace:
        del expected, deadline
        return type(self).statuses.pop(0)


@pytest.fixture(autouse=True)
def reset_fake_source() -> None:
    _FakeSourceClient.statuses = []
    _FakeSourceClient.constructed = 0
    _FakeSourceClient.delivered_deadline = None


@pytest.fixture
def stub_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        twelve_data_cli,
        "_fetch_and_validate",
        lambda _args: (_request(), object()),
    )


def test_default_is_bounded_dry_run_without_source_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_fetch: None,
) -> None:
    def unexpected_config() -> object:
        raise AssertionError("dry-run must not load Source delivery configuration")

    monkeypatch.setattr(twelve_data_cli.FetcherConfig, "from_env", unexpected_config)
    monkeypatch.setattr(twelve_data_cli, "SourceAPIClient", _FakeSourceClient)

    assert twelve_data_cli.main(_args()) == twelve_data_cli.EXIT_OK

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["mode"] == "dry_run"
    assert summary["status"] == "validated"
    assert summary["record_count"] == 1
    assert "data" not in summary
    assert len(captured.out.encode()) < twelve_data_cli.MAX_OUTPUT_BYTES
    assert captured.err == ""
    assert _FakeSourceClient.constructed == 0


def test_deliver_reports_attempt_and_run_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_fetch: None,
) -> None:
    monkeypatch.setattr(twelve_data_cli.FetcherConfig, "from_env", lambda: object())
    monkeypatch.setattr(twelve_data_cli, "SourceAPIClient", _FakeSourceClient)

    assert twelve_data_cli.main(_args("--deliver")) == twelve_data_cli.EXIT_OK

    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "deliver"
    assert summary["attempt_id"] == str(ATTEMPT_ID)
    assert summary["run_id"] == str(RUN_ID)
    assert _FakeSourceClient.delivered_deadline is None


def test_wait_polls_until_completed_under_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_fetch: None,
) -> None:
    _FakeSourceClient.statuses = [
        _run_status("queued"),
        _run_status("processing"),
        _run_status("retrying"),
        _run_status("completed"),
    ]
    monkeypatch.setattr(twelve_data_cli.FetcherConfig, "from_env", lambda: object())
    monkeypatch.setattr(twelve_data_cli, "SourceAPIClient", _FakeSourceClient)
    monkeypatch.setattr(twelve_data_cli.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(twelve_data_cli.time, "sleep", lambda _seconds: None)

    result = twelve_data_cli.main(
        _args(
            "--deliver",
            "--wait",
            "--wait-timeout-seconds",
            "10",
            "--poll-interval-seconds",
            "1",
        )
    )

    assert result == twelve_data_cli.EXIT_OK
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed"
    assert summary["run_id"] == str(RUN_ID)
    assert summary["success_records"] == 1
    assert _FakeSourceClient.delivered_deadline == 110.0
    assert _FakeSourceClient.statuses == []


@pytest.mark.parametrize("terminal_status", ["failed", "completed_with_errors"])
def test_terminal_failure_is_nonzero(
    terminal_status: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_fetch: None,
) -> None:
    _FakeSourceClient.statuses = [_run_status(terminal_status, failed_records=1)]
    monkeypatch.setattr(twelve_data_cli.FetcherConfig, "from_env", lambda: object())
    monkeypatch.setattr(twelve_data_cli, "SourceAPIClient", _FakeSourceClient)
    monkeypatch.setattr(twelve_data_cli.time, "monotonic", lambda: 100.0)

    assert (
        twelve_data_cli.main(_args("--deliver", "--wait")) == twelve_data_cli.EXIT_TERMINAL_FAILURE
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == terminal_status
    assert json.loads(captured.err)["error"] == "terminal_failure"


def test_provider_error_does_not_echo_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "provider-secret-value"

    def fail(_args: object) -> tuple[dict[str, Any], object]:
        raise TwelveDataResponseError(f"provider reflected {secret}")

    monkeypatch.setattr(twelve_data_cli, "_fetch_and_validate", fail)

    assert twelve_data_cli.main(_args()) == twelve_data_cli.EXIT_LOCAL_ERROR

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert json.loads(captured.err) == {"error": "local_validation_failed"}


@pytest.mark.parametrize(
    ("error", "extra", "expected_exit", "expected_category"),
    [
        (
            twelve_data_cli.SourceAPIResponseError(
                409,
                "IDEMPOTENCY_PAYLOAD_MISMATCH",
            ),
            ["--deliver"],
            twelve_data_cli.EXIT_SOURCE_REJECTED,
            "source_rejected",
        ),
        (
            twelve_data_cli.SourceAPITransportError("transport-secret"),
            ["--deliver"],
            twelve_data_cli.EXIT_DELIVERY_ERROR,
            "delivery_failed",
        ),
        (
            twelve_data_cli.SourceAPIProtocolError("protocol-secret"),
            ["--deliver"],
            twelve_data_cli.EXIT_DELIVERY_ERROR,
            "delivery_failed",
        ),
        (
            twelve_data_cli.SourceAPIDeadlineExceeded("deadline-secret"),
            ["--deliver", "--wait"],
            twelve_data_cli.EXIT_WAIT_TIMEOUT,
            "wait_timeout",
        ),
    ],
)
def test_delivery_failures_use_safe_exit_and_output(
    error: Exception,
    extra: list[str],
    expected_exit: int,
    expected_category: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_fetch: None,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> int:
        raise error

    monkeypatch.setattr(twelve_data_cli, "_deliver", fail)

    assert twelve_data_cli.main(_args(*extra)) == expected_exit

    captured = capsys.readouterr()
    assert "secret" not in captured.out
    assert "secret" not in captured.err
    assert json.loads(captured.err)["error"] == expected_category


def test_universe_dry_run_is_bounded_and_does_not_load_source_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    execution = UniverseExecution(
        universe_id="twelve_data_us_common_stocks_v1",
        universe_version=1,
        dataset_key="us_equity_eod",
        credits_budget=3,
        credits_used=3,
        records_fetched=12,
        results=tuple(
            SymbolExecution(symbol=symbol, outcome="validated", record_count=4)
            for symbol in ("AAPL", "MSFT", "NVDA")
        ),
    )
    universe = SimpleNamespace(dataset_key="us_equity_eod")

    class ProviderContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            pass

    def unexpected_source_config() -> object:
        raise AssertionError("universe dry-run must not load Source config")

    monkeypatch.setattr(twelve_data_cli, "load_symbol_universe", lambda _path: universe)
    monkeypatch.setattr(twelve_data_cli, "ContractRegistry", lambda _path: object())
    monkeypatch.setattr(twelve_data_cli.TwelveDataConfig, "from_env", lambda: object())
    monkeypatch.setattr(
        twelve_data_cli,
        "TwelveDataClient",
        lambda _config: ProviderContext(),
    )
    monkeypatch.setattr(
        twelve_data_cli.FetcherConfig,
        "from_env",
        unexpected_source_config,
    )
    monkeypatch.setattr(
        twelve_data_cli,
        "execute_twelve_data_universe",
        lambda *_args, **_kwargs: execution,
    )

    result = twelve_data_cli.main(
        [
            "--universe-file",
            "configs/twelve_data_us_common_stocks.v1.json",
            "--start-date",
            "2024-01-02",
            "--end-date",
            "2024-01-06",
        ]
    )

    assert result == twelve_data_cli.EXIT_OK
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["mode"] == "universe_dry_run"
    assert summary["status"] == "completed"
    assert summary["symbols_succeeded"] == 3
    assert summary["credits_used"] == 3
    assert len(summary["results"]) == 3
    assert len(captured.out.encode()) < twelve_data_cli.MAX_OUTPUT_BYTES
    assert captured.err == ""


def test_universe_partial_failure_has_stable_nonzero_exit_and_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    execution = UniverseExecution(
        universe_id="twelve_data_us_common_stocks_v1",
        universe_version=1,
        dataset_key="us_equity_eod",
        credits_budget=3,
        credits_used=2,
        records_fetched=4,
        results=(
            SymbolExecution(symbol="AAPL", outcome="validated", record_count=4),
            SymbolExecution(symbol="MSFT", outcome="provider_rate_limited"),
            SymbolExecution(symbol="NVDA", outcome="not_attempted"),
        ),
    )
    monkeypatch.setattr(twelve_data_cli, "_run_universe", lambda _args: _emit_execution(execution))

    result = twelve_data_cli.main(
        [
            "--universe-file",
            "universe.json",
            "--start-date",
            "2024-01-02",
            "--end-date",
            "2024-01-06",
        ]
    )

    assert result == twelve_data_cli.EXIT_PARTIAL_FAILURE
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "partial_failure"
    assert json.loads(captured.err) == {"error": "partial_failure"}


def test_maximum_universe_delivery_summary_stays_within_output_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    execution = UniverseExecution(
        universe_id="u" * 64,
        universe_version=1,
        dataset_key="us_equity_eod",
        credits_budget=5,
        credits_used=5,
        records_fetched=10_000,
        results=tuple(
            SymbolExecution(
                symbol=f"SYM{index:02d}",
                outcome="completed",
                record_count=5000,
                source_status="completed_with_errors",
                attempt_id=ATTEMPT_ID,
                run_id=RUN_ID,
                total_records=5000,
                success_records=4999,
                failed_records=1,
            )
            for index in range(5)
        ),
    )

    twelve_data_cli._emit_summary(
        twelve_data_cli._universe_summary(
            execution,
            args=SimpleNamespace(deliver=True, wait=True),
        )
    )

    captured = capsys.readouterr()
    assert len(captured.out.encode()) < twelve_data_cli.MAX_OUTPUT_BYTES
    assert captured.err == ""


@pytest.mark.parametrize("option", ["--canonical-symbol", "--exchange"])
def test_universe_rejects_single_symbol_options(option: str) -> None:
    with pytest.raises(SystemExit) as error:
        twelve_data_cli.main(
            [
                "--universe-file",
                "universe.json",
                option,
                "AAPL",
            ]
        )

    assert error.value.code == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["--wait"],
        ["--deliver", "--wait", "--wait-timeout-seconds", "0"],
        ["--deliver", "--wait", "--wait-timeout-seconds", "-1"],
        ["--deliver", "--wait", "--wait-timeout-seconds", "NaN"],
        ["--deliver", "--wait", "--wait-timeout-seconds", "Infinity"],
        ["--deliver", "--wait", "--wait-timeout-seconds", "7201"],
        ["--deliver", "--wait", "--poll-interval-seconds", "0"],
        ["--deliver", "--wait", "--poll-interval-seconds", "61"],
        [
            "--deliver",
            "--wait",
            "--wait-timeout-seconds",
            "1",
            "--poll-interval-seconds",
            "2",
        ],
    ],
)
def test_invalid_wait_arguments_exit_two(extra: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        twelve_data_cli.main(_args(*extra))

    assert error.value.code == 2


def _run_status(status: str, *, failed_records: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=RUN_ID,
        status=status,
        total_records=1,
        success_records=1 - failed_records,
        failed_records=failed_records,
        failure_code="NORMALIZATION_FAILED" if failed_records else None,
    )


def _emit_execution(execution: UniverseExecution) -> int:
    args = SimpleNamespace(deliver=False, wait=False)
    twelve_data_cli._emit_summary(twelve_data_cli._universe_summary(execution, args=args))
    twelve_data_cli._emit_error("partial_failure")
    return twelve_data_cli.EXIT_PARTIAL_FAILURE
