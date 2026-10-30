"""The US country catalogue retains scope, governor and raw provenance; HK is retired."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_runtime import ReadinessBlockedError
from findb_fetcher.full_market_state import QuotaBlockedError
from test_full_market import _runtime
from test_full_market_finlab_rate import _Clock


@pytest.fixture
def scoped_catalogue(tmp_path, monkeypatch):
    runtime, source, raw = _runtime(tmp_path)
    clock = _Clock(monkeypatch)
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "offline-secret")
    monkeypatch.setenv("FETCHER_CONSUMER_PROFILE", "maintenance")
    runtime.installed_feeds.append(
        {**runtime.installed_feeds[0], "dataset_key": "hk_equity_eod", "market": "HK"}
    )
    authorized = {"us_equity_eod", "hk_equity_eod"}
    authorization_calls = []
    native_authorization = source.authorization

    def authorization(method, path, **kwargs):
        authorization_calls.append(kwargs.get("required_dataset"))
        # Exercise the real Source client's membership enforcement with detached replies.
        holder = type(
            "SourceReply",
            (),
            {
                "request": lambda *a, **kw: {
                    "authorized_dataset_keys": sorted(authorized),
                    "_authorization": native_authorization(method, path),
                }
            },
        )()
        return FullMarketSourceClient.authorization(holder, method, path, **kwargs)

    source.authorization = authorization
    requests, persisted = [], []
    original_persist = raw.persist

    def persist(body, **kwargs):
        persisted.append((body, kwargs))
        return original_persist(body, **kwargs)

    raw.persist = persist
    yield runtime, source, authorized, requests, persisted, authorization_calls, clock
    runtime.close()


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
def test_us_catalogue_uses_header_credential_and_raw_provenance(
    scoped_catalogue, monkeypatch, environment, profile
):
    runtime, _, _, requests, persisted, auth, _ = scoped_catalogue
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    runtime.execution_profile = profile

    def handle(request):
        requests.append(request)
        assert request.url.path == "/stocks"
        assert "apikey" not in request.url.params
        assert "offline-secret" not in str(request.url)
        assert request.headers["authorization"] == "apikey offline-secret"
        return httpx.Response(200, json={"data": [{"symbol": request.url.params["country"]}]})

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(handle))
    rows = runtime._catalogue(dataset_keys=["us_equity_eod"])
    assert [r["symbol"] for r in rows] == ["United States"]
    assert [p[1]["dataset_key"] for p in persisted] == ["us_equity_eod"]
    assert "us_equity_eod" in auth
    evidence = runtime._catalogue_evidence_for("us_equity_eod")
    assert len(evidence) == 1
    url, body = next(iter(evidence.items()))
    assert parse_qs(urlsplit(url).query) == {"country": ["United States"]}
    assert json.loads(body)["data"][0]["symbol"] == "United States"
    assert "offline-secret" not in url
    with runtime.state.connection() as db:
        assert (
            db.execute("SELECT requests FROM full_quota WHERE window='2026-10-01'").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("failure", ["revoked", "stopped", "rate_limited", "oversize", "malformed"])
def test_catalogue_failure_never_publishes_evidence(scoped_catalogue, failure):
    from findb_fetcher.provider_work import ProviderWorkAcquisitionStopped

    runtime, _, authorized, requests, persisted, _, _ = scoped_catalogue
    if failure == "revoked":
        authorized.remove("us_equity_eod")
    if failure == "stopped":
        runtime.can_acquire = lambda: False
    old_evidence = {"previous": b"unchanged"}
    runtime.catalogue_evidence = old_evidence.copy()

    def handle(request):
        requests.append(request)
        if failure == "rate_limited":
            return httpx.Response(429, headers={"Retry-After": "60"}, json={"status": "error"})
        if failure == "oversize":
            return httpx.Response(200, headers={"Content-Length": str(17 * 1024**2)}, content=b"")
        return httpx.Response(200, json={"wrong": []})

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(handle))
    error_type = (
        ProviderWorkAcquisitionStopped
        if failure == "stopped"
        else (ReadinessBlockedError, FullMarketProtocolError, QuotaBlockedError)
    )
    with pytest.raises(error_type):
        runtime._catalogue(dataset_keys=["us_equity_eod"])
    assert len(requests) == (0 if failure in ("revoked", "stopped") else 1)
    assert runtime.catalogue_evidence == old_evidence
    assert persisted == []


@pytest.mark.parametrize(
    "datasets",
    [[], ["tw_equity_eod"], ["us_equity_eod", "uninstalled"], ["hk_equity_eod"]],
)
def test_invalid_catalogue_scope_fails_before_network(scoped_catalogue, datasets):
    runtime, _, _, requests, persisted, _, _ = scoped_catalogue
    with pytest.raises(ReadinessBlockedError, match="scope"):
        runtime._catalogue(dataset_keys=datasets)
    assert requests == []
    assert persisted == []


def test_retired_hk_scope_never_refreshes_hk_universe(scoped_catalogue, monkeypatch):
    import findb_fetcher.full_market_runtime as implementation
    from test_full_market import _member

    runtime, source, _, requests, persisted, _, clock = scoped_catalogue
    # Older controls/credentials may still list HK; only the US feed is installed.
    runtime.installed_feeds[:] = [
        feed for feed in runtime.installed_feeds if feed["dataset_key"] == "us_equity_eod"
    ]
    runtime.installed_feeds[0]["source_timezone"] = "America/New_York"

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [], "country": request.url.params["country"]})

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(handle))
    snapshots = []
    runtime._snapshot = lambda url: snapshots.append(url) or url.encode()
    us = {**_member(), "classification": "ordinary"}
    monkeypatch.setattr(
        implementation, "parse_nasdaq", lambda raw, other=False: [] if other else [us]
    )
    monkeypatch.setattr(
        implementation, "nasdaq_effective_date", lambda *a, **kw: clock.now().date()
    )
    monkeypatch.setattr(
        implementation,
        "parse_hkex",
        lambda *a, **kw: pytest.fail("Twelve Data must not parse the HKEX list"),
    )
    submitted = []
    source.submit = lambda body: submitted.append(body) or {"dataset_key": body["dataset_key"]}
    runtime.sync_universes(dataset_keys=["us_equity_eod", "hk_equity_eod"])
    assert [body["dataset_key"] for body in submitted] == ["us_equity_eod"]
    assert implementation.HKEX_URL not in snapshots
    assert [r.url.params["country"] for r in requests] == ["United States"]
    catalogues = [item for item in submitted[0]["evidence"] if "/stocks?" in item["url"]]
    assert len(catalogues) == 1
    assert parse_qs(urlsplit(catalogues[0]["url"]).query)["country"] == ["United States"]
    assert {metadata["dataset_key"] for _, metadata in persisted} == {"us_equity_eod"}
