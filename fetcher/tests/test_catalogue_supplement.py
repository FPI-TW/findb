"""Listed US equities the Twelve Data catalogue serves beyond the official list (#629)."""

from findb_fetcher.full_market_universe import catalogue_supplement


def _row(symbol, mic="XNCM", kind="Common Stock", name="Acme Inc.", exchange=None):
    return {
        "symbol": symbol,
        "mic_code": mic,
        "exchange": exchange or ("NASDAQ" if mic.startswith("XN") and mic != "XNYS" else "NYSE"),
        "currency": "USD",
        "type": kind,
        "name": name,
    }


def test_only_listed_equities_off_the_list_and_outside_excluded_forms_are_kept():
    members = [{"symbol": "AAPL", "provider_symbol": "AAPL"}]
    catalogue = [
        _row("AAPL", "XNGS"),  # already listed
        _row("NEWC", "XNMS"),
        _row("BRK.C", "XNYS"),  # a share class keeps its dot
        _row("ADRX", "XNYS", "American Depositary Receipt", "Adr Holdings"),
        _row("OTCCO", "PINX", exchange="OTC"),  # OTC markets are out of scope
        _row("PREF", kind="Preferred Stock"),  # excluded type
        _row("ABC.WT", "XNYS"),  # warrant whatever its type
        _row("ABC.U", "XNYS"),  # unit
        _row("GPUS.PR.D", "XASE"),  # preferred series
        _row("C-R", "XNYS"),  # preferred series named with a dash
        _row("ACMEW"),  # Nasdaq fifth letter W: warrant
        _row("ACMER"),  # Nasdaq fifth letter R: right
        _row("ZVZZT"),  # Nasdaq test issue
        _row("ATEST", "XNYS"),  # NYSE test issue
        _row("FUNDX", name="Big Closed-End Fund"),  # excluded by name
        _row("NOTE", name="Acme 5% Notes due 2030"),
        _row("DUAL", "IEXG", exchange="IEX"),
        _row("DUAL", "XNYS"),  # the primary venue wins over IEX
    ]
    kept = catalogue_supplement(catalogue, members)
    assert [(row["symbol"], row["exchange"], row["classification"]) for row in kept] == [
        ("ADRX", "XNYS", "adr"),
        ("BRK.C", "XNYS", "ordinary"),
        ("DUAL", "XNYS", "ordinary"),
        ("NEWC", "XNAS", "ordinary"),
    ]
    assert all(row["mapping_status"] == "mapped" and row["currency"] == "USD" for row in kept)
