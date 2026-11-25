"""TWSE: the TAIEX daily levels, one month response serving each day of it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from findb_fetcher.provider_adapters.tw_official import Table, TwOfficialAdapter
from findb_fetcher.providers import twse
from findb_fetcher.providers.tw_official import TwOfficialFormatError

INDEX_DATASET = "tw_twse_index_eod"
SUMMARY_DATASET = "tw_twse_institutional_summary"
STOCK_DATASET = "tw_twse_institutional_stock"


class TwseAdapter(TwOfficialAdapter):
    provider = "twse"
    index_dataset = INDEX_DATASET
    index_symbol = "^TWII"
    exchange = "TWSE"
    table_datasets = {"summary": SUMMARY_DATASET, "stock": STOCK_DATASET}

    def fetch_index_month(
        self,
        http: httpx.Client,
        *,
        day: date,
        max_response_bytes: int,
        on_http_rate_limited: Callable[[], None] | None,
        on_observed_bytes: Callable[..., None] | None,
    ) -> bytes:
        return twse.fetch_index_month(
            http,
            day=day,
            max_response_bytes=max_response_bytes,
            on_http_rate_limited=on_http_rate_limited,
            on_observed_bytes=on_observed_bytes,
        )

    def index_row(self, raw: bytes, day: date) -> dict[str, Any]:
        return twse.index_row(raw, day)

    def index_month_url(self, month: date) -> str:
        return twse.index_month_url(month)

    def validate_month(self, raw: bytes, month: date) -> None:
        if any((day.year, day.month) != (month.year, month.month) for day in twse.index_rows(raw)):
            raise TwOfficialFormatError("TWSE index response is of another month")

    def table(self, kind: str) -> Table:
        if kind == "summary":
            return Table(
                "summary",
                "BFI82U",
                twse.fetch_summary,
                twse.summary_categories,
                twse.summary_url,
            )
        return Table("stock", "T86", twse.fetch_stock, twse.stock_rows, twse.stock_url)
