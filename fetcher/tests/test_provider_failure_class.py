"""A failed native call keeps a bounded class of what failed, never the provider's words."""

from __future__ import annotations

import json
import time
import traceback
from types import SimpleNamespace

import pytest

from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.provider_adapters.finlab import FinLabAdapter
from findb_fetcher.provider_process import ProviderProcessError, supervised_call
from findb_fetcher.provider_worker import failure_code
from findb_fetcher.providers.finlab import FinLabNativeError, FinLabOperationalError
from provider_worker_support import fake_finlab, install_fake_sdk

SECRET = "provider-words-that-must-not-leak"
PAYLOAD = {"token": "offline", "dataset": "price:收盤價", "symbols": ("2330",), "account": False}


def _named(name: str, *bases: type) -> BaseException:
    return type(name, bases or (Exception,), {})(SECRET)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (FinLabNativeError("auth"), "auth"),
        (FinLabNativeError("quota"), "quota"),
        # A declared class outside the vocabulary shape is ignored.
        (SimpleNamespace(native_failure_code="Usage exceed 5000 MB/day"), "sdk_error"),
        (MemoryError(), "memory"),
        (TimeoutError(SECRET), "timeout"),
        # requests' timeouts are OSError subclasses; the timeout wins.
        (_named("ReadTimeout", OSError), "timeout"),
        (_named("ConnectTimeout", ConnectionError), "timeout"),
        (_named("JSONDecodeError", ValueError), "bad_response"),
        (ConnectionResetError(SECRET), "network"),
        (_named("ConnectionError", OSError), "network"),
        (_named("SSLError", OSError), "network"),
        (OSError(28, SECRET), "os_error"),
        (RuntimeError(SECRET), "sdk_error"),
        (Exception(f"**Error: {SECRET}"), "sdk_error"),
    ],
)
def test_failure_code_reads_types_and_never_messages(exc, code) -> None:
    assert failure_code(exc) == code


@pytest.mark.parametrize(
    ("get", "code"),
    [
        (f"raise ConnectionError({SECRET!r})", "network"),
        (f"raise TimeoutError({SECRET!r})", "timeout"),
        ("raise MemoryError()", "memory"),
        (f"raise RuntimeError({SECRET!r})", "sdk_error"),
        ("os.kill(os.getpid(), 9)", "killed"),
        ("os._exit(3)", "crashed"),
        ("time.sleep(30)", "process_timeout"),
        # The SDK answers an auth error from the API by starting its own login.
        ("finlab.login()", "auth"),
        (f"raise finlab.exceptions.AuthError({SECRET!r})", "auth"),
        (f"raise finlab.exceptions.QuotaExceededError({SECRET!r})", "quota"),
        # An API error the SDK has no type for keeps the SDK's own handling.
        (f'raise RuntimeError("Batch fetch error: " + {SECRET!r})', "sdk_error"),
    ],
)
def test_the_real_worker_reports_what_failed(tmp_path, monkeypatch, get, code) -> None:
    install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab(get))

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.get", PAYLOAD, timeout=1 if code == "process_timeout" else 20)

    assert raised.value.code == code
    assert SECRET not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize(
    "missing",
    [
        "finlab/exceptions.py:QuotaExceededError",
        "finlab/exceptions.py:AuthError",
        "finlab/data/get.py:fetch_metadata_batch",
        "finlab/data/__init__.py:download_usage",
        "finlab/__init__.py:get_token",
    ],
)
def test_an_sdk_without_the_reviewed_names_fails_closed(tmp_path, monkeypatch, missing) -> None:
    called = tmp_path / "called"
    files = fake_finlab(f"open({str(called)!r}, 'w').close()")
    path, _, name = missing.partition(":")
    files[path] += f"\n\ndel {name}\n"
    install_fake_sdk(monkeypatch, tmp_path / "sdk", files)

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.get", PAYLOAD, timeout=20)

    # Refused before any grant is asked for or any native I/O starts.
    assert raised.value.code == "sdk_contract" and not called.exists()


def test_a_saved_browser_session_never_replaces_the_api_token(tmp_path, monkeypatch) -> None:
    called = tmp_path / "called"
    files = fake_finlab(f"open({str(called)!r}, 'w').close()")
    files["finlab/__init__.py"] += '\n\ndef get_token():\n    return "session", "id_token"\n'
    install_fake_sdk(monkeypatch, tmp_path / "sdk", files)

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.get", PAYLOAD, timeout=20)

    assert raised.value.code == "auth" and not called.exists()


def test_an_expired_grant_is_its_own_class(tmp_path, monkeypatch) -> None:
    install_fake_sdk(monkeypatch, tmp_path / "sdk", fake_finlab("return None"))

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.get", PAYLOAD, checkpoint=lambda: time.monotonic() - 1, timeout=20)

    assert raised.value.code == "dispatch_expired"


