import pytest

from app.vocabulary import normalize_asset_class, normalize_market


def test_normalize_market_accepts_unknown_valid_code() -> None:
    assert normalize_market(" ln ") == "LN"


def test_normalize_market_rejects_invalid_format() -> None:
    with pytest.raises(ValueError):
        normalize_market("new york")


def test_normalize_asset_class_accepts_unknown_valid_code() -> None:
    assert normalize_asset_class(" warrant ") == "warrant"


def test_normalize_asset_class_rejects_invalid_format() -> None:
    with pytest.raises(ValueError):
        normalize_asset_class("stock option")
