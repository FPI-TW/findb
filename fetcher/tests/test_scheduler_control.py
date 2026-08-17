from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, time, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from findb_fetcher.config import ConfigError, FetcherConfig
from findb_fetcher.finlab_scheduler_cli import (
    SCHEDULER_CONTROL_KEY as FINLAB_CONTROL_KEY,
)
from findb_fetcher.finlab_scheduler_cli import (
    _run_forever as run_finlab_forever,
)
from findb_fetcher.scheduler_cli import (
    SCHEDULER_CONTROL_KEY as TWELVE_CONTROL_KEY,
)
from findb_fetcher.scheduler_cli import (
    _run_forever as run_twelve_forever,
)
from findb_fetcher.scheduler_control import (
    SchedulerControlClient,
    SchedulerControlLoop,
    SchedulerControlProtocolError,
    SchedulerControlResponse,
    SchedulerControlResponseError,
    SchedulerControlTransportError,
    _log_control_failure,
    validate_scheduler_definition,
)
from findb_fetcher.shioaji_scheduler_cli import (
    SCHEDULER_CONTROL_KEY as SHIOAJI_CONTROL_KEY,
)
from findb_fetcher.shioaji_scheduler_cli import (
    _run_forever as run_shioaji_forever,
)

UTC = timezone.utc


def _config(**overrides: object) -> FetcherConfig:
    values: dict[str, object] = {
        "source_api_url": "https://source.example.test",
        "source_client_key": "source-key",
        "max_attempts": 1,
        "scheduler_control_poll_seconds": 0.01,
    }
    values.update(overrides)
    return FetcherConfig(**values)  # type: ignore[arg-type]


def _response(key: str, desired: str = "running", revision: int = 1) -> dict[str, object]:
    return {
        "success": True,
        "scheduler_key": key,
        "desired_state": desired,
        "revision": revision,
        "server_time": "2026-08-04T00:00:00Z",
    }


def _definition_response(key: str, desired: str = "running") -> dict[str, object]:
    return {
        **_response(key, desired),
        "provider": "twelve_data",
        "dataset_keys": ["us_equity_eod"],
        "slot_id": "western_markets_window",
        "scheduled_local_time": "10:30:00",
        "timezone": "Asia/Taipei",
    }


class _Control:
    scheduler_key = TWELVE_CONTROL_KEY

    def __init__(self, values: list[Any], *, stop_event: threading.Event) -> None:
        self._config = SimpleNamespace(scheduler_control_poll_seconds=0.01)
        self.values = iter(values)
        self.stop_event = stop_event
        self.calls: list[dict[str, Any]] = []

    def poll(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        value = next(self.values)
        if callable(value):
            return value(kwargs, self)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(
            scheduler_key=self.scheduler_key,
            desired_state=value,
            revision=len(self.calls),
            server_time=datetime.now(UTC),
        )


def test_interval_configuration_uses_one_shared_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCE_API_URL", "https://source.example.test")
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "source-key")
    assert FetcherConfig.from_env().scheduler_control_poll_seconds == 30.0
    monkeypatch.setenv("FETCHER_SCHEDULER_CONTROL_POLL_SECONDS", "7.5")
    assert FetcherConfig.from_env().scheduler_control_poll_seconds == 7.5
    for value in ("0", "30.1", "NaN"):
        monkeypatch.setenv("FETCHER_SCHEDULER_CONTROL_POLL_SECONDS", value)
        with pytest.raises(ConfigError):
            FetcherConfig.from_env()


