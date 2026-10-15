"""Country catalogues retain per-request scope, governor and raw provenance."""

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
@pytest.mark.parametrize(
    "datasets", [["us_equity_eod"], ["hk_equity_eod"], ["us_equity_eod", "hk_equity_eod"]]
)
def test_country_catalogues_only_fetch_requested_scope(
    scoped_catalogue, monkeypatch, environment, profile, datasets
):
    runtime, _, _, requests, persisted, auth, _ = scoped_catalogue
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    runtime.execution_profile = profile

    def handle(request):
        requests.append(request)
        assert request.url.path == "/stocks"
        assert request.url.params["apikey"] == "offline-secret"
        return httpx.Response(200, json={"data": [{"symbol": request.url.params["country"]}]})

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(handle))
    rows = runtime._catalogue(dataset_keys=datasets)
    countries = {"us_equity_eod": "United States", "hk_equity_eod": "Hong Kong"}
    assert [r["symbol"] for r in rows] == [countries[d] for d in datasets]
    assert [r.url.params["country"] for r in requests] == [countries[d] for d in datasets]
    assert [p[1]["dataset_key"] for p in persisted] == datasets
    assert all(d in auth for d in datasets)
    assert len(runtime.catalogue_evidence) == len(datasets)
    for dataset in datasets:
        evidence = runtime._catalogue_evidence_for(dataset)
        assert len(evidence) == 1
        url, body = next(iter(evidence.items()))
        assert parse_qs(urlsplit(url).query) == {"country": [countries[dataset]]}
        assert json.loads(body)["data"][0]["symbol"] == countries[dataset]
        assert "offline-secret" not in url
    with runtime.state.connection() as db:
        assert db.execute("SELECT requests FROM full_quota WHERE window='2026-10-01'").fetchone()[
            0
        ] == len(datasets)


@pytest.mark.parametrize("failure", ["revoked", "stopped", "rate_limited", "oversize", "malformed"])
def test_second_country_failure_never_publishes_partial_evidence(scoped_catalogue, failure):
    runtime, _, authorized, requests, persisted, _, _ = scoped_catalogue
    allowed = True
    runtime.can_acquire = lambda: allowed
    old_evidence = {"previous": b"unchanged"}
    runtime.catalogue_evidence = old_evidence.copy()

    def handle(request):
        nonlocal allowed
        requests.append(request)
        if len(requests) == 1:
            if failure == "revoked":
                authorized.remove("hk_equity_eod")
            if failure == "stopped":
                allowed = False
            return httpx.Response(200, json={"data": []})
        if failure == "rate_limited":
            return httpx.Response(429, headers={"Retry-After": "60"}, json={"status": "error"})
        if failure == "oversize":
            return httpx.Response(200, headers={"Content-Length": str(17 * 1024**2)}, content=b"")
        return httpx.Response(200, json={"wrong": []})

    runtime.http.close()
    runtime.http = httpx.Client(transport=httpx.MockTransport(handle))
    with pytest.raises((ReadinessBlockedError, FullMarketProtocolError, QuotaBlockedError)):
        runtime._catalogue(dataset_keys=["us_equity_eod", "hk_equity_eod"])
    assert len(requests) == (1 if failure in ("revoked", "stopped") else 2)
    assert runtime.catalogue_evidence == old_evidence
    assert len(persisted) == 1 and persisted[0][1]["dataset_key"] == "us_equity_eod"


@pytest.mark.parametrize("datasets", [[], ["tw_equity_eod"], ["us_equity_eod", "uninstalled"]])
def test_invalid_catalogue_scope_fails_before_network(scoped_catalogue, datasets):
    runtime, _, _, _, persisted, _, _ = scoped_catalogue
    with pytest.raises(ReadinessBlockedError, match="scope"):
        runtime._catalogue(dataset_keys=datasets)
    assert persisted == []


def test_universe_submission_never_cross_attributes_country_raw(scoped_catalogue, monkeypatch):
    import findb_fetcher.full_market_runtime as implementation
    from test_full_market import _member

    runtime, source, _, _, persisted, _, clock = scoped_catalogue
    for feed in runtime.installed_feeds:
        feed["source_timezone"] = "America/New_York" if feed["market"] == "US" else "Asia/Hong_Kong"
    runtime.http.close()
    runtime.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [], "country": request.url.params["country"]}
            )
        )
    )
    runtime._snapshot = lambda url: url.encode()
    us = {**_member(), "classification": "ordinary"}
    hk = {**us, "symbol": "00001", "exchange": "XHKG", "currency": "HKD"}
    monkeypatch.setattr(
        implementation, "parse_nasdaq", lambda raw, other=False: [] if other else [us]
    )
    monkeypatch.setattr(
        implementation, "nasdaq_effective_date", lambda *a, **kw: clock.now().date()
    )
    monkeypatch.setattr(implementation, "parse_hkex", lambda *a, **kw: (clock.now().date(), [hk]))
    submitted = []
    source.submit = lambda body: submitted.append(body) or {"dataset_key": body["dataset_key"]}
    runtime.sync_universes(dataset_keys=["us_equity_eod", "hk_equity_eod"])
    countries = {"us_equity_eod": "United States", "hk_equity_eod": "Hong Kong"}
    for request in submitted:
        catalogues = [item for item in request["evidence"] if "/stocks?" in item["url"]]
        assert len(catalogues) == 1
        assert parse_qs(urlsplit(catalogues[0]["url"]).query)["country"] == [
            countries[request["dataset_key"]]
        ]
    for body, metadata in persisted:
        if body.startswith(b'{"data"'):
            assert json.loads(body)["country"] == countries[metadata["dataset_key"]]
