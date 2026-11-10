"""FactSet: one native query for the frozen cohort, delivered member by member.

FactSet keeps the optional batch interface instead of the member loop. Its
listings are priced by one fenced database query whose journaled snapshot and
receipts ``FactSetEOD`` replays without querying again; issuing that query per
member would multiply native calls by the cohort size and break replay of the
existing journal keys (``factset-eod:snapshot``, ``factset-eod:receipt:*``).
"""

from __future__ import annotations

from typing import Any

from findb_fetcher import factset_eod
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    BatchContext,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
)
from findb_fetcher.providers.factset import (
    FactSetConfig,
    FactSetGateway,
    FactSetOperationalError,
)


class FactSetAdapter(ProviderAdapter):
    provider = "factset"
    batch = True
    batch_snapshot_key = "factset-eod:snapshot"
    batch_pending_reason = "factset_no_source_row"

    def acquire_batch(self, members: list[dict[str, Any]], context: BatchContext) -> dict[str, Any]:
        return factset_eod.FactSetEOD(
            FactSetGateway(FactSetConfig.from_env()),
            context.delivery,
            context.raw_store,
            on_receipt=context.on_receipt,
        ).run(
            day=context.trade_date,
            members=members,
            dataset=context.dataset_key,
            deliveries=context.deliveries,
            historical=False,
            defer_missing=context.defer_missing,
        )

    def batch_status(self, evidence: dict[str, Any]) -> str:
        return factset_eod.completion_status(evidence)

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        if phase in {"round", "maintenance"} and isinstance(exc, FactSetOperationalError):
            return ProviderFailure(
                FailureKind.ROUND_RETRY if exc.retryable else FailureKind.WORK_FAILED,
                code=exc.code,
            )
        return PASSTHROUGH
