"""FinLab whole-market tables keep their unlisted columns; FinLab data classifies them."""

from datetime import date
from typing import Any

import pytest

from findb_fetcher.providers import finlab
from findb_fetcher.providers.finlab import (
    _reviewed_columns,
    _table_from_dataframe,
    _with_unlisted,
    security_symbols,
)

DAY = date(2026, 10, 1)


def _frame(rows: int = 80) -> Any:
    # Only the FinLab image installs pandas; the classification rule needs none.
    pandas = pytest.importorskip("pandas")
    index = pandas.date_range(end=pandas.Timestamp(DAY), periods=rows, freq="D")
    return pandas.DataFrame(
        {
            "2330": [float(i) for i in range(rows)],
            "7799": [float(100 + i) for i in range(rows)],
            # Served earlier, empty on the target date: a close carried from history.
            "6666": [5.0] + [float("nan")] * (rows - 1),
            "bad symbol": [1.0] * rows,
        },
        index=index,
    )


def test_worker_returns_reviewed_history_and_recent_unlisted_rows():
    frame = _frame()
    assert list(_reviewed_columns(frame, ["2330"]).columns) == ["2330"]
    narrowed = _reviewed_columns(frame, ["2330"], unlisted=True)
    assert set(narrowed.columns) == {"2330", "7799", "6666", "bad symbol"}
    assert narrowed["2330"].notna().sum() == 80
    assert narrowed["7799"].notna().sum() == finlab.UNLISTED_HISTORY_ROWS


def test_unlisted_columns_are_appended_and_read_like_reviewed_ones():
    frame = _frame()
    table = _table_from_dataframe(
        frame, target_date=DAY, reviewed_symbols=("2330",), carry_previous=True
    )
    merged = _with_unlisted(frame, table, target_date=DAY, carry_previous=True)
    # Columns that are no symbol never enter the table.
    assert merged.symbols == ("2330", "6666", "7799") and merged.unlisted == ("6666", "7799")
    assert merged.values[0][merged.symbols.index("7799")] == 179.0
    assert ("6666", "2026-07-14", 5.0) in merged.previous
    assert _with_unlisted(frame[["2330"]], table, target_date=DAY, carry_previous=False) is table


class _Columns(dict):
    """The two frame reads ``security_symbols`` makes, without pandas."""

    @property
    def columns(self) -> list[str]:
        return list(self)


def test_company_and_etf_nav_data_classify_listed_equities_and_etfs():
    company = _Columns(
        {
            "stock_id": ["2330", "9105", "7799", "1234"],
            "市場別": ["sii", "sii", "otc", "rotc"],
            "產業類別": ["半導體業", "存託憑證", "其他", "其他"],
        }
    )
    # Depositary receipts and the emerging board are no listed equity.
    assert security_symbols(finlab.FINLAB_COMPANY_DATASET, company) == ("2330", "7799")
    nav = _Columns({"0050": [1.0], "00999B": [2.0]})
    assert security_symbols(finlab.FINLAB_ETF_NAV_DATASET, nav) == ("0050", "00999B")
    with pytest.raises(finlab.FinLabSdkError):
        security_symbols(finlab.FINLAB_COMPANY_DATASET, nav)
