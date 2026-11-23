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


def catalog_feed(provider: str, dataset_key: str) -> FeedDefinition | None:
    return next(
        (
            f
            for f in provider_catalog().feeds
            if (f.provider, f.dataset_key) == (provider, dataset_key)
        ),
        None,
    )


def historical_cohort(provider: str, dataset_key: str) -> str | None:
    """How the feed's historical dates freeze their members; None when unsupported."""
    feed = catalog_feed(provider, dataset_key)
    return feed.historical_cohort if feed is not None else None


def universe_mode(provider: str, dataset_key: str) -> str | None:
    """``dated`` when a plan needs a universe built for its exact trade date."""
    feed = catalog_feed(provider, dataset_key)
    return feed.universe_mode if feed is not None else None


def delivers_unlisted(provider: str, dataset_key: str) -> bool:
    """Whether the feed's Fetcher delivers served rows of symbols outside the list."""
    feed = catalog_feed(provider, dataset_key)
    return feed is not None and feed.unlisted_symbols == "deliver"


def calendar_required(provider: str, dataset_key: str) -> bool:
    """Whether the feed's dates wait for a published exchange calendar."""
    feed = catalog_feed(provider, dataset_key)
    return feed is not None and feed.calendar_required


def dataset_calendar_market(dataset_key: str, market: str) -> str:
    """The dataset's catalog calendar market (shared by its feeds, the loader checks).

    Datasets without a catalog feed (retired Shioaji minute bars) keep their market.
    """
    return next(
        (f.calendar_market for f in provider_catalog().feeds if f.dataset_key == dataset_key),
        market,
    )