def test_control_client_rejects_protocol_identity_and_sends_safe_payload() -> None:
    key = TWELVE_CONTROL_KEY
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_response("wrong-key"), request=request)

    client = SchedulerControlClient(
        _config(),
        key,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(SchedulerControlProtocolError):
        client.poll(
            observed_state="running",
            cycle_started_at=datetime(2026, 8, 4, tzinfo=UTC),
            last_error="token=super-secret " + "x" * 1000,
        )
    assert requests[0].url.path == f"/api/v1/source/scheduler-controls/{key}/poll"
    assert requests[0].headers["X-API-Key"] == "source-key"
    body = json.loads(requests[0].content)
    assert body["observed_state"] == "running"
    assert body["cycle_started_at"].endswith("Z")
    assert len(body["last_error"]) <= 512
    assert "super-secret" not in body["last_error"]


def test_control_client_parses_authoritative_scheduler_definition() -> None:
    key = TWELVE_CONTROL_KEY
    client = SchedulerControlClient(
        _config(),
        key,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_definition_response(key), request=request)
            )
        ),
    )
    response = client.poll(observed_state="stopped")
    assert response.provider == "twelve_data"
    assert response.dataset_keys == ("us_equity_eod",)
    assert response.slot_id == "western_markets_window"
    assert response.scheduled_local_time == time(10, 30)
    assert response.timezone == "Asia/Taipei"


def test_definition_drift_is_rejected_before_cycle() -> None:
    response = SchedulerControlResponse(
        scheduler_key=TWELVE_CONTROL_KEY,
        provider="twelve_data",
        dataset_keys=("other_dataset",),
        slot_id="western_markets_window",
        scheduled_local_time=time(10, 30),
        timezone="Asia/Taipei",
        desired_state="running",
        revision=1,
        server_time=datetime(2026, 8, 4, tzinfo=UTC),
    )
    with pytest.raises(SchedulerControlProtocolError):
        validate_scheduler_definition(
            response,
            provider="twelve_data",
            dataset_keys=("us_equity_eod",),
            slot_id="western_markets_window",
            scheduled_local_time="10:30:00",
            timezone_name="Asia/Taipei",
        )


def test_disabled_idle_never_constructs_or_invokes_cycle() -> None:
    stop_event = threading.Event()

    def stopped(_kwargs: dict[str, Any], control: _Control) -> Any:
        control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=1,
            server_time=datetime.now(UTC),
        )

    control = _Control([stopped], stop_event=stop_event)
    cycles: list[int] = []
    assert SchedulerControlLoop(control).run(lambda: cycles.append(1), stop_event=stop_event) == 0
    assert cycles == []
    assert [call["observed_state"] for call in control.calls] == ["stopped"]


def test_enabled_cycle_requires_running_preflight_and_reports_completion() -> None:
    stop_event = threading.Event()

    def post(kwargs: dict[str, Any], control: _Control) -> Any:
        if kwargs["cycle_completed_at"] is not None:
            control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="running",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(["running", "running", post], stop_event=stop_event)
    cycles: list[int] = []
    SchedulerControlLoop(control).run(lambda: cycles.append(1), stop_event=stop_event)
    assert cycles == [1]
    assert [call["observed_state"] for call in control.calls] == [
        "stopped",
        "running",
        "stopped",
    ]
    assert control.calls[-1]["cycle_completed_at"] is not None


def test_graceful_stop_mid_cycle_finishes_work_then_stays_stopped() -> None:
    stop_event = threading.Event()
    heartbeat_seen = threading.Event()
    cycle_finished = threading.Event()

    def heartbeat_or_stop(kwargs: dict[str, Any], control: _Control) -> Any:
        if kwargs["observed_state"] == "running":
            heartbeat_seen.set()
            return SimpleNamespace(
                scheduler_key=control.scheduler_key,
                desired_state="stopped",
                revision=len(control.calls),
                server_time=datetime.now(UTC),
            )
        control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(
        ["running", "running", heartbeat_or_stop, heartbeat_or_stop], stop_event=stop_event
    )

    def cycle() -> None:
        assert heartbeat_seen.wait(1)
        cycle_finished.set()

    SchedulerControlLoop(control).run(cycle, stop_event=stop_event)
    assert cycle_finished.is_set()
    assert len(control.calls) >= 4
    assert control.calls[-1]["observed_state"] == "stopped"
    assert control.calls[-1]["cycle_completed_at"] is not None


