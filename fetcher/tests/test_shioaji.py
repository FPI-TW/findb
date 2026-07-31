from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.providers import shioaji
from findb_fetcher.providers.shioaji import (
    IsolatedShioajiGateway,
    ShioajiPayloadError,
    ShioajiSdkError,
    ShioajiSdkGateway,
    build_market_minute_request,
)


def _ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    # ts is intentionally a Taipei wall-clock epoch, not a UTC epoch.
    return int(
        (datetime(year, month, day, hour, minute) - datetime(1970, 1, 1)).total_seconds()
        * 1_000_000_000
    )


def _kbars(volume: object = 2, amount: object = 1234) -> dict[str, list[object]]:
    return {
        "ts": [_ns(2026, 7, 29, 9, 1)],
        "Open": [100],
        "High": [101],
        "Low": [99],
        "Close": [100.5],
        "Volume": [volume],
        "Amount": [amount],
    }


def _request(**kwargs: object) -> dict[str, object]:
    values: dict[str, object] = {
        "kbars": {key: tuple(value) for key, value in _kbars().items()},
        "dataset_key": "tw_equity_minute",
        "target_date": date(2026, 7, 29),
        "symbols": ("2330",),
        "fetched_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
        "usage_before_requests": 7,
        "usage_after_requests": 8,
        "snapshot_id": "snapshot-1",
        "daily_update_id": "update-1",
        "universe_id": "tw-staging",
    }
    values.update(kwargs)
    return build_market_minute_request(**values)  # type: ignore[arg-type]


