from __future__ import annotations

import json
import traceback
from datetime import date
from types import SimpleNamespace

import pytest

from findb_fetcher.providers import finlab
from findb_fetcher.providers.finlab import (
    FinLabOperationalError,
    FinLabSdkError,
    FinLabSdkGateway,
    _dataframe_response_bytes,
)

DAY = date(2026, 10, 15)


class Axis(tuple):
    @property
    def is_unique(self):
        return len(set(self)) == len(self)


class Frame:
    def __init__(self, columns, dates=(DAY,)):
        self.columns = Axis(columns)
        self.index = Axis(dates)
        self.iloc = self
        self.loc = SimpleNamespace()
        self.slices = []

    def fragment(self, columns):
        return json.dumps(
            {column: {day.isoformat(): 123.5 for day in self.index} for column in columns},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def to_json(self):
        raise AssertionError("whole historical dataframe must not be serialized")

    def __getitem__(self, key):
        rows, columns = key
        assert rows == slice(None)
        assert columns.stop - columns.start <= 16
        self.slices.append(columns)
        return SimpleNamespace(to_json=lambda: self.fragment(self.columns[columns]))


@pytest.mark.parametrize("count", [0, 1, 15, 16, 17, 32, 33, 300])
@pytest.mark.parametrize("dates", [(), (date(2000, 1, 1), DAY)])
def test_counts_complete_frame_in_bounded_column_batches(count, dates):
    frame = Frame([f"商品{i}" for i in range(count)], dates)
    assert _dataframe_response_bytes(frame) == len(frame.fragment(frame.columns).encode("utf-8"))
    assert len(frame.slices) == (count + 15) // 16


@pytest.mark.parametrize("duplicate_axis", ["columns", "index"])
def test_rejects_global_duplicates_before_any_serialization(duplicate_axis):
    frame = Frame([str(i) for i in range(33)])
    if duplicate_axis == "columns":
        frame.columns = Axis([*frame.columns[:-1], frame.columns[0]])
    else:
        frame.index = Axis((DAY, DAY))
    with pytest.raises(FinLabSdkError, match="axes must be unique"):
        _dataframe_response_bytes(frame)
    assert frame.slices == []


def gateway(frame, callback):
    return FinLabSdkGateway(
        api_token="offline-test-token",
        _sdk=SimpleNamespace(login=lambda _: None, data=SimpleNamespace(get=lambda _: frame)),
        on_response_bytes=callback,
    )


def test_gateway_observes_complete_frame_once_before_target_projection():
    frame = Frame([str(i) for i in range(33)])
    observed = []
    # Missing target is rejected only after the complete response is accounted.
    with pytest.raises(FinLabSdkError, match="exactly one target date"):
        gateway(frame, observed.append).fetch_dataset(
            "price:收盤價", target_date=date(2026, 10, 16), symbols=("0",)
        )
    assert observed == [len(frame.fragment(frame.columns).encode("utf-8"))]


def test_existing_response_bound_callback_still_rejects_and_sanitizes():
    frame = Frame([str(i) for i in range(33)])
    observed = []

    def reject(size):
        observed.append(size)
        raise RuntimeError("credential-must-not-leak")

    with pytest.raises(FinLabSdkError) as raised:
        gateway(frame, reject).fetch_dataset("price:收盤價", target_date=DAY, symbols=("0",))
    assert observed == [len(frame.fragment(frame.columns).encode("utf-8"))]
    assert "credential-must-not-leak" not in "".join(traceback.format_exception(raised.value))


def test_invalid_serialization_cannot_be_accounted_as_valid():
    frame = Frame(["0"])
    frame.fragment = lambda _: "credential-must-not-leak"
    observed = []
    with pytest.raises(FinLabSdkError) as raised:
        gateway(frame, observed.append).fetch_dataset(
            "price:收盤價", target_date=DAY, symbols=("0",)
        )
    assert observed == []
    assert "credential-must-not-leak" not in "".join(traceback.format_exception(raised.value))


def test_catalogue_accounts_full_frame_and_preserves_operational_failure():
    frame = Frame(["2330", "0050"])
    observed = []
    table = gateway(frame, observed.append).fetch_catalogue(symbols=("0050", "00415A"))
    assert table.symbols == ("0050",) and table.dates == table.values == ()
    assert observed == [len(frame.fragment(frame.columns).encode())]

    def reject(size):
        raise RuntimeError("credential-must-not-leak")

    with pytest.raises(FinLabOperationalError) as raised:
        gateway(frame, reject).fetch_catalogue(symbols=("0050",))
    assert "credential-must-not-leak" not in "".join(traceback.format_exception(raised.value))


def test_catalogue_source_denial_is_operational_before_sdk_get(tmp_path):
    from findb_fetcher.acquisition import authorization_scope
    from findb_fetcher.full_market_state import QuotaBlockedError

    calls = []
    sdk = SimpleNamespace(
        login=lambda _: None, data=SimpleNamespace(get=lambda _: calls.append("get"))
    )

    def deny():
        raise QuotaBlockedError("credential-must-not-leak")

    with authorization_scope(deny, state_path=tmp_path / "account.sqlite3"):
        with pytest.raises(FinLabOperationalError) as raised:
            FinLabSdkGateway(api_token="offline-token", _sdk=sdk).fetch_catalogue(symbols=("0050",))
    assert calls == []
    assert "credential-must-not-leak" not in "".join(traceback.format_exception(raised.value))


class Columns(Axis):
    def isin(self, values):
        return [column in values for column in self]


class Loc:
    def __init__(self, frame):
        self.frame = frame

    def __getitem__(self, key):
        rows, keep = key
        assert rows == slice(None)
        narrowed = Frame([], self.frame.index)
        narrowed.columns = Columns(c for c, kept in zip(self.frame.columns, keep) if kept)
        return narrowed


def native_get(monkeypatch, frame, *, account):
    modules = {
        "finlab": SimpleNamespace(login=lambda _: None),
        "finlab.data": SimpleNamespace(get=lambda _: frame),
        "finlab.utils": SimpleNamespace(),
    }
    monkeypatch.setattr(finlab.importlib, "import_module", modules.__getitem__)
    payload = {"token": "offline", "dataset": "x", "symbols": ("0050", "2330"), "account": account}
    return finlab.native_get(payload, lambda: None)


@pytest.mark.parametrize("account", [False, True])
def test_worker_returns_only_reviewed_columns_with_complete_frame_bytes(monkeypatch, account):
    frame = Frame([])
    frame.columns = Columns(["2330", *(str(i) for i in range(32)), "0050"])
    frame.loc = Loc(frame)
    reviewed, observed = native_get(monkeypatch, frame, account=account)
    # The parent never receives the complete table, only its accounted size.
    assert reviewed.columns == ("2330", "0050")
    assert observed == (len(frame.fragment(frame.columns).encode()) if account else None)


def test_worker_returns_invalid_frame_whole_for_typed_parent_validation(monkeypatch):
    frame = Frame(Columns(["2330", "2330"]))
    assert native_get(monkeypatch, frame, account=False) == (frame, None)