def test_control_failure_is_fail_closed_and_loop_keeps_polling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = threading.Event()

    def recover(kwargs: dict[str, Any], control: _Control) -> Any:
        control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(
        [
            SchedulerControlTransportError("offline"),
            SchedulerControlTransportError("offline"),
            recover,
        ],
        stop_event=stop_event,
    )
    cycles: list[int] = []
    caplog.set_level(logging.WARNING, logger="findb_fetcher.scheduler_control")
    SchedulerControlLoop(control).run(lambda: cycles.append(1), stop_event=stop_event)
    assert cycles == []
    assert len(control.calls) == 3
    warnings = [
        record for record in caplog.records if record.name == "findb_fetcher.scheduler_control"
    ]
    assert len(warnings) == 2
    assert [record.phase for record in warnings] == ["idle_poll", "idle_poll"]
    assert all(record.failure_class == "transport" for record in warnings)
    assert all("offline" not in record.getMessage() for record in warnings)
    assert all("offline" not in repr(record.args) for record in warnings)


def test_preflight_failure_is_logged_once_and_does_not_start_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = threading.Event()

    def recover(_kwargs: dict[str, Any], control: _Control) -> Any:
        control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(
        ["running", SchedulerControlTransportError("transport sentinel"), recover],
        stop_event=stop_event,
    )
    cycles: list[int] = []
    caplog.set_level(logging.WARNING, logger="findb_fetcher.scheduler_control")
    SchedulerControlLoop(control).run(lambda: cycles.append(1), stop_event=stop_event)

    assert cycles == []
    warnings = [
        record for record in caplog.records if record.name == "findb_fetcher.scheduler_control"
    ]
    assert len(warnings) == 1
    assert warnings[0].phase == "preflight_poll"
    assert warnings[0].failure_class == "transport"
    assert "transport sentinel" not in warnings[0].getMessage()
    assert "transport sentinel" not in repr(warnings[0].args)


def test_completion_report_failure_is_logged_without_replaying_cycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = threading.Event()

    def recover(_kwargs: dict[str, Any], control: _Control) -> Any:
        control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(
        ["running", "running", SchedulerControlResponseError(503), recover],
        stop_event=stop_event,
    )
    cycles: list[int] = []
    caplog.set_level(logging.WARNING, logger="findb_fetcher.scheduler_control")
    SchedulerControlLoop(control).run(lambda: cycles.append(1), stop_event=stop_event)

    assert cycles == [1]
    assert [call["observed_state"] for call in control.calls] == [
        "stopped",
        "running",
        "stopped",
        "stopped",
    ]
    warnings = [
        record for record in caplog.records if record.name == "findb_fetcher.scheduler_control"
    ]
    assert len(warnings) == 1
    assert warnings[0].phase == "completion_report"
    assert warnings[0].failure_class == "http_status"
    assert warnings[0].status_code == 503
    assert warnings[0].status_class == "5xx"


def test_heartbeat_failure_is_logged_once_while_cycle_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = threading.Event()
    heartbeat_failed = threading.Event()
    release_cycle = threading.Event()

    def heartbeat_failure(_kwargs: dict[str, Any], _control: _Control) -> Any:
        heartbeat_failed.set()
        raise SchedulerControlProtocolError("body contains credential=heartbeat-secret")

    def finish(_kwargs: dict[str, Any], control: _Control) -> Any:
        control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(["running", "running", heartbeat_failure, finish], stop_event=stop_event)
    caplog.set_level(logging.WARNING, logger="findb_fetcher.scheduler_control")

    def cycle() -> None:
        assert heartbeat_failed.wait(1)
        release_cycle.set()

    SchedulerControlLoop(control).run(cycle, stop_event=stop_event)
    assert release_cycle.is_set()
    assert len(control.calls) == 4
    assert control.calls[-1]["cycle_completed_at"] is not None
    warnings = [
        record for record in caplog.records if record.name == "findb_fetcher.scheduler_control"
    ]
    assert len(warnings) == 1
    assert warnings[0].phase == "heartbeat"
    assert warnings[0].failure_class == "protocol"
    assert "heartbeat-secret" not in warnings[0].getMessage()
    assert "heartbeat-secret" not in repr(warnings[0].args)