def test_the_class_reaches_the_member_diagnostic(tmp_path) -> None:
    fields = FinLabAdapter().diagnostic(FinLabOperationalError("failed", "quota"))

    assert fields == {"category": "finlab_sdk:quota"}
    state = FullMarketState(tmp_path / "state.sqlite3")
    state.record_diagnostic("k", provider="finlab", **fields)
    assert [row["category"] for row in state.diagnostics("k")] == ["finlab_sdk:quota"]


@pytest.mark.parametrize(
    ("replies", "receipts", "usage"),
    [
        (
            [{"quota": 1234.56, "limit_size": 5000}],
            [],
            {"used_mb": 1234.56, "limit_mb": 5000.0},
        ),
        # The update and the full table are two requests; the later reply is the usage.
        (
            [{"quota": 40.5, "limit_size": 5000}, {"quota": 148.5, "limit_size": 5000}],
            [{"billed_bytes": 1500000}, {"billed_bytes": None}, {"billed_bytes": 113000000}],
            {"used_mb": 148.5, "limit_mb": 5000.0, "billed_bytes": 114500000},
        ),
        # The SDK answered from its cache, or the API sent nothing usable.
        ([], [], None),
        ([{"quota": "many", "limit_size": 5000}], [], None),
        ([{"quota": float("nan"), "limit_size": 5000}], [], None),
        ([{"quota": 10}], [{"billed_bytes": 2048}], None),
        # A charge the API did not state as a whole number of bytes is not reported.
        (
            [{"quota": 10, "limit_size": 5000}],
            [{"billed_bytes": "2048"}],
            {"used_mb": 10.0, "limit_mb": 5000.0},
        ),
    ],
)
def test_the_real_worker_returns_the_quota_the_api_reported(
    tmp_path, monkeypatch, replies, receipts, usage
) -> None:
    get = "from provider_worker_support import Frame\nimport datetime\n"
    get += "fetch_metadata_batch({dataset: None})\n" * len(replies)
    get += f"receipts.extend({receipts!r})\n"
    get += "return Frame(datetime.date(2026, 10, 15))"
    files = fake_finlab(
        get,
        metadata=(
            "reply = replies.pop(0)\n"
            "return {name: types.SimpleNamespace(**reply) for name in request_map}"
        ),
    )
    files["finlab/data/get.py"] += f"\n\nreplies = {replies!r}\n".replace("nan", "float('nan')")
    install_fake_sdk(monkeypatch, tmp_path / "sdk", files)

    frame, observed, reported = supervised_call("finlab.get", PAYLOAD, timeout=20)

    assert frame.columns == ("2330",) and observed is None
    assert reported == usage


def test_reported_usage_goes_to_the_ledger_and_the_log(capsys) -> None:
    from findb_fetcher.providers.finlab import _record_usage

    samples = []
    operation = SimpleNamespace(usage=samples.append)

    _record_usage(operation, "price:收盤價", {"used_mb": 1234.56, "limit_mb": 5000.0})

    megabyte = 1024 * 1024
    [sample] = samples
    assert sample["bytes"] == int(1234.56 * megabyte)
    assert sample["limit_bytes"] == 5000 * megabyte
    assert sample["remaining_bytes"] == sample["limit_bytes"] - sample["bytes"]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"finlab_quota": {"dataset": "price:收盤價", "used_mb": 1234.6, "limit_mb": 5000.0}}
    ]


def test_the_charge_the_api_stated_is_logged_beside_the_usage(capsys) -> None:
    from findb_fetcher.providers.finlab import _record_usage

    _record_usage(
        SimpleNamespace(usage=lambda sample: None),
        "price:收盤價",
        {"used_mb": 148.5, "limit_mb": 5000.0, "billed_bytes": 114500000},
    )

    assert json.loads(capsys.readouterr().out) == {
        "finlab_quota": {
            "dataset": "price:收盤價",
            "used_mb": 148.5,
            "limit_mb": 5000.0,
            "billed_bytes": 114500000,
        }
    }


def test_a_ledger_failure_never_fails_the_fetch_it_rode_on(capsys) -> None:
    import sqlite3

    from findb_fetcher.providers.finlab import _record_usage

    def failing(sample):
        raise sqlite3.OperationalError("database is locked")

    _record_usage(
        SimpleNamespace(usage=failing), "price:收盤價", {"used_mb": 6000.0, "limit_mb": 5000.0}
    )

    # Over the limit is still reported as it is; the remaining amount never goes negative.
    assert '"used_mb":6000.0' in capsys.readouterr().out
