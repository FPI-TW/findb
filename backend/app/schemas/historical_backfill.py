"""Durable whole-day TAIFEX historical completion evidence."""

import hashlib
import json
from datetime import date, datetime
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.ingress import FuturesEODRow

PRODUCTS = frozenset({"TX", "MTX", "TMF", "TE", "TF"})
# First trading day of each product on TAIFEX (verified against futDataDown: the day before
# returns only the header). A historical date requires only products already listed (#754).
# The Fetcher keeps the same table in ``findb_fetcher.providers.taifex``.
PRODUCT_LISTED_DATES = {
    "TX": date(1998, 7, 21),
    "TE": date(1999, 7, 21),
    "TF": date(1999, 7, 21),
    "MTX": date(2001, 4, 9),
    "TMF": date(2024, 7, 29),
}


def listed_products(trade_date: date) -> frozenset[str]:
    """Products whose official report must carry rows on the date."""
    return frozenset(
        product for product, listed in PRODUCT_LISTED_DATES.items() if listed <= trade_date
    )


class HistoricalFuturesRow(FuturesEODRow):
    @model_validator(mode="after")
    def validate_prices(self) -> "HistoricalFuturesRow":
        if self.contract_code != f"{self.product_code}:{self.contract_month}":
            raise ValueError("contract identity differs from product/month")
        if not self.no_trade:
            FuturesEODRow.model_validate(self.model_dump())
        return self

    @property
    def no_trade(self) -> bool:
        return self.volume == 0 and all(
            getattr(self, field) is None
            for field in ("open", "high", "low", "close", "settlement_price", "open_interest")
        )


class HistoricalRawReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    _extensions: ClassVar[tuple[str, ...]] = ("json",)

    ref: str = Field(min_length=1, max_length=2048, pattern=r"^r2://[0-9a-f]{32}/")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=16 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_ref(self) -> "HistoricalRawReference":
        # R2 keys are content addressed; reject URLs carrying query/credential data.
        if any(
            token in self.ref for token in ("?", "#", "@", "%", "\\", "/../")
        ) or not self.ref.endswith(
            tuple(f"/{self.sha256}.{extension}" for extension in self._extensions)
        ):
            raise ValueError("raw reference must identify its immutable content")
        if "/taifex/tw_futures_eod/" not in self.ref:
            raise ValueError("raw reference is outside the TAIFEX dataset")
        return self


class HistoricalProductReport(HistoricalRawReference):
    # Exact CP950 report bytes use CSV; retain older JSON-addressed checkpoints.
    _extensions: ClassVar[tuple[str, ...]] = ("csv", "json")

    product_code: Literal["TX", "MTX", "TMF", "TE", "TF"]
    url: Literal["https://www.taifex.com.tw/cht/3/futDataDown"]


class HistoricalResultEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    item_id: UUID
    request_id: UUID
    trade_date: date
    fetched_at: datetime
    reports: list[HistoricalProductReport] = Field(min_length=1, max_length=5)
    rows: list[HistoricalFuturesRow] = Field(min_length=1, max_length=5000)
    manifest: HistoricalRawReference

    @model_validator(mode="after")
    def validate_manifest(self) -> "HistoricalResultEvidence":
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        listed = listed_products(self.trade_date)
        codes = [report.product_code for report in self.reports]
        if not listed or len(codes) != len(set(codes)) or set(codes) != listed:
            raise ValueError("every listed product requires exactly one report")
        keys = [(row.contract_code, row.session) for row in self.rows]
        if len(keys) != len(set(keys)) or any(
            row.trade_date != self.trade_date for row in self.rows
        ):
            raise ValueError("historical member identities must be unique and match the date")
        if {row.product_code for row in self.rows} != listed:
            raise ValueError("every listed product requires dated official rows")
        manifest = json.dumps(
            self.model_dump(mode="json", exclude={"manifest"}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if (
            len(manifest) != self.manifest.size_bytes
            or hashlib.sha256(manifest).hexdigest() != self.manifest.sha256
        ):
            raise ValueError("historical manifest checksum differs from completion evidence")
        return self
