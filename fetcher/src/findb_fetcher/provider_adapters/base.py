"""The provider adapter boundary of Full daily acquisition.

The runtime owns work ownership, account quotas, raw persistence, the Source
protocol, checkpoints and retries. An adapter owns only what is specific to one
provider: how one member is fetched and mapped to a contract request, which
native failures mean what, and its per-round native caches. Everything an
adapter may touch reaches it through an explicit context, so a historical
runner can call the same ``acquire`` with its own context.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal

import httpx

from findb_fetcher.raw_storage import RawObject


class FailureKind(StrEnum):
    """What a provider failure says about its scope; the runtime owns the handling."""

    # Only this member: report it and continue the round (bounded no-data budget).
    MEMBER = "member"
    # Report this member (or, with ``wait``, nobody) and end the round; a later round retries.
    ROUND_RETRY = "round_retry"
    # Deterministic for the whole work; the round settles failed.
    WORK_FAILED = "work_failed"
    # The provider account is rate limited: cool down, report and end the round.
    QUOTA_WAIT = "quota_wait"
    # Not a provider classification; the runtime default for the phase applies.
    PASSTHROUGH = "passthrough"


# ``member``: one acquisition in the member loop; ``round``: a failure that escaped
# a daily round; ``maintenance``: a universe refresh round.
Phase = Literal["member", "round", "maintenance"]


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    kind: FailureKind
    # Source outcome reason when the member is reported.
    reason: str = "source_error"
    # The member failure counts toward its no-data budget and breaker.
    no_data: bool = False
    # ROUND_RETRY without charging the member: ``publication`` waits for the provider
    # to publish the target date, ``outage`` for a report every member shares.
    wait: Literal["publication", "outage"] | None = None
    # Bounded failure class; for maintenance it is the settled failure code.
    code: str | None = None
    # HTTP-shaped failure: the runtime derives the failure code from ``status_code``.
    http: bool = False
    status_code: int | None = None
    retry_after: float = 0


PASSTHROUGH = ProviderFailure(FailureKind.PASSTHROUGH)


@dataclass(frozen=True)
class AcquisitionContext:
    """Everything ``acquire`` may use; it never sees plans, parts, owners or state."""

    dataset_key: str
    trade_date: date
    # One observation time for the request body, delivery metadata and evidence.
    fetched_at: datetime
    feed: Mapping[str, Any]
    # Source delivery metadata attached unchanged to the contract request.
    delivery: Mapping[str, Any]
    # The packaged catalog contract of this dataset (schema id and current version).
    contract: Mapping[str, Any] | None
    # Platform limits; ``max_response_bytes`` bounds every native response.
    limits: Mapping[str, Any]
    http: httpx.Client
    # Raw persistence bound to this dataset: ``persist(raw, source_symbol, payload_format=)``.
    persist: Callable[..., RawObject]
    # Account byte accounting: ``observe_response(size, declared=0)``.
    observe_response: Callable[..., None]
    # Account cooldown after a provider rate limit.
    rate_limited: Callable[[], None]
    # Governed, journaled official snapshot: ``snapshot(url, provider_response=False)``.
    snapshot: Callable[..., bytes]
    # The frozen cohort, for providers that serve one shared table per date.
    cohort: Sequence[Mapping[str, Any]] | None = None

    @property
    def max_response_bytes(self) -> int:
        return int(self.limits["max_response_bytes"])


@dataclass(frozen=True)
class AcquisitionResult:
    """One unsigned contract request with its raw object, or a no-trade observation.

    The caller derives the idempotency identity, attaches the raw reference and
    reports a no-trade outcome; the adapter never talks to Source.
    """

    request: dict[str, Any] | None = None
    raw_object: RawObject | None = None
    no_trade: dict[str, Any] | None = None

    @classmethod
    def data(cls, request: dict[str, Any], raw_object: RawObject) -> AcquisitionResult:
        return cls(request=request, raw_object=raw_object)

    @classmethod
    def no_trade_evidence(cls, evidence: dict[str, Any]) -> AcquisitionResult:
        return cls(no_trade=evidence)


@dataclass(frozen=True)
class BatchContext:
    """A whole-cohort acquisition that delivers member by member itself."""

    dataset_key: str
    trade_date: date
    # Per member: Source delivery slot and work item.
    deliveries: dict[str, dict[str, Any]]
    delivery: Any
    raw_store: Any
    on_receipt: Callable[[str, dict[str, Any], str], None]
    # Defer listings the provider has not published yet instead of reporting them.
    defer_missing: bool = False


@dataclass(frozen=True)
class DatedUniverse:
    """A provider observation that defines one date's universe."""

    raw: bytes
    members: list[dict[str, Any]]
    url: str


class ProviderAdapter:
    """Defaults describe a provider without optional capabilities."""

    provider: ClassVar[str] = ""
    # Daily rounds enqueue a universe refresh; dated-universe providers do not.
    scheduled_maintenance: ClassVar[bool] = True
    # Source plan-conflict code asking for ``dated_universe`` before planning.
    dated_universe_code: ClassVar[str | None] = None
    # Journal prefix of a shared native response; once observed, a published
    # calendar closure repairs instead of reusing that response.
    shared_response_prefix: ClassVar[str | None] = None
    # Bounded retry window of a ``wait="outage"`` failure; a success clears it.
    outage_retry_seconds: ClassVar[int | None] = None
    # ``acquire_batch`` replaces the member loop (see FactSet).
    batch: ClassVar[bool] = False
    # Journal key of the batch's native snapshot, and the pending reason it raises.
    batch_snapshot_key: ClassVar[str | None] = None
    batch_pending_reason: ClassVar[str | None] = None

    def reset(self) -> None:
        """Drop per-round native caches; called whenever ownership may change."""

    def close(self) -> None:
        """Release native clients."""

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        raise NotImplementedError(f"{self.provider} has no member acquisition")

    def acquire_batch(self, members: list[dict[str, Any]], context: BatchContext) -> dict[str, Any]:
        raise NotImplementedError(f"{self.provider} has no batch acquisition")

    def batch_status(self, evidence: dict[str, Any]) -> str:
        raise NotImplementedError(f"{self.provider} has no batch acquisition")

    def dated_universe(self, day: date, context: AcquisitionContext) -> DatedUniverse:
        raise NotImplementedError(f"{self.provider} has no dated universe")

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        return PASSTHROUGH

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        """Bounded diagnostic fields for a provider failure; never provider free text."""
        return None
