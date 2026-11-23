"""Index universes: FMP's own symbols and venues, bound to index datasets only."""

import pytest

from app.services.full_market import FullMarketError, submit_universe
from tests.test_full_market import _dataset, _universe


async def test_a_hong_kong_index_keeps_the_venue_its_provider_names(test_session):
    await _dataset(test_session, "hk_index_eod")
    body = _universe("hk_index_eod", 2)
    assert {member.exchange for member in body.members} == {"HKSE"}
    release = await submit_universe(test_session, body, allowed_datasets=["hk_index_eod"])
    assert release.member_count == 2


async def test_listed_hong_kong_members_still_name_their_exchange(test_session):
    await _dataset(test_session, "hk_equity_eod")
    body = _universe("hk_equity_eod", 1)
    member = body.members[0].model_copy(update={"exchange": "HKSE"})
    with pytest.raises(FullMarketError, match="HKEX/SEHK/XHKG"):
        await submit_universe(
            test_session,
            body.model_copy(update={"members": [member]}),
            allowed_datasets=["hk_equity_eod"],
        )


async def test_index_members_belong_to_index_datasets_only(test_session):
    await _dataset(test_session, "us_index_eod")
    body = _universe("us_equity_eod", 1)
    with pytest.raises(FullMarketError, match="asset class"):
        await submit_universe(
            test_session,
            body.model_copy(update={"dataset_key": "us_index_eod", "provider": "fmp"}),
            allowed_datasets=["us_index_eod"],
        )
