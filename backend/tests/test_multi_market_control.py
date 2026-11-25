"""One control may group feeds of different markets, timezones and slots (FMP indices)."""

from app.services.market_freshness import list_market_freshness
from scripts.seed_data import seed_datasets


def _keys(rows) -> set[str]:
    return {row.scheduler_key for row in rows}


async def test_a_control_appears_under_each_slot_and_market_of_its_feeds(test_session):
    await seed_datasets(test_session)

    (fmp,) = [row for row in await list_market_freshness(test_session) if row.provider == "fmp"]
    assert fmp.markets == ("HK", "US")
    assert fmp.market in fmp.markets
    assert {feed.dataset_key: feed.schedule.source_timezone for feed in fmp.feeds} == {
        "hk_index_eod": "Asia/Hong_Kong",
        "us_index_eod": "Asia/Taipei",
        "us_dollar_index_eod": "America/New_York",
    }
    # The control's own slot is its earliest feed's; filtering reads every feed's.
    assert fmp.slot_id == "western_markets_window"
    assert fmp.slot_ids == ("asia_pacific_markets_window", "western_markets_window")
    for slot in ("western_markets_window", "asia_pacific_markets_window"):
        assert "full_market_fmp_v1" in _keys(
            await list_market_freshness(test_session, slot_id=slot)
        )
    assert "full_market_fmp_v1" not in _keys(
        await list_market_freshness(test_session, slot_id="taiwan_market_window")
    )
    for market in ("HK", "US"):
        assert "full_market_fmp_v1" in _keys(
            await list_market_freshness(test_session, market=market)
        )
    # A single-market control keeps one market.
    (finlab,) = [
        row for row in await list_market_freshness(test_session) if row.provider == "finlab"
    ]
    assert finlab.markets == ("TW",)
    assert finlab.slot_ids == (finlab.slot_id,)


async def test_admin_freshness_lists_every_market_of_a_control(client, admin_headers, test_session):
    await seed_datasets(test_session)
    response = await client.get("/api/v1/admin/market-freshness", headers=admin_headers)
    assert response.status_code == 200, response.text
    rows = {row["scheduler_key"]: row for row in response.json()["data"]}
    assert rows["full_market_fmp_v1"]["markets"] == ["HK", "US"]
    assert rows["full_market_twelve_data_v1"]["markets"] == ["US"]
    assert rows["full_market_fmp_v1"]["slot_ids"] == [
        "asia_pacific_markets_window",
        "western_markets_window",
    ]
    assert rows["full_market_twelve_data_v1"]["slot_ids"] == [
        rows["full_market_twelve_data_v1"]["slot_id"]
    ]
