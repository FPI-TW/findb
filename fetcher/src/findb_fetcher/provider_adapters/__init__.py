"""Provider adapters for Full daily acquisition; see ``base`` for the boundary."""

from __future__ import annotations

from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    BatchContext,
    CatalogueRequest,
    DatedUniverse,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
    UniverseContext,
    UniverseSnapshot,
)
from findb_fetcher.provider_adapters.factset import FactSetAdapter
from findb_fetcher.provider_adapters.finlab import FinLabAdapter
from findb_fetcher.provider_adapters.taifex import TaifexAdapter
from findb_fetcher.provider_adapters.twelve_data import TwelveDataAdapter

ADAPTERS: dict[str, type[ProviderAdapter]] = {
    adapter.provider: adapter
    for adapter in (TwelveDataAdapter, FinLabAdapter, TaifexAdapter, FactSetAdapter)
}


def adapter_for(provider: str) -> ProviderAdapter:
    """A fresh adapter with its own native clients and per-round caches.

    A retired provider keeps the capability-free default: it can drain retained
    deliveries but has no acquisition.
    """
    adapter = ADAPTERS.get(provider)
    return adapter() if adapter is not None else ProviderAdapter()


__all__ = [
    "ADAPTERS",
    "PASSTHROUGH",
    "AcquisitionContext",
    "AcquisitionResult",
    "BatchContext",
    "CatalogueRequest",
    "DatedUniverse",
    "FactSetAdapter",
    "FailureKind",
    "FinLabAdapter",
    "Phase",
    "ProviderAdapter",
    "ProviderFailure",
    "TaifexAdapter",
    "TwelveDataAdapter",
    "UniverseContext",
    "UniverseSnapshot",
    "adapter_for",
]
