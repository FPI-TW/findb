"""Concrete provider runtimes for leased historical work.

Every Twelve Data/FinLab request freezes each date's Full release cohort, and HKEX
and FMP requests freeze the release current at creation; all of them are acquired
member by member through the Full daily adapters (``member_historical``).
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from findb_fetcher.historical_backfill import HistoricalRunResult, HistoricalWorkItem
from findb_fetcher.member_historical import MemberHistoricalRunner, full_release_cohort
from findb_fetcher.provider_work import require_owner


class HistoricalRuntimeError(RuntimeError):
    pass


class _MemberRunner:
    """Runs a provider's frozen cohorts through one reused member runner."""

    provider: str
    _members: MemberHistoricalRunner | None = None

    @classmethod
    def from_env(cls) -> Self:
        return cls()

    def _check(self, item: HistoricalWorkItem) -> None:
        pass

    def run(self, item: HistoricalWorkItem) -> UUID | HistoricalRunResult:
        require_owner(item.provider)
        if item.provider != self.provider:
            raise HistoricalRuntimeError("provider mismatch")
        self._check(item)
        if self._members is None:
            self._members = MemberHistoricalRunner.from_env(self.provider)
        return self._members.run(item)

    def close(self) -> None:
        if self._members is not None:
            self._members.close()
            self._members = None


class _DatedReleaseRunner(_MemberRunner):
    """Each date freezes the Full release that applied to it."""

    def _check(self, item: HistoricalWorkItem) -> None:
        # Requests from before cohorts existed have all drained; none may run now.
        if not full_release_cohort(item):
            raise HistoricalRuntimeError(f"{self.provider} historical item has no frozen cohort")


class HkexHistoricalRunner(_MemberRunner):
    """Every request freezes the release current at its creation; members serve it."""

    provider = "hkex"


class FmpHistoricalRunner(_MemberRunner):
    """One request per index and date, through the same adapter as Full daily."""

    provider = "fmp"


class TwelveDataHistoricalRunner(_DatedReleaseRunner):
    provider = "twelve_data"


class FinLabHistoricalRunner(_DatedReleaseRunner):
    provider = "finlab"