@pytest.mark.parametrize(
    ("phase", "error", "failure_class", "status_code", "status_class"),
    [
        ("idle_poll", SchedulerControlResponseError(503), "http_status", 503, "5xx"),
        (
            "preflight_poll",
            SchedulerControlTransportError("url=https://secret.example"),
            "transport",
            None,
            "none",
        ),
        (
            "completion_report",
            SchedulerControlProtocolError("response body token=secret-value"),
            "protocol",
            None,
            "none",
        ),
        ("heartbeat", SchedulerControlResponseError(429), "http_status", 429, "4xx"),
    ],
)
def test_control_failure_warning_is_structured_and_secret_free(
    caplog: pytest.LogCaptureFixture,
    phase: str,
    error: Exception,
    failure_class: str,
    status_code: int | None,
    status_class: str,
) -> None:
    caplog.set_level(logging.WARNING, logger="findb_fetcher.scheduler_control")
    _log_control_failure(TWELVE_CONTROL_KEY, phase, error)  # type: ignore[arg-type]

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.scheduler_key == TWELVE_CONTROL_KEY
    assert record.phase == phase
    assert record.failure_class == failure_class
    assert record.failure_classification == failure_class
    assert record.status_code == status_code
    assert record.status_class == status_class
    assert "secret-value" not in caplog.text
    assert "https://secret.example" not in caplog.text
    assert "response body" not in caplog.text


def test_cycle_exception_is_bounded_and_reported() -> None:
    stop_event = threading.Event()

    def post(kwargs: dict[str, Any], control: _Control) -> Any:
        if kwargs["cycle_completed_at"] is not None:
            control.stop_event.set()
            assert kwargs["last_error"] is not None
            assert len(kwargs["last_error"]) <= 512
            assert "top-secret" not in kwargs["last_error"]
            assert "access-value" not in kwargs["last_error"]
            assert "credential-value" not in kwargs["last_error"]
            assert "bearer-value" not in kwargs["last_error"]
            assert "authorization-value" not in kwargs["last_error"]
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(["running", "running", post], stop_event=stop_event)
    SchedulerControlLoop(control).run(
        lambda: (_ for _ in ()).throw(
            RuntimeError(
                "token=top-secret access_key=access-value "
                "credential:credential-value Bearer bearer-value "
                "Authorization: Bearer authorization-value " + "x" * 1000
            )
        ),
        stop_event=stop_event,
    )


def test_successful_completion_is_not_repeated_on_later_idle_poll() -> None:
    stop_event = threading.Event()

    def after_completion(kwargs: dict[str, Any], control: _Control) -> Any:
        if len(control.calls) == 4:
            assert kwargs["observed_state"] == "stopped"
            assert kwargs["cycle_completed_at"] is None
            control.stop_event.set()
        return SimpleNamespace(
            scheduler_key=control.scheduler_key,
            desired_state="stopped",
            revision=len(control.calls),
            server_time=datetime.now(UTC),
        )

    control = _Control(
        ["running", "running", "stopped", after_completion],
        stop_event=stop_event,
    )
    SchedulerControlLoop(control).run(lambda: None, stop_event=stop_event)
    assert control.calls[2]["cycle_completed_at"] is not None


@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (run_twelve_forever, TWELVE_CONTROL_KEY),
        (run_finlab_forever, FINLAB_CONTROL_KEY),
        (run_shioaji_forever, SHIOAJI_CONTROL_KEY),
    ],
)
def test_all_production_clis_use_fixed_control_keys(
    monkeypatch: pytest.MonkeyPatch,
    runner: Any,
    expected: str,
) -> None:
    seen: list[str] = []

    class Client:
        def __init__(self, _config: object, key: str) -> None:
            seen.append(key)

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Loop:
        def __init__(self, _client: object) -> None:
            pass

        def run(self, _cycle: Any, *, stop_event: Any = None) -> int:
            return 0

    module = runner.__module__
    imported = __import__(module, fromlist=["SchedulerControlClient", "SchedulerControlLoop"])
    monkeypatch.setattr(imported, "SchedulerControlClient", Client)
    monkeypatch.setattr(imported, "SchedulerControlLoop", Loop)
    assert runner(_config(), lambda: None) == 0
    assert seen == [expected]
