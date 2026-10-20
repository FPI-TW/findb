"""Composed fixture clocks preserve grant expiry and cannot leak to other tests."""

import time

from tests.provider_work_clock import account_clock, dispatch_clock


def test_account_offset_preserves_dispatch_expiry_and_restores_module_clocks(monkeypatch):
    from findb_fetcher import acquisition, full_market_client

    acquisition_time, source_time = acquisition.time, full_market_client.time
    real_monotonic = time.monotonic
    with monkeypatch.context() as patch:
        dispatch = dispatch_clock(patch)
        offset = account_clock(patch)
        offset[0] = 120
        assert acquisition.time.monotonic is full_market_client.time.monotonic
        assert time.monotonic is real_monotonic
        assert full_market_client.time.time is time.time
        assert acquisition.time.sleep is time.sleep
        assert acquisition.time.time() >= time.time() + 119
        now = acquisition.time.time()
        grant = acquisition.Authorization(dispatch[0], now, now, provider="taifex")
        assert grant.valid()
        dispatch[0] += 0.051
        assert not grant.valid(), "account offset must not reset the dispatch expiry clock"
    assert acquisition.time is acquisition_time
    assert full_market_client.time is source_time
