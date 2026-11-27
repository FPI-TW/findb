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
from zoneinfo import ZoneInfo

import httpx

from findb_fetcher.full_market_errors import MemberUnavailableError
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
    # The frozen universe release's effective date, when the caller knows it.
    universe_effective_date: date | None = None
    # Provider symbols of this part that may share one native request, starting
    # with this member; evaluated only when the adapter really dispatches.
    batch_candidates: Callable[[], tuple[str, ...]] | None = None
    # What this runtime delivered for another trade date of the dataset, from its
    # own checkpoints: ``delivered(trade_date)`` maps member key to the delivered
    # row, or to ``None`` for a source-proven no-trade member. ``None`` when that
    # plan is not held locally.
    delivered: Callable[[date], Mapping[str, Mapping[str, Any] | None] | None] | None = None
    # The latest earlier trade date of the dataset with a locally held plan, if any.
    earlier_plan_date: Callable[[], date | None] | None = None

    @property
    def max_response_bytes(self) -> int:
        return int(self.limits["max_response_bytes"])


@dataclass(frozen=True)
class AcquisitionResult:
    """One contract request with its raw object, or a no-trade observation.

    The caller replaces the request identity with its own (Full daily keys it by
    plan and member), attaches the raw reference and reports a no-trade outcome;
    the adapter never talks to Source.
    """

    request: dict[str, Any] | None = None
    # The persisted native observation: the request's raw, or the no-trade evidence.
    raw_object: RawObject | None = None
    no_trade: dict[str, Any] | None = None
    # A no-trade claim only Source can corroborate: if it refuses, this is the failure.
    unconfirmed: BaseException | None = None
    # The Source no-data reason, equal to the evidence's ``source_status``:
    # ``no_trade``, or ``halted`` for a suspended or halted security.
    no_data_reason: str = "no_trade"

    @classmethod
    def data(cls, request: dict[str, Any], raw_object: RawObject) -> AcquisitionResult:
        return cls(request=request, raw_object=raw_object)

    @classmethod
    def no_trade_evidence(
        cls,
        evidence: dict[str, Any],
        raw_object: RawObject | None = None,
        *,
        unconfirmed: BaseException | None = None,
        reason: Literal["no_trade", "halted"] = "no_trade",
    ) -> AcquisitionResult:
        return cls(
            no_trade=evidence,
            raw_object=raw_object,
            unconfirmed=unconfirmed,
            no_data_reason=reason,
        )


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


@dataclass(frozen=True)
class UniverseSnapshot:
    """One dataset's universe: effective date, members and exact official evidence."""

    effective_date: date
    members: list[dict[str, Any]]
    # Official URL -> exact observed bytes; the runtime persists and submits them.
    evidence: dict[str, bytes]
    # Catalogue symbols outside the list: requested with each plan, never expected.
    # ``None`` for a provider that keeps none; an empty list records that none remain.
    supplementary: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class UniverseContext:
    """Everything ``build_universe`` may use during one owned universe refresh."""

    # The Source-authorized refresh scope.
    dataset_keys: Sequence[str]
    # One observation time; exchange-local dates derive from it and ``zones``.
    observed_at: datetime
    # Source-published feed timezones, never local tables.
    zones: Mapping[str, ZoneInfo]
    # Governed, journaled official snapshot: ``snapshot(url, provider_response=False)``.
    snapshot: Callable[..., bytes]
    # Governed provider catalogue rows (``catalogue(dataset_keys=)``) and their evidence.
    catalogue: Callable[..., list[dict[str, Any]]]
    catalogue_evidence: Callable[[str], dict[str, bytes]]
    # Raw persistence: ``persist(raw, dataset_key, source_symbol)``.
    persist: Callable[..., RawObject]
    observe_response: Callable[..., None]
    # Official evidence already persisted, by URL; the runtime reuses these objects.
    snapshot_objects: dict[str, RawObject]
    # Re-read platform limits, and pace the account after a completed SDK call.
    refresh_limits: Callable[[], None]
    pace: Callable[[], None]
    # The runtime's governed HTTP client, for a provider list read with the account key.
    http: httpx.Client | None = None
    # Source-published ``target_date_lag_days`` by dataset, beside ``zones``: a feed
    # scheduled the next morning lists members for the previous market date.
    target_date_lags: Mapping[str, int] | None = None


@dataclass(frozen=True)
class CatalogueRequest:
    """One provider catalogue HTTP request; the runtime governs and journals it."""

    url: str
    params: Mapping[str, str]
    headers: Mapping[str, str]
    # Credential-free URL that identifies the catalogue evidence.
    evidence_url: str
    max_response_bytes: int


