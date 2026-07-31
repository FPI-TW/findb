"""Immutable, provider-neutral release manifests for minute-data archives."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Sha256 = str


def _validate_month(value: date) -> date:
    if value.day != 1:
        raise ValueError("month coverage values must be the first day of their month")
    return value


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


class ArchiveObject(BaseModel):
    """One immutable object reference; URLs and credentials are forbidden."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    object_key: str = Field(min_length=1, max_length=1024)
    sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=1)

    @field_validator("object_key")
    @classmethod
    def require_storage_key_not_url(cls, value: str) -> str:
        if "://" in value or "?" in value or "#" in value:
            raise ValueError("object_key must be a stable storage key, not a URL")
        return value


class ArchiveChunk(BaseModel):
    """A released chunk with a strict governance cap of 5,000 rows."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    snapshot_sequence: int = Field(ge=1)
    chunk_sequence: int = Field(ge=1)
    chunk_count: int = Field(ge=1)
    row_count: int = Field(ge=1, le=5_000)
    checksum_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    object: ArchiveObject


class MarketMinuteArchiveManifest(BaseModel):
    """Finalized immutable manifest for ``market_minute_archive.v1``."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_id: Literal["market_minute_archive"]
    schema_version: Literal[1]
    release_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    dataset_key: Literal["tw_equity_minute", "tw_etf_minute"]
    source: Literal["tw_recorder_archive"]
    upstream_source: Literal["shioaji"]
    overlap_precedence: Literal["direct_daily_shioaji"]
    trading_calendar_checksum: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_start_month: date
    coverage_end_month: date
    covered_months: list[date] = Field(min_length=1)
    expected_trading_dates: list[date] = Field(min_length=1)
    covered_trading_dates: list[date] = Field(min_length=1)
    trading_dates_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    instruments: list[str] = Field(min_length=1, max_length=10_000)
    instruments_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    sequence_count: int = Field(ge=1)
    chunk_count: int = Field(ge=1)
    row_count: int = Field(ge=1)
    checksum_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    chunks: list[ArchiveChunk] = Field(min_length=1)
    objects: list[ArchiveObject] = Field(min_length=1)
    release_state: Literal["finalized"]
    finalized_at: datetime

    @field_validator("coverage_start_month", "coverage_end_month")
    @classmethod
    def require_month_values(cls, value: date) -> date:
        return _validate_month(value)

    @field_validator("covered_months")
    @classmethod
    def require_covered_month_values(cls, values: list[date]) -> list[date]:
        return [_validate_month(value) for value in values]

    @field_validator("finalized_at")
    @classmethod
    def require_finalized_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("finalized_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_release(self) -> "MarketMinuteArchiveManifest":
        expected_months: list[date] = []
        current = self.coverage_start_month
        while current <= self.coverage_end_month:
            expected_months.append(current)
            current = _next_month(current)
        if self.covered_months != expected_months:
            raise ValueError("covered_months must be continuous and equal the declared coverage")
        coverage_months = set(self.covered_months)
        for label, trading_dates in (
            ("expected_trading_dates", self.expected_trading_dates),
            ("covered_trading_dates", self.covered_trading_dates),
        ):
            if trading_dates != sorted(trading_dates) or len(trading_dates) != len(
                set(trading_dates)
            ):
                raise ValueError(f"{label} must be strictly ascending without duplicates")
            if any(
                date(value.year, value.month, 1) not in coverage_months for value in trading_dates
            ):
                raise ValueError(f"{label} must lie in declared covered months")
        if any(
            month not in {date(value.year, value.month, 1) for value in self.expected_trading_dates}
            for month in self.covered_months
        ):
            raise ValueError("every covered month requires an expected trading date")
        if self.expected_trading_dates != self.covered_trading_dates:
            raise ValueError("expected_trading_dates must exactly equal covered_trading_dates")
        expected_dates_checksum = hashlib.sha256(
            "\n".join(value.isoformat() for value in self.expected_trading_dates).encode("utf-8")
        ).hexdigest()
        if self.trading_dates_sha256 != expected_dates_checksum:
            raise ValueError("trading_dates_sha256 must match LF-joined expected_trading_dates")
        if len(self.instruments) != len(set(self.instruments)):
            raise ValueError("instruments must not contain duplicates")
        expected_instruments = hashlib.sha256(
            "\n".join(sorted(self.instruments)).encode("utf-8")
        ).hexdigest()
        if self.instruments_sha256 != expected_instruments:
            raise ValueError("instruments_sha256 must match sorted instruments")
        if self.chunk_count != len(self.chunks):
            raise ValueError("chunk_count must match chunks length")
        chunks_by_snapshot: dict[int, list[ArchiveChunk]] = {}
        for chunk in self.chunks:
            chunks_by_snapshot.setdefault(chunk.snapshot_sequence, []).append(chunk)
        if sorted(chunks_by_snapshot) != list(range(1, self.sequence_count + 1)):
            raise ValueError("snapshot sequences must be contiguous from 1 through sequence_count")
        for snapshot_chunks in chunks_by_snapshot.values():
            expected_count = snapshot_chunks[0].chunk_count
            if any(chunk.chunk_count != expected_count for chunk in snapshot_chunks):
                raise ValueError("chunks in a snapshot sequence must agree on chunk_count")
            if len(snapshot_chunks) != expected_count or sorted(
                chunk.chunk_sequence for chunk in snapshot_chunks
            ) != list(range(1, expected_count + 1)):
                raise ValueError("chunk sequences must be contiguous within each snapshot sequence")
        if sum(chunk.row_count for chunk in self.chunks) != self.row_count:
            raise ValueError("row_count must equal the sum of chunk row_count values")
        objects_by_key = {item.object_key: item for item in self.objects}
        if len(objects_by_key) != len(self.objects):
            raise ValueError("objects must not contain duplicate object_key values")
        if any(
            objects_by_key.get(chunk.object.object_key) != chunk.object for chunk in self.chunks
        ):
            raise ValueError("every chunk object must exactly match its listed object evidence")
        return self
