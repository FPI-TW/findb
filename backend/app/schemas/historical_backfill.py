"""Durable whole-day TAIFEX historical completion evidence."""

import hashlib
import json
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.ingress import FuturesEODRow

PRODUCTS = frozenset({"TX", "MTX", "TMF", "TE", "TF"})


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

    ref: str = Field(min_length=1, max_length=2048, pattern=r"^r2://[0-9a-f]{32}/")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=16 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_ref(self) -> "HistoricalRawReference":
        # R2 keys are content addressed; reject URLs carrying query/credential data.
        if any(
            token in self.ref for token in ("?", "#", "@", "%", "\\", "/../")
        ) or not self.ref.endswith(f"/{self.sha256}.json"):
            raise ValueError("raw reference must identify its immutable content")
        if "/taifex/tw_futures_eod/" not in self.ref:
            raise ValueError("raw reference is outside the TAIFEX dataset")
        return self


class HistoricalProductReport(HistoricalRawReference):
    product_code: Literal["TX", "MTX", "TMF", "TE", "TF"]
    url: Literal["https://www.taifex.com.tw/cht/3/futDataDown"]


class HistoricalResultEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    item_id: UUID
    request_id: UUID
    trade_date: date
    fetched_at: datetime
    reports: list[HistoricalProductReport] = Field(min_length=5, max_length=5)
    rows: list[HistoricalFuturesRow] = Field(min_length=1, max_length=5000)
    manifest: HistoricalRawReference

    @model_validator(mode="after")
    def validate_manifest(self) -> "HistoricalResultEvidence":
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        if {report.product_code for report in self.reports} != PRODUCTS:
            raise ValueError("every product requires exactly one report")
        keys = [(row.contract_code, row.session) for row in self.rows]
        if len(keys) != len(set(keys)) or any(
            row.trade_date != self.trade_date for row in self.rows
        ):
            raise ValueError("historical member identities must be unique and match the date")
        if {row.product_code for row in self.rows} != PRODUCTS:
            raise ValueError("every product requires dated official rows")
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
