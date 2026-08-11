"""Source credential scope must remain bounded to the retained feeds."""

import pytest

from app.services.ingestion import IngestionService
from app.services.source_clients import (
    SourceProviderScopeError,
    normalize_provider_scope,
)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("twelve_data", ["us_equity_eod"]),
        ("finlab", ["tw_equity_eod"]),
        ("shioaji", ["tw_equity_minute", "tw_etf_minute"]),
    ],
)
def test_provider_scope_expands_null_to_complete_mapping(provider, expected):
    assert normalize_provider_scope(provider, None) == (provider, expected)


def test_provider_scope_rejects_unknown_or_widened_mapping():
    with pytest.raises(SourceProviderScopeError):
        normalize_provider_scope("legacy_provider", None)
    with pytest.raises(SourceProviderScopeError):
        normalize_provider_scope("finlab", ["tw_equity_eod", "retired_dataset"])
    with pytest.raises(SourceProviderScopeError):
        normalize_provider_scope("shioaji", [])


def test_provider_scope_allows_non_empty_least_privilege_subset():
    assert normalize_provider_scope("shioaji", ["tw_etf_minute"]) == (
        "shioaji",
        ["tw_etf_minute"],
    )


@pytest.mark.asyncio
async def test_list_datasets_fails_closed_for_null_scope():
    class Session:
        info = {"allowed_datasets": None}

        async def execute(self, _statement):  # pragma: no cover - must not be called
            raise AssertionError("NULL source scope must not query the registry")

    assert await IngestionService(Session()).list_datasets() == []
