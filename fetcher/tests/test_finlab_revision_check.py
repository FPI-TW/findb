"""The daily FinLab fetch compares the previous trading day, as served now, with what was delivered."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.full_market_universe import canonical_bytes
from findb_fetcher.provider_adapters.finlab import FinLabAdapter, finlab_revision_check
from findb_fetcher.providers.finlab import (
    FINLAB_FIELD_DATASETS,
    FinLabDatasetTable,
    FinLabSdkGateway,
)

TARGET = date(2026, 11, 19)
PREVIOUS = date(2026, 11, 18)
FIELDS = ("open", "high", "low", "close", "volume")


class _Loc:
    def __init__(self, values: dict[tuple[object, str], object]) -> None:
        self._values = values

    def __getitem__(self, key: tuple[object, str]) -> object:
        return self._values[key]


class _Frame:
    """The pandas surface the gateway reads: index, columns and ``loc[row, column]``."""

    def __init__(self, rows: dict[date, dict[str, object]]) -> None:
        self.index = tuple(rows)
        self.columns = tuple(next(iter(rows.values())))
        self.loc = _Loc(
            {(day, symbol): value for day, row in rows.items() for symbol, value in row.items()}
        )


def _gateway(frame: _Frame) -> FinLabSdkGateway:
    sdk = SimpleNamespace(login=lambda token: None, data=SimpleNamespace(get=lambda name: frame))
    return FinLabSdkGateway(api_token="test-token", _sdk=sdk)


def test_a_fresh_table_remembers_the_previous_trading_day_as_served_now() -> None:
    gateway = _gateway(
        _Frame(
            {
                # The frame's own row order is not trusted; the latest earlier date wins.
                PREVIOUS: {"2330": 1001.0, "2317": None},
                TARGET: {"2330": 1010.0, "2317": 210.0},
                date(2026, 11, 17): {"2330": 990.0, "2317": 200.0},
            }
        )
    )

    table = gateway.fetch_dataset("price:收盤價", target_date=TARGET, symbols=("2330", "2317"))

    # The delivered table is unchanged: still exactly the target row.
    assert table == FinLabDatasetTable(("2026-11-19",), ("2330", "2317"), ((1010.0, 210.0),))
    assert gateway.prior_rows == {"price:收盤價": ("2026-11-18", {"2330": 1001.0, "2317": None})}


def test_a_table_holding_only_the_target_day_remembers_nothing() -> None:
    gateway = _gateway(_Frame({TARGET: {"2330": 1010.0, "2317": 210.0}}))

    gateway.fetch_dataset("price:收盤價", target_date=TARGET, symbols=("2330", "2317"))

    assert gateway.prior_rows == {}


def _prior(**symbols: tuple[object, object, object, object, object]) -> dict[str, dict]:
    return {
        field: {symbol: values[index] for symbol, values in symbols.items()}
        for index, field in enumerate(FIELDS)
    }


def _row(open_: str, high: str, low: str, close: str, volume: int) -> dict[str, Any]:
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume}


MEMBERS = {"2330": "2330", "2317": "2317", "1101": "1101"}
DELIVERED = {
    "2330": _row("1000", "1010", "990", "1001", 25000000),
    "2317": _row("200.5", "201", "199", "200", 30000000),
    # A source-proven no-trade member.
    "1101": None,
}
SAME = _prior(
    **{
        "2330": (1000.0, 1010.0, 990.0, 1001.0, 25000000.0),
        "2317": (200.5, 201.0, 199.0, 200.0, 30000000.0),
        "1101": (None, None, None, None, 0.0),
    }
)


def _check(prior: dict, delivered=DELIVERED, members=MEMBERS) -> dict[str, Any]:
    return finlab_revision_check(
        prior=prior,
        delivered=delivered,
        members=members,
        target_close={"2330": 1010.0, "2317": 210.0, "1101": None},
    )


def test_a_day_served_as_delivered_has_no_revision() -> None:
    assert _check(SAME) == {
        "target_valued": 2,
        "previous_valued_now": 2,
        "target_to_previous": 1.0,
        "compared": 3,
        "previous_valued_delivered": 2,
        "no_trade_now_valued": 0,
        "delivered_changed": 0,
        "delivered_now_empty": 0,
        "delivered_now_missing": 0,
    }


@pytest.mark.parametrize(
    ("change", "kind"),
    [
        # A delivered member's close was corrected afterwards.
        ({"2330": (1000.0, 1010.0, 990.0, 1002.0, 25000000.0)}, "delivered_changed"),
        # Its volume was filled in later.
        ({"2317": (200.5, 201.0, 199.0, 200.0, 30000500.0)}, "delivered_changed"),
        # A member delivered as no trade now has prices: it traded after all.
        ({"1101": (30.0, 30.5, 29.5, 30.0, 1000.0)}, "no_trade_now_valued"),
        # A delivered member's row was emptied.
        ({"2330": (None, None, None, None, None)}, "delivered_now_empty"),
    ],
    ids=["price changed", "volume changed", "no trade became valued", "emptied"],
)
def test_each_kind_of_later_revision_is_counted(change, kind) -> None:
    prior = _prior(
        **{
            symbol: tuple(SAME[field][symbol] for field in FIELDS)
            for symbol in MEMBERS
            if symbol not in change
        },
        **change,
    )

    result = _check(prior)

    assert result[kind] == 1
    others = {"no_trade_now_valued", "delivered_changed", "delivered_now_empty"} - {kind}
    assert all(result[other] == 0 for other in others)


def test_a_member_no_longer_served_is_counted_as_missing() -> None:
    prior = {
        field: {k: v for k, v in values.items() if k != "2317"} for field, values in SAME.items()
    }

    assert _check(prior)["delivered_now_missing"] == 1


def test_a_day_delivered_with_carried_prices_compares_only_its_volume() -> None:
    # Every price empty with a positive volume: delivered with the earlier close.
    delivered = {**DELIVERED, "2317": _row("199", "199", "199", "199", 500)}
    unchanged = _prior(
        **{
            "2330": (1000.0, 1010.0, 990.0, 1001.0, 25000000.0),
            "2317": (None, None, None, None, 500.0),
            "1101": (None, None, None, None, 0.0),
        }
    )
    more = {**unchanged, "volume": {**unchanged["volume"], "2317": 800.0}}

    assert _check(unchanged, delivered)["delivered_changed"] == 0
    assert _check(more, delivered)["delivered_changed"] == 1


def test_without_a_locally_held_delivery_only_the_ratio_is_reported() -> None:
    result = _check(SAME, delivered=None)

    assert (result["target_valued"], result["previous_valued_now"]) == (2, 2)
    assert result["compared"] is None and result["delivered_changed"] is None


def test_members_no_longer_listed_today_are_not_compared() -> None:
    assert _check(SAME, members={"2330": "2330"})["compared"] == 1


def _plan(day: date) -> dict[str, Any]:
    return {
        "plan_id": f"plan-{day}",
        "dataset_key": "tw_equity_eod",
        "trade_date": day.isoformat(),
        "parts": [{"work_item_id": "fp1:1", "member_keys": ["2330", "2317", "1101", "6488"]}],
    }


def test_the_state_returns_only_what_a_held_plan_settled(tmp_path: Path) -> None:
    state = FullMarketState(tmp_path / "state.sqlite3")
    assert state.delivered_rows("finlab", "tw_equity_eod", PREVIOUS) is None
    state.record_plan(_plan(PREVIOUS), "finlab")
    request = {"payload": {"data": [{"source_symbol": "2330", **DELIVERED["2330"]}]}}
    state.prepare("fp1:1:2330", canonical_bytes([request]))
    state.finish("fp1:1:2330", status="complete", receipt="run")
    state.finish("fp1:1:1101", status="complete", reason="source_proven_no_trade")
    # Prepared but not settled, and never acquired: neither was delivered.
    state.prepare("fp1:1:2317", canonical_bytes([request]))

    assert state.delivered_rows("finlab", "tw_equity_eod", PREVIOUS) == {
        "2330": {"source_symbol": "2330", **DELIVERED["2330"]},
        "1101": None,
    }
    assert state.delivered_rows("finlab", "tw_etf_eod", PREVIOUS) is None


class _Gateway:
    """Serves the target tables and, like the SDK gateway, remembers the previous day."""

    def __init__(self, prior: dict[str, dict]) -> None:
        self.prior_rows: dict[str, tuple[str, dict]] = {}
        self._prior = prior

    def fetch_dataset(self, name, *, target_date, symbols):
        field = next(field for field, dataset in FINLAB_FIELD_DATASETS.items() if dataset == name)
        self.prior_rows[name] = (PREVIOUS.isoformat(), self._prior[field])
        values = {"open": 1010.0, "high": 1015.0, "low": 1005.0, "close": 1012.0, "volume": 2.0e7}
        return FinLabDatasetTable(
            (target_date.isoformat(),), tuple(symbols), (tuple(values[field] for _ in symbols),)
        )


def _context(delivered, tmp_path: Path) -> Any:
    from findb_fetcher.provider_adapters.base import AcquisitionContext
    from findb_fetcher.raw_storage import RawObject

    cohort = [
        {
            "member_key": symbol,
            "symbol": symbol,
            "provider_symbol": symbol,
            "mapping_status": "mapped",
        }
        for symbol in ("2330", "2317", "1101")
    ]
    return AcquisitionContext(
        dataset_key="tw_equity_eod",
        trade_date=TARGET,
        fetched_at=datetime(2026, 11, 19, 10, tzinfo=timezone.utc),
        feed={"asset_class": "equity"},
        delivery={"slot_id": "taiwan_market_window", "work_item_id": "fp1:2"},
        contract=None,
        limits={"max_response_bytes": 16 * 1024 * 1024},
        http=None,  # type: ignore[arg-type]
        persist=lambda raw, symbol, **kwargs: RawObject(
            "r2://" + "a" * 32 + "/raw/x.json", "b" * 64, len(raw)
        ),
        observe_response=lambda *args, **kwargs: None,
        rate_limited=lambda: None,
        snapshot=lambda *args, **kwargs: b"",
        cohort=cohort,
        delivered=lambda day: delivered if day == PREVIOUS else None,
    )


def _lines(capsys) -> dict[str, dict]:
    return {
        key: value
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
        for key, value in json.loads(line).items()
    }


@pytest.mark.parametrize("revised", [False, True])
def test_the_daily_fetch_logs_the_check_and_alerts_only_on_a_revision(
    tmp_path, capsys, revised
) -> None:
    prior = SAME if not revised else {**SAME, "close": {**SAME["close"], "2330": 1002.0}}
    adapter = FinLabAdapter()
    gateway = _Gateway(prior)
    adapter.gateway = lambda observe: gateway  # type: ignore[method-assign]

    result = adapter.acquire(
        {"member_key": "2330", "symbol": "2330", "provider_symbol": "2330"},
        _context(DELIVERED, tmp_path),
    )

    # Delivery of the target day is not affected by the check.
    assert result.request is not None
    assert result.request["payload"]["data"][0]["close"] == "1012.0"
    lines = _lines(capsys)
    check = lines["finlab_revision_check"]
    assert (check["trade_date"], check["previous_date"], check["compared"]) == (
        "2026-11-19",
        "2026-11-18",
        3,
    )
    assert check["delivered_changed"] == int(revised)
    assert ("finlab_revision_detected" in lines) is revised
    # Bounded fields only: counts and dates, never a served or delivered value.
    assert "1002" not in json.dumps(lines) and "1001" not in json.dumps(lines)


def test_a_check_that_cannot_run_never_disturbs_the_delivery(tmp_path, capsys) -> None:
    adapter = FinLabAdapter()
    gateway = _Gateway(SAME)
    adapter.gateway = lambda observe: gateway  # type: ignore[method-assign]
    context = _context(DELIVERED, tmp_path)

    def broken(day):
        raise RuntimeError("state unavailable")

    from dataclasses import replace

    result = adapter.acquire(
        {"member_key": "2330", "symbol": "2330", "provider_symbol": "2330"},
        replace(context, delivered=broken),
    )

    assert result.request is not None
    assert "finlab_revision_check" not in _lines(capsys)
