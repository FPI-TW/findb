"""TPEx: the TPEx index daily levels, one month response serving each day of it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import httpx

from findb_fetcher.provider_adapters.tw_official import TwOfficialAdapter
from findb_fetcher.providers import tpex

INDEX_DATASET = "tw_tpex_index_eod"


class TpexAdapter(TwOfficialAdapter):
    provider = "tpex"
    index_dataset = INDEX_DATASET
    index_symbol = "^TWOII"
    exchange = "TPEX"

    def fetch_index_month(
        self,
        http: httpx.Client,
        *,
        day: date,
        max_response_bytes: int,
        on_http_rate_limited: Callable[[], None] | None,
        on_observed_bytes: Callable[..., None] | None,
    ) -> bytes:
        return tpex.fetch_index_month(
            http,
            day=day,
            max_response_bytes=max_response_bytes,
            on_http_rate_limited=on_http_rate_limited,
            on_observed_bytes=on_observed_bytes,
        )

    def index_row(self, raw: bytes, day: date) -> dict[str, Any]:
        return tpex.index_row(raw, day)

    def index_month_url(self, month: date) -> str:
        return tpex.index_month_url(month)

    def validate_month(self, raw: bytes, month: date) -> None:
        tpex.index_rows(raw, month)