class ProviderAdapter:
    """Defaults describe a provider without optional capabilities."""

    provider: ClassVar[str] = ""
    # The scheduled refresh publishes a release effective on the trade date, so
    # today's plan briefly waits for it rather than freezing the previous list.
    daily_waits_for_same_day_universe: ClassVar[bool] = False
    # Journal prefix of a shared native response; once observed, a published
    # calendar closure repairs instead of reusing that response.
    shared_response_prefix: ClassVar[str | None] = None
    # The outage is of one observation the whole cohort is read from: the plan waits
    # as a whole, and an exhausted window ends the work instead of charging the
    # members one at a time.
    outage_holds_plan: ClassVar[bool] = False
    # Each member publishes on its own (one native series per member): a member not
    # yet published waits alone while the round goes on with the others.
    publication_per_member: ClassVar[bool] = False
    # ``acquire_batch`` replaces the member loop (see FactSet).
    batch: ClassVar[bool] = False
    # Journal key of the batch's native snapshot, and the pending reason it raises.
    batch_snapshot_key: ClassVar[str | None] = None
    batch_pending_reason: ClassVar[str | None] = None
    # Datasets with an HTTP provider catalogue (``catalogue_request``) and its journal
    # name; ``None`` means the catalogue is observed inside ``build_universe``.
    catalogue_datasets: ClassVar[tuple[str, ...] | None] = ()
    catalogue_journal: ClassVar[str] = ""

    def reset(self) -> None:
        """Drop per-round native caches; called whenever ownership may change."""

    def round_started(self, context: AcquisitionContext) -> None:
        """A daily round of a frozen plan begins, before any member; the owner holds it.

        Never part of the round's outcome: an adapter catches its own failures.
        """

    def round_finished(self, dataset_key: str) -> None:
        """A daily round of ``dataset_key`` is about to settle; the owner still holds it."""

    def close(self) -> None:
        """Release native clients."""

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        raise NotImplementedError(f"{self.provider} has no member acquisition")

    def batch_limit(self, dataset_key: str) -> int:
        """Members of one part a dataset may fetch together; below 2 is one at a time."""
        return int(getattr(self, "member_batch_limit", 0))

    def acquire_batch(self, members: list[dict[str, Any]], context: BatchContext) -> dict[str, Any]:
        raise NotImplementedError(f"{self.provider} has no batch acquisition")

    def batch_status(self, evidence: dict[str, Any]) -> str:
        raise NotImplementedError(f"{self.provider} has no batch acquisition")

    def dated_universe(self, day: date, context: AcquisitionContext) -> DatedUniverse:
        raise NotImplementedError(f"{self.provider} has no dated universe")

    def build_universe(
        self, feeds: Sequence[Mapping[str, Any]], context: UniverseContext
    ) -> dict[str, UniverseSnapshot]:
        """Fetch official listings, map them to provider symbols, keyed by dataset."""
        raise NotImplementedError(f"{self.provider} has no universe refresh")

    def catalogue_request(self, dataset_key: str, max_response_bytes: int) -> CatalogueRequest:
        raise NotImplementedError(f"{self.provider} has no HTTP catalogue")

    def catalogue_rows(self, raw: bytes) -> list[dict[str, Any]]:
        raise NotImplementedError(f"{self.provider} has no HTTP catalogue")

    def catalogue_evidence_dataset(self, url: str) -> str | None:
        """The dataset whose catalogue request produced this evidence URL."""
        return None

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        return PASSTHROUGH

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        """Bounded diagnostic fields for a provider failure; never provider free text."""
        return None

    def member_outcome(self, exc: BaseException) -> tuple[str, str | None] | None:
        """A deterministic one-member historical outcome ``(outcome, reason)``, else None.

        Historical dates record these per member instead of retrying the date.
        """
        if isinstance(exc, MemberUnavailableError):
            return (
                ("missing", "no_source_row") if exc.absent else ("invalid", "row_structure_invalid")
            )
        return None

    def no_data_evidence(
        self, member: Mapping[str, Any], context: AcquisitionContext
    ) -> dict[str, Any] | None:
        """Official evidence that a member which used every no-data retry had no session.

        Asked once, after the last retry; ``None`` leaves the outcome blocked.
        """
        return None

    def pending_shared_fetch(self, dataset_key: str, trade_date: date) -> bool:
        """True when the next ``acquire`` fetches a native response every member shares."""
        return False

    def unlisted_requests(self, context: AcquisitionContext) -> list[tuple[str, AcquisitionResult]]:
        """Served rows of symbols outside the frozen list, as ``(symbol, result)``.

        Only a whole-market response serves them; member-scoped providers have none.
        """
        return []
