from __future__ import annotations

import hashlib
import json
import tomllib
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabDatasetBundle,
    FinLabDatasetConfig,
    FinLabDatasetTable,
    FinLabPayloadError,
    FinLabSdkError,
    FinLabSdkGateway,
    FinLabSymbol,
    build_finlab_dataset_bundle,
    build_market_eod_request,
    prepare_market_eod_delivery,
)
from findb_fetcher.raw_storage import RawObject

TARGET_DATE = date(2026, 7, 29)


def _config() -> FinLabDatasetConfig:
    return FinLabDatasetConfig(
        dataset_key="tw_equity_eod",
        market="TW",
        asset_class="equity",
        currency="TWD",
        field_datasets={
            "open": "price:開盤價",
            "high": "price:最高價",
            "low": "price:最低價",
            "close": "price:收盤價",
            "volume": "price:成交股數",
        },
        symbols=(
            FinLabSymbol(source_symbol="2330", canonical_symbol="2330"),
            FinLabSymbol(source_symbol="2317", canonical_symbol="2317"),
        ),
    )


def _tables() -> dict[str, FinLabDatasetTable]:
    return {
        "open": FinLabDatasetTable(("2026-07-29",), ("2330", "2317"), ((1000, 200),)),
        "high": FinLabDatasetTable(("2026-07-29",), ("2330", "2317"), ((1010, 205),)),
        "low": FinLabDatasetTable(("2026-07-29",), ("2330", "2317"), ((995, 198),)),
        "close": FinLabDatasetTable(("2026-07-29",), ("2330", "2317"), ((1005, 202),)),
        "volume": FinLabDatasetTable(("2026-07-29",), ("2330", "2317"), ((123456, 654321),)),
    }


def test_bundle_bytes_are_deterministic_and_request_is_reviewed_scope_only(
    contracts_dir: Path,
) -> None:
    first = build_finlab_dataset_bundle(config=_config(), target_date=TARGET_DATE, tables=_tables())
    second = build_finlab_dataset_bundle(
        config=_config(), target_date=TARGET_DATE, tables=_tables()
    )

    assert first.raw_bytes == second.raw_bytes
    assert first.sha256 == hashlib.sha256(first.raw_bytes).hexdigest()
    request = build_market_eod_request(
        first,
        fetched_at=datetime(2026, 7, 29, 7, tzinfo=timezone.utc),
    )
    ContractRegistry(contracts_dir).validate("market_eod", 1, request)
    assert request["source"] == "finlab"
    assert request["payload"]["batch"]["data_date"] == "2026-07-29"
    assert request["payload"]["batch"]["delivery_mode"] == "full_snapshot"
    assert [row["symbol"] for row in request["payload"]["data"]] == ["2317", "2330"]
    assert all(row["volume"] is not None for row in request["payload"]["data"])
    assert len(json.dumps(request, ensure_ascii=False).encode("utf-8")) < 1024 * 1024


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda tables: tables.__setitem__(
                "close", FinLabDatasetTable(("2026-07-28",), ("2330", "2317"), ((1, 2),))
            ),
            "target date",
        ),
        (
            lambda tables: tables.__setitem__(
                "close",
                FinLabDatasetTable(
                    ("2026-07-29", "2026-07-29"), ("2330", "2317"), ((1, 2), (1, 2))
                ),
            ),
            "duplicate dates",
        ),
        (
            lambda tables: tables.__setitem__(
                "close", FinLabDatasetTable(("2026-07-29",), ("2330",), ((1,),))
            ),
            "grids do not match",
        ),
        (
            lambda tables: tables.__setitem__(
                "open", FinLabDatasetTable(("2026-07-29",), ("2330", "2317"), ((float("nan"), 1),))
            ),
            "finite",
        ),
    ],
)
def test_bundle_rejects_unreviewable_or_invalid_grids(mutate, message: str) -> None:
    tables = _tables()
    mutate(tables)
    with pytest.raises(FinLabPayloadError, match=message):
        build_finlab_dataset_bundle(config=_config(), target_date=TARGET_DATE, tables=tables)


