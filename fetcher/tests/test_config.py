from __future__ import annotations

import pytest

from findb_fetcher.config import ConfigError, FetcherConfig


def _set_required(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("SOURCE_API_URL", url)
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "test-source-key")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _set_required(monkeypatch, "https://source.example.test")
    monkeypatch.setenv("FETCHER_REQUEST_TIMEOUT_SECONDS", value)

    with pytest.raises(ConfigError, match="finite"):
        FetcherConfig.from_env()


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "http://source.example.test",
        "https://user:password@source.example.test",
        "https://@source.example.test",
        "https://source.example.test/api/v1",
        "https://source.example.test?query=value",
        "https://source.example.test?",
        "https://source.example.test#fragment",
        "https://source.example.test#",
        "https://source.example.test:",
        "https://source.example.test%2f.evil.test",
        "https://source.example.test%00",
        "https://source.example.test%3A443",
        "https://.example",
        "https://example..test",
        "https://foo_bar.example",
        "https://-source.example",
        "https://source-.example",
        "https://999.999.999.999",
        "https://source.example.test:0",
        "https://source.example.test:65536",
        " https://source.example.test",
        "https://source.example.test\n",
        "https:\\\\source.example.test",
    ],
)
def test_source_api_url_rejects_non_https_origins(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    _set_required(monkeypatch, url)

    with pytest.raises(ConfigError, match="valid HTTPS origin"):
        FetcherConfig.from_env()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://source.example.test", "https://source.example.test"),
        ("https://source.example.test/", "https://source.example.test"),
        ("https://source.example.test:8443", "https://source.example.test:8443"),
        ("https://SOURCE.EXAMPLE.TEST/", "https://source.example.test"),
        ("https://[2001:db8::1]:8443/", "https://[2001:db8::1]:8443"),
        ("https://[2001:0db8:0:0::1]/", "https://[2001:db8::1]"),
    ],
)
def test_source_api_url_accepts_and_normalizes_https_origins(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expected: str,
) -> None:
    _set_required(monkeypatch, url)

    assert FetcherConfig.from_env().source_api_url == expected


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_app_environment_is_the_only_environment_input(monkeypatch, environment):
    from findb_fetcher.config import app_environment

    _set_required(monkeypatch, "https://source.example.test")
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    monkeypatch.delenv("DEPLOYMENT_TARGET", raising=False)
    assert app_environment() == environment
    FetcherConfig.from_env()
    # The retired legacy metadata is ignored entirely, whether it agrees or not.
    monkeypatch.setenv(
        "DEPLOYMENT_TARGET", "production" if environment != "production" else "staging"
    )
    assert app_environment() == environment
    FetcherConfig.from_env()
    monkeypatch.delenv("APP_ENVIRONMENT")
    assert app_environment() == "local"
    assert app_environment("staging") == "staging"
    monkeypatch.setenv("APP_ENVIRONMENT", "qa")
    with pytest.raises(ConfigError, match="must be local, staging, or production"):
        app_environment()
