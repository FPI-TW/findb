"""A failed native call keeps a bounded class of what failed, never the provider's words."""

from __future__ import annotations

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


API_ERRORS = """

_AUTH_ERRORS = ["request not valid", "User not found", "api_token not valid"]


def _handle_fetch_error(ret, dataset, time_saved):
    raise Exception("**Error: provider-words-that-must-not-leak")
"""


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
        ('return _handle_fetch_error({"error": "api_token not valid"}, dataset, None)', "auth"),
        (
            'return _handle_fetch_error({"error": "Usage exceed 5000 MB/day. Please consider '
            'upgrade to VIP program."}, dataset, None)',
            "quota",
        ),
        # An API error the worker does not recognise keeps the SDK's own handling.
        ('return _handle_fetch_error({"error": "something new"}, dataset, None)', "sdk_error"),
    ],
)
def test_the_real_worker_reports_what_failed(tmp_path, monkeypatch, get, code) -> None:
    files = fake_finlab(get)
    files["finlab/data.py"] += API_ERRORS
    install_fake_sdk(monkeypatch, tmp_path / "sdk", files)

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.get", PAYLOAD, timeout=1 if code == "process_timeout" else 20)

    assert raised.value.code == code
    assert SECRET not in "".join(traceback.format_exception(raised.value))


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