def test_prepare_persists_raw_bundle_before_request_provenance() -> None:
    bundle = build_finlab_dataset_bundle(
        config=_config(), target_date=TARGET_DATE, tables=_tables()
    )
    events: list[str] = []

    class Store:
        def persist(
            self,
            raw_bytes: bytes,
            *,
            dataset_key: str,
            source_symbol: str,
            provider: str = "twelve_data",
        ) -> RawObject:
            events.append("persist")
            assert raw_bytes == bundle.raw_bytes
            assert (dataset_key, source_symbol, provider) == (
                "tw_equity_eod",
                "dataset_bundle",
                "finlab",
            )
            return RawObject(
                "r2://0123456789abcdef0123456789abcdef/findb-fetcher-raw/x.json",
                bundle.sha256,
                len(raw_bytes),
            )

    request = prepare_market_eod_delivery(
        bundle,
        fetched_at=datetime(2026, 7, 29, 7, tzinfo=timezone.utc),
        raw_store=Store(),
    )
    assert events == ["persist"]
    assert request["payload"]["batch"]["source_raw_sha256"] == bundle.sha256
    assert request["idempotency_key"].startswith("finlab:")


def test_prepare_rejects_raw_store_integrity_mismatch() -> None:
    bundle = build_finlab_dataset_bundle(
        config=_config(), target_date=TARGET_DATE, tables=_tables()
    )

    class Store:
        def persist(
            self,
            raw_bytes: bytes,
            *,
            dataset_key: str,
            source_symbol: str,
            provider: str = "twelve_data",
        ) -> RawObject:
            return RawObject(
                "r2://0123456789abcdef0123456789abcdef/findb-fetcher-raw/x.json",
                "0" * 64,
                len(raw_bytes),
            )

    with pytest.raises(FinLabPayloadError, match="does not match"):
        prepare_market_eod_delivery(
            bundle,
            fetched_at=datetime(2026, 7, 29, 7, tzinfo=timezone.utc),
            raw_store=Store(),
        )


def test_disabled_manifest_uses_only_the_reviewed_finlab_pilot_universe() -> None:
    configs = Path(__file__).parents[1] / "configs"
    schedule = json.loads((configs / "daily_scheduler.v2.json").read_text(encoding="utf-8"))
    finlab = next(feed for feed in schedule["feeds"] if feed["provider"] == "finlab")

    assert finlab["enabled"] is True
    assert finlab["universe_file"] == "finlab_tw_review_required.v1.json"
    universe = load_finlab_universe(configs / finlab["universe_file"])
    assert universe.provider == "finlab"
    assert universe.dataset == "tw_equity_eod"
    assert universe.market == "TW"
    assert universe.asset_class == "equity"
    assert universe.currency == "TWD"
    assert {item.source_symbol for item in universe.symbols} == {"2330", "2317"}


def test_finlab_dependency_stays_on_token_compatible_release() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert config["project"]["optional-dependencies"]["finlab"] == ["finlab==1.5.7"]


class _Loc:
    def __init__(self, values: dict[tuple[object, str], object]) -> None:
        self._values = values

    def __getitem__(self, key: tuple[object, str]) -> object:
        return self._values[key]


class _Frame:
    def __init__(self, index: tuple[object, ...], columns: tuple[str, ...]) -> None:
        self.index = index
        self.columns = columns
        self.loc = _Loc({(index[0], "2330"): 1000, (index[0], "2317"): 200})


def test_sdk_gateway_logs_in_headlessly_and_binds_the_returned_frame_to_target_date() -> None:
    calls: list[object] = []
    frame = _Frame((TARGET_DATE,), ("2330", "2317"))
    sdk = SimpleNamespace(
        login=lambda token: calls.append(("login", token)),
        data=SimpleNamespace(get=lambda dataset: calls.append(("get", dataset)) or frame),
    )
    gateway = FinLabSdkGateway(api_token="test-token", _sdk=sdk)

    table = gateway.fetch_dataset(
        "price:開盤價",
        target_date=TARGET_DATE,
        symbols=("2330", "2317"),
    )

    assert calls == [("login", "test-token"), ("get", "price:開盤價")]
    assert table == FinLabDatasetTable(("2026-07-29",), ("2330", "2317"), ((1000, 200),))
    assert "test-token" not in repr(gateway)


@pytest.mark.parametrize(
    ("index", "columns", "message"),
    [
        ((date(2026, 7, 28),), ("2330", "2317"), "target date"),
        (("2026-07-29",), ("2330", "2317"), "index must contain date"),
        ((TARGET_DATE, TARGET_DATE), ("2330", "2317"), "duplicate date"),
        ((TARGET_DATE,), ("2330", "2330"), "duplicate symbol"),
    ],
)
def test_sdk_gateway_rejects_unsafe_dataframe_axes(index, columns, message: str) -> None:
    frame = _Frame(index, columns)
    sdk = SimpleNamespace(login=lambda token: None, data=SimpleNamespace(get=lambda dataset: frame))

    with pytest.raises(FinLabSdkError, match=message):
        FinLabSdkGateway(api_token="test-token", _sdk=sdk).fetch_dataset(
            "price:開盤價",
            target_date=TARGET_DATE,
            symbols=("2330", "2317"),
        )


