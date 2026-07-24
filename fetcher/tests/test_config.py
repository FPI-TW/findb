from __future__ import annotations

import pytest

from findb_fetcher.config import ConfigError, FetcherConfig


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SOURCE_API_URL", "https://source.example.test")
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "test-source-key")
    monkeypatch.setenv("FETCHER_REQUEST_TIMEOUT_SECONDS", value)

    with pytest.raises(ConfigError, match="finite"):
        FetcherConfig.from_env()
