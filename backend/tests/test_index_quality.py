"""Indices whose open, high and low are vendor fills are written with a warning."""

from __future__ import annotations

from sqlalchemy import select

from app.models.canonical import Instrument, MarketDataEOD
from app.models.registry import DQIssue
from app.services.index_quality import CLOSE_ONLY_INDICES, close_only
from tests.test_index_names import _row, _write, index_dataset  # noqa: F401


def test_the_close_only_list_names_the_eight_compared_indices():
    assert {symbol for _, symbol in CLOSE_ONLY_INDICES} == {
        "^NYA",
        "^RUT",
        "^RUA",
        "^RLG",
        "^RLV",
        "^SPXEW",
        "^PSE",
        "^XAX",
    }
    assert close_only("us", " ^nya ") and not close_only("US", "^GSPC")
    assert not close_only(None, "^NYA") and not close_only("HK", "^NYA")


async def test_a_close_only_index_is_written_as_served_with_a_warning(
    test_session,
    index_dataset,  # noqa: F811
):
    run_id = await _write(test_session, _row("^NYA"), _row("^GSPC"))

    issues = list(await test_session.scalars(select(DQIssue).where(DQIssue.run_id == run_id)))
    assert [(i.issue_type, i.severity, i.raw_data["symbol"]) for i in issues] == [
        ("INDEX_OHL_UNRELIABLE", "warning", "^NYA")
    ]
    # A warning never stops the write: both rows are stored with every field.
    rows = (
        await test_session.execute(
            select(Instrument.symbol, MarketDataEOD.open, MarketDataEOD.close).join(
                MarketDataEOD, MarketDataEOD.instrument_id == Instrument.instrument_id
            )
        )
    ).all()
    assert {symbol for symbol, _, _ in rows} == {"^NYA", "^GSPC"}
    assert all(open_ is not None and close is not None for _, open_, close in rows)


async def _stored(session, symbol: str) -> tuple[float, int]:
    session.expire_all()
    row = (
        await session.execute(
            select(MarketDataEOD.close, MarketDataEOD.volume)
            .join(Instrument, Instrument.instrument_id == MarketDataEOD.instrument_id)
            .where(Instrument.symbol == symbol)
        )
    ).one()
    return float(row.close), row.volume


async def test_a_preliminary_zero_volume_never_replaces_a_filled_one(
    test_session,
    index_dataset,  # noqa: F811
):
    # The daily fetch stored FMP's preliminary 0; the next day's refetch fills it in.
    await _write(test_session, _row("^HSI", close="105", volume=0))
    await _write(test_session, _row("^HSI", close="105.01", volume=3841700000))
    assert await _stored(test_session, "^HSI") == (105.01, 3841700000)

    # A later fetch serving 0 again still replaces the prices, but not the volume.
    await _write(test_session, _row("^HSI", close="105.02", volume=0))
    assert await _stored(test_session, "^HSI") == (105.02, 3841700000)

    # A corrected non-zero volume does replace it.
    await _write(test_session, _row("^HSI", close="105.02", volume=3900000000))
    assert await _stored(test_session, "^HSI") == (105.02, 3900000000)


def test_only_preliminary_index_sources_keep_a_stored_volume():
    from app.services.index_quality import zero_volume_keeps_stored

    assert zero_volume_keeps_stored("fmp", "index") is True
    assert zero_volume_keeps_stored(" FMP ", "Index") is True
    # Another provider's 0, or a stock's 0 (no trade), is a value like any other.
    assert zero_volume_keeps_stored("twelve_data", "index") is False
    assert zero_volume_keeps_stored("fmp", "equity") is False
    assert zero_volume_keeps_stored(None, None) is False