def test_sdk_gateway_sanitizes_provider_exceptions_that_contain_the_token() -> None:
    token = "token-that-must-not-leak"

    def fail_login(received: str) -> None:
        raise RuntimeError(f"provider rejected {received}")

    sdk = SimpleNamespace(login=fail_login, data=SimpleNamespace(get=lambda dataset: None))
    with pytest.raises(FinLabSdkError) as raised:
        FinLabSdkGateway(api_token=token, _sdk=sdk).fetch_dataset(
            "price:開盤價",
            target_date=TARGET_DATE,
            symbols=("2330", "2317"),
        )

    assert token not in str(raised.value)
    assert token not in "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("failure_location", ["index", "row"])
def test_sdk_gateway_sanitizes_dataframe_exceptions_that_contain_the_token(
    failure_location: str,
) -> None:
    token = "token-that-must-not-leak"

    class FailingIndex:
        def __iter__(self):
            raise RuntimeError(token)

    class FailingLoc:
        def __getitem__(self, key: tuple[object, str]) -> object:
            raise RuntimeError(token)

    frame = SimpleNamespace(
        index=FailingIndex() if failure_location == "index" else (TARGET_DATE,),
        columns=("2330",),
        loc=FailingLoc(),
    )
    sdk = SimpleNamespace(
        login=lambda received: None, data=SimpleNamespace(get=lambda dataset: frame)
    )

    with pytest.raises(FinLabSdkError) as raised:
        FinLabSdkGateway(api_token=token, _sdk=sdk).fetch_dataset(
            "price:開盤價",
            target_date=TARGET_DATE,
            symbols=("2330",),
        )

    assert token not in str(raised.value)
    assert token not in "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None


def test_sdk_gateway_selects_only_reviewed_symbols_and_rejects_missing_members() -> None:
    index = (TARGET_DATE,)
    frame = SimpleNamespace(
        index=index,
        columns=("2330", "0050", "2317"),
        loc=_Loc(
            {
                (TARGET_DATE, "2330"): 1000,
                (TARGET_DATE, "0050"): 200,
                (TARGET_DATE, "2317"): 300,
            }
        ),
    )
    sdk = SimpleNamespace(login=lambda token: None, data=SimpleNamespace(get=lambda dataset: frame))
    gateway = FinLabSdkGateway(api_token="test-token", _sdk=sdk)

    table = gateway.fetch_dataset(
        "price:開盤價",
        target_date=TARGET_DATE,
        symbols=("2330", "2317"),
    )

    assert table.symbols == ("2330", "2317")
    assert table.values == ((1000, 300),)
    with pytest.raises(FinLabSdkError, match="missing reviewed symbols"):
        gateway.fetch_dataset(
            "price:開盤價",
            target_date=TARGET_DATE,
            symbols=("2330", "9999"),
        )


def test_bundle_cannot_be_mutated_or_constructed_with_inconsistent_integrity_data() -> None:
    bundle = build_finlab_dataset_bundle(
        config=_config(), target_date=TARGET_DATE, tables=_tables()
    )
    with pytest.raises(TypeError):
        bundle.field_values["close"]["2330"] = "1"  # type: ignore[index]

    bad_fields = {field: dict(values) for field, values in bundle.field_values.items()}
    bad_fields["close"]["2330"] = "1"
    with pytest.raises(FinLabPayloadError, match="must agree"):
        FinLabDatasetBundle(
            config=bundle.config,
            target_date=bundle.target_date,
            field_values=bad_fields,
            raw_bytes=bundle.raw_bytes,
            sha256=bundle.sha256,
        )


def test_asset_class_has_a_fixed_source_dataset_key() -> None:
    config = _config()
    with pytest.raises(FinLabConfigError, match="fixed FinLab Source dataset key"):
        FinLabDatasetConfig(
            dataset_key="tw_etf_eod",
            market=config.market,
            asset_class=config.asset_class,
            currency=config.currency,
            field_datasets=config.field_datasets,
            symbols=config.symbols,
        )

    wrong_fields = dict(config.field_datasets)
    wrong_fields["open"] = "price:收盤價"
    with pytest.raises(FinLabConfigError, match="reviewed FinLab OHLCV"):
        FinLabDatasetConfig(
            dataset_key=config.dataset_key,
            market=config.market,
            asset_class=config.asset_class,
            currency=config.currency,
            field_datasets=wrong_fields,
            symbols=config.symbols,
        )
