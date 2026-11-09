"""Backend's validated catalog projection, packaged with the application image."""

import json
from functools import lru_cache
from pathlib import Path

from app.provider_catalog import FeedDefinition, ProviderCatalog

METADATA_PATH = Path(__file__).resolve().parents[1] / "generated/provider_metadata.v1.json"


@lru_cache(maxsize=1)
def provider_catalog() -> ProviderCatalog:
    # No source checkout / contract files are required by the deployed image.
    # The exporter validates published contracts; the runtime verifies its projection.
    raw = json.loads(METADATA_PATH.read_text())
    digest = raw.pop("source_sha256")
    scopes = raw.pop("source_scopes")
    catalog = ProviderCatalog.model_validate(raw)
    if catalog.digest() != digest or source_scopes(catalog) != scopes:
        raise ValueError("Invalid generated provider metadata")
    return catalog


def source_scopes(catalog: ProviderCatalog | None = None) -> dict[str, list[str]]:
    catalog = catalog if catalog is not None else provider_catalog()
    return {
        provider.provider: sorted(
            feed.dataset_key
            for feed in catalog.feeds
            if feed.provider == provider.provider and feed.capabilities.source
        )
        for provider in catalog.providers
        if any(f.provider == provider.provider and f.capabilities.source for f in catalog.feeds)
    }


def acquisition_feeds() -> tuple[FeedDefinition, ...]:
    return tuple(f for f in provider_catalog().feeds if f.capabilities.acquisition)


def acquisition_feed(provider: str, dataset_key: str) -> FeedDefinition | None:
    return next(
        (f for f in acquisition_feeds() if (f.provider, f.dataset_key) == (provider, dataset_key)),
        None,
    )