def test_maps_taipei_wall_clock_right_label_and_contract() -> None:
    request = _request()
    row = request["payload"]["data"][0]  # type: ignore[index]
    assert row["bar_start_time"] == "2026-07-29T01:00:00Z"
    assert row["bar_end_time"] == row["signal_time"] == "2026-07-29T01:01:00Z"
    assert row["volume"] == 2000
    assert row["turnover"] == "1234"
    assert request["payload"]["batch"]["provider_usage_before"] == {
        "requests_used": 7,
        "requests_limit": 50,
    }  # type: ignore[index]
    assert request["payload"]["batch"]["provider_usage_after"] == {
        "requests_used": 8,
        "requests_limit": 50,
    }  # type: ignore[index]
    digest = hashlib.sha256(
        json.dumps(
            {
                "data_date": "2026-07-29",
                "dataset_key": "tw_equity_minute",
                "sequence": 1,
                "snapshot_id": "snapshot-1",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert (
        request["request_key"] == f"mmr:{digest}" and request["idempotency_key"] == f"mms:{digest}"
    )
    assert request["payload"]["batch"]["symbols_sha256"] == hashlib.sha256(b"2330").hexdigest()  # type: ignore[index]
    ContractRegistry(Path(__file__).resolve().parents[2] / "contracts").validate(
        "market_minute", 1, request
    )


def test_identity_is_deterministic_and_volume_amount_anomalies_are_bidirectional() -> None:
    first = _request(
        kbars={key: tuple(value) for key, value in _kbars(volume="bad", amount="-1").items()}
    )
    second = _request(
        kbars={key: tuple(value) for key, value in _kbars(volume="bad", amount="-1").items()}
    )
    assert first["idempotency_key"] == second["idempotency_key"]
    row = first["payload"]["data"][0]  # type: ignore[index]
    assert row["volume"] is None and row["turnover"] is None
    anomalies = first["payload"]["batch"]["anomalies"]  # type: ignore[index]
    assert [item["field"] for item in anomalies] == ["volume", "turnover"]
    assert all(len(item["raw_value_sha256"]) == 64 for item in anomalies)


def test_rejects_non_monotonic_usage_and_known_close_auction_is_right_labelled() -> None:
    with pytest.raises(ShioajiPayloadError):
        _request(usage_before_requests=2, usage_after_requests=1)
    with pytest.raises(ShioajiPayloadError):
        _request(sequence=100_000, sequence_count=100_000)
    auction = _request(
        kbars={
            key: tuple(value)
            for key, value in {**_kbars(), "ts": [_ns(2026, 7, 29, 13, 30)]}.items()
        }
    )
    row = auction["payload"]["data"][0]  # type: ignore[index]
    assert row["bar_start_time"] == "2026-07-29T05:29:00Z"
    assert row["bar_end_time"] == "2026-07-29T05:30:00Z"


def test_rejects_ohlc_cross_field_violation() -> None:
    invalid = {key: tuple(value) for key, value in _kbars().items()}
    invalid["High"] = (99,)
    with pytest.raises(ShioajiPayloadError, match="OHLC"):
        _request(kbars=invalid)


def test_sdk_shape_failures_are_terminal_payload_errors() -> None:
    with pytest.raises(ShioajiSdkError) as caught:
        shioaji._plain_kbars({"ts": ()})
    assert caught.value.code == "PAYLOAD"
    with pytest.raises(ShioajiSdkError) as scalar:
        shioaji._normalize_scalar(float("nan"))
    assert scalar.value.reason == "kbars_scalar"


def test_plain_kbars_canonicalizes_decimal_and_rejects_non_json_scalars() -> None:
    raw = _kbars()
    raw["Open"] = [Decimal("100.500")]
    plain = shioaji._plain_kbars(raw)
    assert plain["Open"] == ("100.5",)
    assert json.loads(json.dumps({key: list(value) for key, value in plain.items()}))["Open"] == [
        "100.5"
    ]
    raw["Close"] = [object()]
    with pytest.raises(ShioajiSdkError) as caught:
        shioaji._plain_kbars(raw)
    assert caught.value.reason == "kbars_scalar"


def test_isolated_ipc_fails_closed_for_unknown_reason_and_bad_success_scalar() -> None:
    for raw in (
        b'{"code":"PAYLOAD","stage":"payload","reason":"secret"}',
        b'{"code":"PAYLOAD","stage":"payload","reason":[]}',
        b'{"code":[],"stage":"payload"}',
    ):
        with pytest.raises(ShioajiSdkError) as unknown:
            shioaji._decode_isolated_message(raw)
        assert unknown.value.reason == "ipc_schema"
    bad = {"code": "OK", "stage": "payload", "kbars": _kbars(), "before": None, "after": None}
    bad["kbars"]["ts"] = [True]
    with pytest.raises(ShioajiSdkError) as scalar:
        shioaji._decode_isolated_message(json.dumps(bad).encode())
    assert scalar.value.reason == "ipc_schema"
    bad["kbars"] = {**_kbars(), "provider_extra": [1]}
    with pytest.raises(ShioajiSdkError) as shape:
        shioaji._decode_isolated_message(json.dumps(bad).encode())
    assert shape.value.reason == "ipc_schema"


def test_static_ipc_error_wires_are_bounded_and_secret_free() -> None:
    for reason in ("ipc_encode", "ipc_size"):
        wire = shioaji._isolated_error_wire("PAYLOAD", reason)
        assert len(wire) <= shioaji.MAX_ISOLATED_IPC_BYTES
        assert shioaji._decode_isolated_message(wire) == {"code": "PAYLOAD", "reason": reason}
        assert "secret" not in wire.decode()


def test_bounded_success_wire_uses_static_encode_and_size_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message: dict[str, object] = {
        "code": "OK",
        "stage": "payload",
        "kbars": _kbars(),
        "before": None,
        "after": None,
    }
    exact = shioaji._encode_isolated_success(message)
    monkeypatch.setattr(shioaji, "MAX_ISOLATED_IPC_BYTES", len(exact))
    assert shioaji._bounded_isolated_success_wire(message) == exact
    monkeypatch.setattr(shioaji, "MAX_ISOLATED_IPC_BYTES", len(exact) - 1)
    assert shioaji._decode_isolated_message(shioaji._bounded_isolated_success_wire(message)) == {
        "code": "PAYLOAD",
        "reason": "ipc_size",
    }

    def fail(_message: object) -> bytes:
        raise TypeError("secret provider scalar")

    monkeypatch.setattr(shioaji, "_encode_isolated_success", fail)
    wire = shioaji._bounded_isolated_success_wire(message)
    assert b"secret" not in wire
    assert shioaji._decode_isolated_message(wire) == {
        "code": "PAYLOAD",
        "reason": "ipc_encode",
    }


def test_success_wire_encodes_the_exact_complete_message_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message: dict[str, object] = {
        "code": "OK",
        "stage": "payload",
        "kbars": {},
        "before": None,
        "after": None,
    }
    original = shioaji.json.dumps
    calls: list[object] = []

    def encode(value: object, **kwargs: object) -> str:
        calls.append(value)
        return original(value, **kwargs)

    monkeypatch.setattr(shioaji.json, "dumps", encode)
    wire = shioaji._encode_isolated_success(message)
    assert calls == [message]
    assert wire == original(message, separators=(",", ":"), allow_nan=False).encode()


def test_isolated_gateway_silences_child_and_returns_coarse_credentials_code(
    capfd: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(ShioajiSdkError) as caught:
        IsolatedShioajiGateway("", "", timeout_seconds=5).fetch_kbars(
            "2330",
            date(2026, 7, 29),
        )
    output = capfd.readouterr()
    assert caught.value.code == "CREDENTIALS"
    assert output.out == output.err == ""


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        json.dumps({"code": "UNKNOWN", "stage": "login"}).encode(),
        json.dumps({"code": "LOGIN", "stage": "payload"}).encode(),
        json.dumps({"code": "OK", "stage": "payload"}).encode(),
        b"x" * (shioaji.MAX_ISOLATED_IPC_BYTES + 1),
    ],
)
def test_isolated_gateway_ipc_rejects_malformed_or_unreviewed_messages(raw: bytes) -> None:
    with pytest.raises(ShioajiSdkError) as caught:
        shioaji._decode_isolated_message(raw)
    assert caught.value.code == "PAYLOAD"


def test_isolated_gateway_ipc_accepts_only_exact_coarse_error_stage() -> None:
    assert shioaji._decode_isolated_message(b'{"code":"LOGIN","stage":"login"}') == {
        "code": "LOGIN"
    }
    assert shioaji._isolated_error_message("UNKNOWN") == {
        "code": "PAYLOAD",
        "stage": "payload",
        "reason": "child_boundary",
    }


@pytest.mark.parametrize("transport_error", [EOFError(), OSError(), ValueError()])
def test_isolated_gateway_transport_failure_is_terminal_payload(
    monkeypatch: pytest.MonkeyPatch,
    transport_error: Exception,
) -> None:
    class Connection:
        def poll(self, _timeout: float) -> bool:
            return True

        def recv_bytes(self, _max_length: int) -> bytes:
            raise transport_error

        def close(self) -> None:
            pass

    class Process:
        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def join(self, _timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(
        shioaji.multiprocessing,
        "Pipe",
        lambda **_: (Connection(), Connection()),
    )
    monkeypatch.setattr(shioaji.multiprocessing, "Process", lambda **_: Process())
    with pytest.raises(ShioajiSdkError) as caught:
        IsolatedShioajiGateway("key", "secret").fetch_kbars(
            "2330",
            date(2026, 7, 29),
        )
    assert caught.value.code == "PAYLOAD"
    assert caught.value.reason == "ipc_transport"


def test_isolated_gateway_timeout_terminates_then_kills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Connection:
        def poll(self, _timeout: float) -> bool:
            return False

        def close(self) -> None:
            events.append("close")

    class Process:
        alive = True

        def start(self) -> None:
            events.append("start")

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            events.append("terminate")

        def kill(self) -> None:
            events.append("kill")
            self.alive = False

        def join(self, _timeout: float | None = None) -> None:
            events.append("join")

    parent = Connection()
    child = Connection()
    process = Process()
    monkeypatch.setattr(shioaji.multiprocessing, "Pipe", lambda **_: (parent, child))
    monkeypatch.setattr(shioaji.multiprocessing, "Process", lambda **_: process)
    with pytest.raises(ShioajiSdkError, match="timed out"):
        IsolatedShioajiGateway("key", "secret", timeout_seconds=0.01).fetch_kbars(
            "2330",
            date(2026, 7, 29),
        )
    assert "terminate" in events and "kill" in events


def test_normalizes_numpy_like_scalars_and_keeps_usage_bytes_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Scalar:
        def __init__(self, value: object) -> None:
            self.value = value

        def item(self) -> object:
            return self.value

    plain = shioaji._plain_kbars(
        {key: [Scalar(value) for value in values] for key, values in _kbars().items()}
    )
    assert type(plain["ts"][0]) is int and type(plain["Open"][0]) is int
    events: list[str] = []

    class Stocks:
        def get(self, _code: str) -> str:
            return "contract"

    class Contracts:
        stocks = Stocks()

    class KbarsResult:
        def dict(self) -> dict[str, list[object]]:
            return _kbars()

    class Api:
        contracts = Contracts()

        def login(self, **_kwargs: object) -> None:
            events.append("login")

        def usage(self) -> dict[str, int]:
            events.append("usage")
            return {"bytes": 100 + len(events)}

        def kbars(self, _contract: str, **_kwargs: object) -> KbarsResult:
            events.append("kbars")
            return KbarsResult()

        def logout(self) -> None:
            events.append("logout")

    class Sdk:
        Shioaji = staticmethod(lambda **_: Api())

    monkeypatch.setattr(
        "findb_fetcher.providers.shioaji.importlib.metadata.version", lambda _: "1.7.1"
    )
    snapshot = ShioajiSdkGateway("key", "secret", _sdk=Sdk()).fetch_kbars("2330", date(2026, 7, 29))
    assert events == ["login", "usage", "kbars", "usage", "logout"]
    assert snapshot.usage_bytes_delta == 2
    request = _request(kbars=snapshot.kbars, usage_before_requests=0, usage_after_requests=1)
    assert request["payload"]["batch"]["provider_usage_after"]["requests_used"] == 1  # type: ignore[index]


def test_gateway_uses_lowercase_contracts_and_always_logs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Stocks:
        def get(self, code: str) -> str:
            events.append(("get", code))
            return "contract"

    class Contracts:
        stocks = Stocks()

    class Api:
        contracts = Contracts()

        def login(self, **kwargs: object) -> None:
            events.append(("login", kwargs))

        def kbars(self, contract: str, **kwargs: object) -> dict[str, list[object]]:
            events.append(("kbars", contract, kwargs))
            raise RuntimeError("secret")

        def logout(self) -> None:
            events.append("logout")

    class Sdk:
        @staticmethod
        def _factory(*, simulation: bool) -> Api:
            events.append(("factory", simulation))
            return Api()

        Shioaji = _factory

    gateway = ShioajiSdkGateway("api-secret", "key-secret", _sdk=Sdk())
    monkeypatch.setattr(
        "findb_fetcher.providers.shioaji.importlib.metadata.version", lambda _: "1.7.1"
    )
    assert "api-secret" not in repr(gateway) and "key-secret" not in repr(gateway)
    with pytest.raises(Exception) as exc:
        gateway.fetch_kbars("2330", date(2026, 7, 29))
    assert "secret" not in str(exc.value)
    assert events[-1] == "logout"


def test_gateway_logs_out_when_login_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Api:
        def login(self, **_kwargs: object) -> None:
            raise RuntimeError("secret")

        def logout(self) -> None:
            events.append("logout")

    class Sdk:
        Shioaji = staticmethod(lambda **_: Api())

    monkeypatch.setattr(
        "findb_fetcher.providers.shioaji.importlib.metadata.version", lambda _: "1.7.1"
    )
    with pytest.raises(Exception):
        ShioajiSdkGateway("key", "secret", _sdk=Sdk()).fetch_kbars("2330", date(2026, 7, 29))
    assert events == ["logout"]
