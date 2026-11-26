"""FinLab intraday: one symbol's trading day fetched, mapped to the contract layout, delivered by reference."""

from __future__ import annotations

import hashlib
import io
import json
import textwrap
import traceback
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.full_market_universe import (
    TW_COMPANY_URLS,
    TW_ISIN_ETF_URLS,
    TW_ISIN_STOCK_URLS,
)
from findb_fetcher.provider_adapters import finlab as adapter_module
from findb_fetcher.provider_adapters.base import (
    AcquisitionContext,
    FailureKind,
    UniverseContext,
)
from findb_fetcher.provider_adapters.finlab import INTRADAY_DATASETS, FinLabAdapter
from findb_fetcher.provider_process import ProviderProcessError, supervised_call
from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabDatasetTable,
    FinLabNativeError,
    FinLabOperationalError,
    FinLabSdkError,
    FinLabSdkGateway,
)
from findb_fetcher.providers.finlab_intraday import (
    FinLabIntradayNotReadyError,
    FinLabIntradaySymbolError,
    IntradayPartition,
    fetch_partition,
    manifest_bytes,
    map_rows,
    verify_intraday_contract,
)
from findb_fetcher.raw_storage import RawObject
from isin_support import class_page
from provider_worker_support import fake_finlab, install_fake_sdk

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
LAYOUTS = json.loads((CONTRACTS / "intraday_partition" / "v1.schema.json").read_text())[
    "x-findb-row-layouts"
]
DAY = date(2026, 11, 13)
FETCHED = datetime(2026, 11, 13, 9, 30, tzinfo=timezone.utc)
TAIPEI = ZoneInfo("Asia/Taipei")
SECRET = "provider-words-that-must-not-leak"
BAR_COLUMNS = (
    "stock_id",
    "trade_date",
    "timestamp",
    "session",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "tick_count",
    "vwap",
)
TICK_COLUMNS = (
    "stock_id",
    "trade_date",
    "timestamp",
    "sequence",
    "ts",
    "close",
    "volume",
    "tick_type",
    "bid_price",
    "bid_volume",
    "ask_price",
    "ask_volume",
    "session",
)


def provider_file(kind: str, symbol: str = "2330", rows: int = 3, **columns: Any) -> bytes:
    """A partition file shaped like FinLab's: local-time instants, text dates, its own names."""
    stamps = [
        datetime(2026, 11, 13, 9, 0, tzinfo=TAIPEI) + timedelta(minutes=i) for i in range(rows)
    ]
    local = pa.timestamp("ns", tz="Asia/Taipei")
    if kind == "bar_1m":
        values: dict[str, Any] = {
            "stock_id": [symbol] * rows,
            "trade_date": [DAY.isoformat()] * rows,
            "timestamp": pa.array(stamps, type=local),
            "session": ["regular"] * rows,
            "open": [100.0 + i for i in range(rows)],
            "high": [101.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "close": [100.5 + i for i in range(rows)],
            "volume": [10 + i for i in range(rows)],
            "tick_count": [2 + i for i in range(rows)],
            "vwap": [100.2 + i for i in range(rows)],
        }
    else:
        values = {
            "stock_id": [symbol] * rows,
            "trade_date": [DAY.isoformat()] * rows,
            "timestamp": pa.array(stamps, type=local),
            "sequence": list(range(rows)),
            "ts": [1_794_560_400_000_000_000 + i for i in range(rows)],
            "close": [100.5 + i for i in range(rows)],
            "volume": [1 + i for i in range(rows)],
            "tick_type": pa.array([1] * rows, type=pa.int8()),
            "bid_price": [100.0] * rows,
            "bid_volume": [5] * rows,
            "ask_price": [101.0] * rows,
            "ask_volume": [7] * rows,
            "session": ["regular"] * rows,
        }
    values.update(columns)
    sink = io.BytesIO()
    pq.write_table(pa.table(values), sink)
    return sink.getvalue()


def _sdk(tmp_path: Path, monkeypatch, replies: dict[str, Any], **changes: str) -> None:
    """A fake SDK whose intraday API answers from ``replies`` (by request mode)."""
    script = tmp_path / "replies.json"
    script.write_text(json.dumps(replies))
    files = fake_finlab("return None")
    files["finlab/data/auth.py"] = f"""
        import json
        import finlab.exceptions


        def post_signing_request(kind, query, label):
            assert (kind, label) == ("intraday", "Intraday")
            reply = json.load(open({str(script)!r}))[query["mode"]]
            if isinstance(reply, str):
                raise getattr(finlab.exceptions, reply)({SECRET!r})
            if "error" not in reply:
                reply = {{"schema_version": 1, "dataset": query["dataset"], **reply}}
            return reply
    """
    files["finlab/data/intraday.py"] = f"""
        import finlab.exceptions

        BAR_COLUMNS = {BAR_COLUMNS!r}
        TICK_COLUMNS = {TICK_COLUMNS!r}


        def _verify_signed_partition(actual, part):
            if actual is None or any(actual.get(k) != part[k] for k in ("generation", "sha256", "size", "rows")):
                raise finlab.exceptions.DataError({SECRET!r})
            return actual


        def _download(part):
            return open(part["path"], "rb").read()
    """
    for path, source in changes.items():
        files[path] = textwrap.dedent(files[path]) + source
    install_fake_sdk(monkeypatch, tmp_path / "sdk", files)


def _published(tmp_path: Path, kind: str, raw: bytes, **part: Any) -> dict[str, Any]:
    """Manifest and download replies for one ready partition holding ``raw``."""
    path = tmp_path / f"{kind}.parquet"
    path.write_bytes(raw)
    described = {
        "date": DAY.isoformat(),
        "status": "ready",
        "key": "normalized/secret-key",
        "generation": "1794560332114812",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "rows": pq.read_metadata(io.BytesIO(raw)).num_rows,
        **part,
    }
    return {
        "manifest": {
            "partitions": [described],
            "closed_dates": [],
            "volume_unit": "provider_native",
        },
        "download": {
            "partitions": [{**described, "path": str(path), "url": "https://signed/" + SECRET}]
        },
    }


def _payload(kind: str = "bar_1m", **changes: Any) -> dict[str, Any]:
    return {
        "token": "offline",
        "record_kind": kind,
        "provider_symbol": "2330",
        "symbol": "2330",
        "trade_date": DAY.isoformat(),
        "layout": LAYOUTS[kind]["columns"],
        **changes,
    }


@pytest.mark.parametrize("kind", ["bar_1m", "tick"])
def test_a_published_day_returns_the_provider_file_and_rows_in_the_contract_layout(
    tmp_path, monkeypatch, kind
) -> None:
    raw = provider_file(kind)
    _sdk(tmp_path, monkeypatch, _published(tmp_path, kind, raw))

    status, manifest, row_count, original, rows = supervised_call(
        "finlab.intraday", _payload(kind), timeout=30
    )

    assert (status, row_count, original) == ("ready", 3, raw)
    # Evidence names the published partition; nothing signed travels with it.
    assert manifest == {
        "date": "2026-11-13",
        "status": "ready",
        "generation": "1794560332114812",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
        "rows": 3,
        "volume_unit": "provider_native",
    }
    table = pq.read_table(io.BytesIO(rows))
    assert [(field.name, str(field.type), field.nullable) for field in table.schema] == [
        (column["name"], column["type"], column["nullable"]) for column in LAYOUTS[kind]["columns"]
    ]
    first = table.slice(0, 1).to_pylist()[0]
    assert (first["symbol"], first["trade_date"]) == ("2330", DAY)
    instant = first["bar_start_time" if kind == "bar_1m" else "trade_time"]
    # 09:00 Taipei is 01:00 UTC: the instant is kept, the zone is not.
    assert instant == datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)
    if kind == "bar_1m":
        assert (first["trade_count"], first["volume"], first["close"]) == (2, 10, 100.5)
    else:
        assert (first["price"], first["sequence"], first["tick_type"]) == (100.5, 0, 1)
        assert "ts" not in table.column_names


def test_the_canonical_symbol_replaces_the_provider_one_in_the_rows(tmp_path, monkeypatch) -> None:
    raw = provider_file("bar_1m", symbol="1101B")
    _sdk(tmp_path, monkeypatch, _published(tmp_path, "bar_1m", raw))

    *_, rows = supervised_call(
        "finlab.intraday", _payload(provider_symbol="1101B", symbol="1101-B"), timeout=30
    )

    assert set(pq.read_table(io.BytesIO(rows))["symbol"].to_pylist()) == {"1101-B"}


def test_a_day_without_trades_is_an_answer_with_its_manifest_entry(tmp_path, monkeypatch) -> None:
    entry = {"date": DAY.isoformat(), "status": "no_trades", "key": "k", "generation": "7"}
    _sdk(tmp_path, monkeypatch, {"manifest": {"partitions": [entry], "closed_dates": []}})

    result = supervised_call("finlab.intraday", _payload(), timeout=30)

    assert result == (
        "no_trades",
        {"date": "2026-11-13", "status": "no_trades", "generation": "7"},
        0,
        b"",
        b"",
    )


def _refusals(tmp_path: Path) -> dict[str, tuple[dict[str, Any], str]]:
    raw = provider_file("bar_1m")
    ready = _published(tmp_path, "bar_1m", raw)
    part = ready["manifest"]["partitions"][0]
    not_ready = {"code": "not_ready", "error": SECRET, "partitions": []}
    return {
        # FinLab has not verified the trade date: nobody's partition exists.
        "calendar": (
            {"manifest": {**not_ready, "available_through": "2026-11-12"}},
            "not_ready_calendar",
        ),
        "closed day": (
            {"manifest": {"partitions": [], "closed_dates": ["2026-11-13"]}},
            "not_ready_partition",
        ),
        "still building": (
            {"manifest": {"partitions": [{**part, "status": "building"}]}},
            "not_ready_partition",
        ),
        "quota code": ({"manifest": {"code": "quota_exceeded", "error": SECRET}}, "quota"),
        "quota error": ({"manifest": "QuotaExceededError"}, "quota"),
        "auth error": ({"manifest": "AuthError"}, "auth"),
        "unknown refusal": ({"manifest": {"code": "internal", "error": SECRET}}, "bad_response"),
        "other dataset": (
            {"manifest": {"dataset": "tw_minute:0050", "partitions": [part]}},
            "bad_response",
        ),
        "republished meanwhile": (
            {**ready, "download": {"partitions": [{**part, "generation": "2"}]}},
            "bad_response",
        ),
        "not the described bytes": (
            {
                **ready,
                "manifest": {"partitions": [{**part, "sha256": "0" * 64}]},
                "download": {
                    "partitions": [{**ready["download"]["partitions"][0], "sha256": "0" * 64}]
                },
            },
            "bad_response",
        ),
    }


@pytest.mark.parametrize(
    "manifest",
    [
        {"code": "not_ready", "error": SECRET, "partitions": [], "missing_dates": ["2026-11-13"]},
        {"partitions": [], "closed_dates": []},
    ],
    ids=["refused as missing", "not listed"],
)
def test_a_verified_day_without_a_partition_is_an_absent_answer(
    tmp_path, monkeypatch, manifest
) -> None:
    # The date is verified but this symbol has none: no board-lot trade, still being
    # built, or unknown. Only Source can tell, so the worker answers instead of failing.
    _sdk(tmp_path, monkeypatch, {"manifest": manifest})

    result = supervised_call("finlab.intraday", _payload(), timeout=30)

    assert result == ("absent", {"date": "2026-11-13", "status": "absent"}, 0, b"", b"")
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "case",
    [
        "calendar",
        "closed day",
        "still building",
        "quota code",
        "quota error",
        "auth error",
        "unknown refusal",
        "other dataset",
        "republished meanwhile",
        "not the described bytes",
    ],
)
def test_a_refusal_keeps_a_bounded_class_and_none_of_the_provider_words(
    tmp_path, monkeypatch, case
) -> None:
    replies, code = _refusals(tmp_path)[case]
    _sdk(tmp_path, monkeypatch, replies)

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.intraday", _payload(), timeout=30)

    assert raised.value.code == code
    assert SECRET not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize(
    "raw",
    [
        lambda: provider_file("bar_1m", stock_id=["2330", "2330", "0050"]),
        lambda: provider_file("bar_1m", trade_date=["2026-11-13", "2026-11-13", "2026-11-12"]),
        lambda: provider_file("bar_1m", close=[100.5, None, 102.5]),
        lambda: provider_file("tick"),
        lambda: b"not a parquet file",
    ],
    ids=["another symbol", "another date", "empty required value", "another layout", "not parquet"],
)
def test_a_file_that_is_not_this_symbols_day_is_never_mapped(tmp_path, monkeypatch, raw) -> None:
    body = raw()
    rows = 3 if body.startswith(b"PAR1") else 1
    replies = _published(tmp_path, "bar_1m", provider_file("bar_1m"))
    part = {
        **replies["manifest"]["partitions"][0],
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "rows": rows,
    }
    path = tmp_path / "other.parquet"
    path.write_bytes(body)
    _sdk(
        tmp_path,
        monkeypatch,
        {
            "manifest": {"partitions": [part]},
            "download": {"partitions": [{**part, "path": str(path)}]},
        },
    )

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.intraday", _payload(), timeout=30)

    assert raised.value.code == "bad_response"


@pytest.mark.parametrize(
    "missing",
    [
        "finlab/data/auth.py:post_signing_request",
        "finlab/data/intraday.py:_download",
        "finlab/data/intraday.py:_verify_signed_partition",
        "finlab/data/intraday.py:BAR_COLUMNS",
        "finlab/exceptions.py:DataError",
    ],
)
def test_an_sdk_without_the_reviewed_intraday_names_fails_closed(
    tmp_path, monkeypatch, missing
) -> None:
    path, _, name = missing.partition(":")
    removal = f"\n\ndel {name}\n" if name != "DataError" else "\n\nDataError = None\n"
    _sdk(tmp_path, monkeypatch, {"manifest": "AuthError"}, **{path: removal})

    with pytest.raises(ProviderProcessError) as raised:
        supervised_call("finlab.intraday", _payload(), timeout=30)

    # Refused before any request: the fake would have answered with an auth error.
    assert raised.value.code == "sdk_contract"


def test_instants_without_a_time_zone_are_refused_rather_than_read_as_utc() -> None:
    naive = [datetime(2026, 11, 13, 9, 0) + timedelta(minutes=i) for i in range(3)]
    raw = provider_file("bar_1m", timestamp=pa.array(naive, type=pa.timestamp("ns")))

    with pytest.raises(FinLabNativeError) as raised:
        map_rows(
            raw,
            record_kind="bar_1m",
            provider_symbol="2330",
            symbol="2330",
            trade_date=DAY.isoformat(),
            row_count=3,
            layout=LAYOUTS["bar_1m"]["columns"],
            source_columns=BAR_COLUMNS,
        )

    assert raised.value.native_failure_code == "bad_response"


def test_the_offline_check_maps_a_sample_to_the_installed_contract(tmp_path, monkeypatch) -> None:
    """Run in the provider image by CI: the installed Arrow must print the contract's types."""
    import sys
    import types

    intraday = types.SimpleNamespace(
        _download=lambda part: b"",
        _verify_signed_partition=lambda actual, part: actual,
        BAR_COLUMNS=BAR_COLUMNS,
        TICK_COLUMNS=TICK_COLUMNS,
    )
    fake = {
        "finlab": types.SimpleNamespace(
            login=lambda token: None, get_token=lambda: ("t", "api_token")
        ),
        "finlab.data.auth": types.SimpleNamespace(post_signing_request=lambda *a: {}),
        "finlab.data.intraday": intraday,
        "finlab.exceptions": types.SimpleNamespace(
            QuotaExceededError=RuntimeError, AuthError=PermissionError, DataError=ValueError
        ),
    }
    for name, module in fake.items():
        monkeypatch.setitem(sys.modules, name, module)

    verify_intraday_contract(CONTRACTS)

    # A contract whose layout the mapping cannot produce fails the check.
    changed = json.loads((CONTRACTS / "intraday_partition" / "v1.schema.json").read_text())
    changed["x-findb-row-layouts"]["tick"]["columns"][2]["type"] = "timestamp[ns, tz=UTC]"
    (tmp_path / "intraday_partition").mkdir()
    (tmp_path / "intraday_partition" / "v1.schema.json").write_text(json.dumps(changed))
    with pytest.raises(FinLabSdkError):
        verify_intraday_contract(tmp_path)
    intraday.BAR_COLUMNS = None
    with pytest.raises(FinLabSdkError):
        verify_intraday_contract(CONTRACTS)


@pytest.fixture
def ungoverned(monkeypatch):
    """The wrapper's own behaviour; the account governor has its own tests."""
    from contextlib import contextmanager, nullcontext

    from findb_fetcher import acquisition, provider_process
    from findb_fetcher.providers import finlab_intraday

    calls: list[tuple[str, dict[str, Any]]] = []
    answer: dict[str, Any] = {}

    @contextmanager
    def acquiring(provider, credential=None):
        yield types_namespace(dispatch_checkpoint=lambda: 0.0)

    def call(operation, payload, **kwargs):
        calls.append((operation, payload))
        if isinstance(answer["value"], Exception):
            raise answer["value"]
        return answer["value"]

    monkeypatch.setattr(acquisition, "Acquisition", acquiring)
    monkeypatch.setattr(finlab_intraday, "sdk_credential_scope", nullcontext)
    monkeypatch.setattr(provider_process, "supervised_call", call)
    return calls, answer


def types_namespace(**values: Any) -> Any:
    import types

    return types.SimpleNamespace(**values)


def test_the_gateway_call_returns_the_partition_and_accounts_the_provider_bytes(
    ungoverned,
) -> None:
    calls, answer = ungoverned
    answer["value"] = ("ready", {"generation": "7"}, 3, b"provider file", b"rows")
    observed: list[int] = []
    gateway = FinLabSdkGateway("offline", on_response_bytes=observed.append)

    partition = fetch_partition(
        gateway,
        record_kind="tick",
        provider_symbol="2330",
        symbol="2330",
        trade_date=DAY,
        layout=LAYOUTS["tick"]["columns"],
    )

    assert partition == IntradayPartition(
        "ready", {"generation": "7"}, 3, b"provider file", b"rows"
    )
    assert observed == [len(b"provider file")]
    ((operation, payload),) = calls
    assert operation == "finlab.intraday"
    assert {k: v for k, v in payload.items() if k not in {"token", "layout"}} == {
        "record_kind": "tick",
        "provider_symbol": "2330",
        "symbol": "2330",
        "trade_date": "2026-11-13",
        # A delivery downloads the partition; only a recheck asks for the manifest alone.
        "manifest_only": False,
    }
    assert b"provider file" not in repr(partition).encode()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("not_ready_calendar", "calendar"),
        ("not_ready_partition", "partition"),
        ("network", "network"),
        ("quota", "quota"),
        ("auth", "auth"),
        ("process_timeout", "process_timeout"),
        ("sdk_error", "sdk_error"),
    ],
)
def test_a_worker_failure_becomes_a_not_ready_scope_or_a_bounded_operational_class(
    ungoverned, capsys, code, expected
) -> None:
    calls, answer = ungoverned
    answer["value"] = ProviderProcessError("worker failed", code)
    gateway = FinLabSdkGateway("offline")
    arguments = dict(
        record_kind="bar_1m",
        provider_symbol="2330",
        symbol="2330",
        trade_date=DAY,
        layout=LAYOUTS["bar_1m"]["columns"],
    )

    if code.startswith("not_ready"):
        with pytest.raises(FinLabIntradayNotReadyError) as not_ready:
            fetch_partition(gateway, **arguments)
        assert not_ready.value.scope == expected
        # Waiting for publication is not a failure worth a log line.
        assert capsys.readouterr().out == ""
    else:
        with pytest.raises(FinLabIntradaySymbolError) as failed:
            fetch_partition(gateway, **arguments)
        assert failed.value.code == expected
        # Still an operational failure for every caller that does not tell them apart.
        assert isinstance(failed.value, FinLabOperationalError)
        assert json.loads(capsys.readouterr().out) == {
            "finlab_sdk_failure": {"dataset": "tw_minute:2330", "code": expected}
        }


@pytest.mark.parametrize(
    "change",
    [
        {"record_kind": "bar_60m"},
        {"provider_symbol": "2330 "},
        {"provider_symbol": "../x"},
        {"symbol": ""},
    ],
)
def test_an_invalid_intraday_request_never_reaches_the_worker(ungoverned, change) -> None:
    calls, _ = ungoverned
    arguments = {
        "record_kind": "bar_1m",
        "provider_symbol": "2330",
        "symbol": "2330",
        "trade_date": DAY,
        "layout": LAYOUTS["bar_1m"]["columns"],
        **change,
    }

    with pytest.raises(FinLabConfigError):
        fetch_partition(FinLabSdkGateway("offline"), **arguments)

    assert calls == []


# --- adapter ---------------------------------------------------------------------------


class _Raw:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    def __call__(self, raw: bytes, symbol: str, *, payload_format: str = "json") -> RawObject:
        self.calls.append((raw, symbol, payload_format))
        digest = hashlib.sha256(raw).hexdigest()
        extension = "parquet" if payload_format == "parquet" else "json"
        return RawObject(
            f"r2://{'a' * 32}/findb-test-raw/finlab/tw_equity_bar_1m/0123456789abcdef/{digest}.{extension}",
            digest,
            len(raw),
        )


def _context(dataset: str = "tw_equity_bar_1m", **changes: Any) -> tuple[AcquisitionContext, _Raw]:
    raw = _Raw()
    values: dict[str, Any] = {
        "dataset_key": dataset,
        "trade_date": DAY,
        "fetched_at": FETCHED,
        "feed": {"provider": "finlab", "dataset_key": dataset, "asset_class": "equity"},
        "delivery": {
            "slot_id": "taiwan_market_window",
            "scheduled_for": FETCHED.isoformat(),
            "target_data_date": DAY.isoformat(),
            "work_item_id": "fp1:" + "0" * 32,
        },
        "contract": {
            "schema_id": "intraday_partition",
            "current_version": 1,
            "accepted_versions": [1],
        },
        "limits": {"max_response_bytes": 16 * 1024 * 1024},
        "http": httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        "persist": raw,
        "observe_response": lambda size, declared=0: None,
        "rate_limited": lambda: None,
        "snapshot": lambda url, provider_response=False: pytest.fail("no snapshot expected"),
    }
    values.update(changes)
    return AcquisitionContext(**values), raw


def _adapter(monkeypatch, partition: IntradayPartition | Exception) -> tuple[FinLabAdapter, list]:
    calls: list[dict[str, Any]] = []

    def fetch(gateway, **arguments):
        calls.append(arguments)
        if isinstance(partition, Exception):
            raise partition
        return partition

    monkeypatch.setenv("FETCHER_CONTRACTS_DIR", str(CONTRACTS))
    monkeypatch.setattr(adapter_module, "fetch_partition", fetch)
    adapter = FinLabAdapter()
    adapter.gateway = lambda observe: object()  # type: ignore[method-assign]
    return adapter, calls


MEMBER = {
    "member_key": "2330",
    "symbol": "2330",
    "provider_symbol": "2330",
    "mapping_status": "mapped",
}


@pytest.mark.parametrize(
    ("dataset", "kind"),
    [(dataset, kind) for dataset, (kind, _) in sorted(INTRADAY_DATASETS.items())],
)
def test_a_member_is_delivered_as_one_partition_that_references_both_stored_files(
    monkeypatch, dataset, kind
) -> None:
    partition = IntradayPartition("ready", {"generation": "7"}, 3, b"provider file", b"mapped rows")
    adapter, calls = _adapter(monkeypatch, partition)
    context, raw = _context(dataset)

    result = adapter.acquire(MEMBER, context)

    assert calls == [
        {
            "record_kind": kind,
            "provider_symbol": "2330",
            "symbol": "2330",
            "trade_date": DAY,
            "layout": LAYOUTS[kind]["columns"],
        }
    ]
    # Both files go straight to raw storage as Parquet, under the member's symbol.
    assert raw.calls == [
        (b"provider file", "2330", "parquet"),
        (b"mapped rows", "2330", "parquet"),
    ]
    assert result.no_trade is None
    assert result.raw_object.sha256 == hashlib.sha256(b"provider file").hexdigest()
    request = result.request
    (row,) = request["payload"]["data"]
    assert row == {
        "symbol": "2330",
        "source_symbol": "2330",
        "trade_date": "2026-11-13",
        "record_kind": kind,
        "row_count": 3,
        "rows_ref": raw(b"mapped rows", "2330", payload_format="parquet").ref,
        "rows_sha256": hashlib.sha256(b"mapped rows").hexdigest(),
        "rows_bytes": len(b"mapped rows"),
        "rows_format": "parquet",
        "price_adjustment": "none",
        "volume_unit": "lot",
    }
    assert request["payload"]["batch"] == {
        "data_date": "2026-11-13",
        "delivery_mode": "incremental",
        "declared_record_count": 1,
    }
    # With the provider file attached as the runtime does, the published contract accepts it.
    request["payload"]["batch"].update(
        source_raw_ref=result.raw_object.ref, source_raw_sha256=result.raw_object.sha256
    )
    ContractRegistry(CONTRACTS).validate("intraday_partition", 1, request)


def test_a_day_without_trades_is_reported_with_the_stored_manifest_entry(monkeypatch) -> None:
    partition = IntradayPartition("no_trades", {"date": "2026-11-13", "status": "no_trades"})
    adapter, _ = _adapter(monkeypatch, partition)
    context, raw = _context()

    result = adapter.acquire(MEMBER, context)

    evidence = manifest_bytes(partition, dataset="tw_equity_bar_1m")
    assert result.request is None
    assert raw.calls == [(evidence, "2330", "json")]
    assert result.no_trade == {
        "url": "https://www.finlab.tw/data/intraday?dataset=tw_equity_bar_1m",
        "sha256": hashlib.sha256(evidence).hexdigest(),
        "observed_at": FETCHED.isoformat(),
        "source_symbol": "2330",
        "trade_date": "2026-11-13",
        "session": None,
        "source_status": "no_trade",
        "source_excerpt": evidence.decode(),
    }
    assert result.raw_object.sha256 == hashlib.sha256(evidence).hexdigest()


def test_a_day_without_a_partition_is_a_claim_only_source_can_accept(monkeypatch) -> None:
    partition = IntradayPartition("absent", {"date": "2026-11-13", "status": "absent"})
    adapter, _ = _adapter(monkeypatch, partition)
    context, _ = _context()

    result = adapter.acquire(MEMBER, context)

    evidence = manifest_bytes(partition, dataset="tw_equity_bar_1m")
    assert result.request is None
    assert result.no_trade["source_status"] == "no_record"
    assert result.no_trade["source_excerpt"] == evidence.decode()
    assert result.raw_object.sha256 == hashlib.sha256(evidence).hexdigest()
    # Refused, it is the member failure FinLab's missing partition always was.
    assert isinstance(result.unconfirmed, FinLabIntradayNotReadyError)
    assert result.unconfirmed.scope == "partition"
    classified = adapter.classify(result.unconfirmed, "member")
    assert (classified.kind, classified.no_data) == (FailureKind.MEMBER, True)


@pytest.mark.parametrize(
    ("failure", "kind", "wait", "no_data"),
    [
        # No symbol can be served yet: the plan waits for publication.
        (FinLabIntradayNotReadyError("calendar"), FailureKind.ROUND_RETRY, "publication", False),
        # Only this symbol: it keeps its retries, the other members go on.
        (FinLabIntradayNotReadyError("partition"), FailureKind.MEMBER, None, True),
        (FinLabOperationalError("x", "network"), FailureKind.ROUND_RETRY, "outage", False),
        # One symbol's request failed in a way that says nothing about the provider:
        # that member is charged and the round goes on, until a run of three (#747).
        (FinLabIntradaySymbolError("x", "sdk_error"), FailureKind.ROUND_RETRY, None, False),
        (FinLabIntradaySymbolError("x", "bad_response"), FailureKind.ROUND_RETRY, None, False),
        (FinLabIntradaySymbolError("x", "os_error"), FailureKind.ROUND_RETRY, None, False),
        # The provider or the account failed: every member would, so the plan waits.
        (FinLabIntradaySymbolError("x", "network"), FailureKind.ROUND_RETRY, "outage", False),
        (FinLabIntradaySymbolError("x", "timeout"), FailureKind.ROUND_RETRY, "outage", False),
        (FinLabIntradaySymbolError("x", "quota"), FailureKind.ROUND_RETRY, "outage", False),
        (FinLabIntradaySymbolError("x", "killed"), FailureKind.ROUND_RETRY, "outage", False),
        (FinLabIntradaySymbolError("x", "auth"), FailureKind.WORK_FAILED, None, False),
        (FinLabOperationalError("x", "quota"), FailureKind.ROUND_RETRY, "outage", False),
        (FinLabOperationalError("x", "auth"), FailureKind.WORK_FAILED, None, False),
        (FinLabOperationalError("x", "sdk_contract"), FailureKind.WORK_FAILED, None, False),
        (FinLabConfigError("x"), FailureKind.WORK_FAILED, None, False),
    ],
)
def test_an_intraday_failure_is_scoped_to_the_member_the_plan_or_the_work(
    monkeypatch, failure, kind, wait, no_data
) -> None:
    adapter, _ = _adapter(monkeypatch, failure)
    context, raw = _context()

    with pytest.raises(type(failure)) as raised:
        adapter.acquire(MEMBER, context)

    classified = adapter.classify(raised.value, "member")
    assert (classified.kind, classified.wait, classified.no_data) == (kind, wait, no_data)
    assert raw.calls == []


def test_intraday_members_share_no_table_and_keep_the_daily_path_untouched(monkeypatch) -> None:
    adapter, _ = _adapter(monkeypatch, IntradayPartition("ready", {}, 1, b"a", b"b"))

    assert adapter.pending_shared_fetch("tw_equity_tick", DAY) is False
    assert adapter.pending_shared_fetch("tw_equity_eod", DAY) is True
    adapter.acquire(MEMBER, _context()[0])
    assert adapter.bundles == {}


def test_a_dataset_without_the_partition_contract_is_a_configuration_error(monkeypatch) -> None:
    adapter, calls = _adapter(monkeypatch, IntradayPartition("ready", {}, 1, b"a", b"b"))
    context, raw = _context(
        contract={"schema_id": "market_eod", "current_version": 1, "accepted_versions": [1]}
    )

    with pytest.raises(FinLabConfigError):
        adapter.acquire(MEMBER, context)

    assert calls == [] and raw.calls == []


@pytest.mark.parametrize(
    ("dataset", "expected"),
    [
        # 2317 has no company record: its classification is a gap and it is not attempted.
        ("tw_equity_tick", {"2330": "mapped", "1101": "mapped", "2317": "gap", "6488": "mapped"}),
        ("tw_etf_bar_1m", {"0050": "mapped", "00632R": "mapped"}),
    ],
)
def test_an_intraday_dataset_uses_the_daily_official_list_without_a_catalogue_request(
    monkeypatch, dataset, expected
) -> None:
    official = {
        TW_COMPANY_URLS[0]: '[{"公司代號":"2330"},{"公司代號":"1101"}]'.encode(),
        TW_COMPANY_URLS[1]: b'[{"SecuritiesCompanyCode":"6488"}]',
        TW_ISIN_STOCK_URLS[0]: class_page(
            [
                ("2330", "ESVUFR", "1994/09/05"),
                ("1101", "ESVUFR", "1962/02/09"),
                ("2317", "ESVUFR", "1991/06/18"),
            ]
        ),
        TW_ISIN_STOCK_URLS[1]: class_page([("6488", "ESVUFR", "2014/03/26")], otc=True),
        TW_ISIN_ETF_URLS[0]: class_page([("0050", "CEOGEU", "2003/06/30")], security_type="ETF"),
        TW_ISIN_ETF_URLS[1]: class_page(
            [("00632R", "CEOGEU", "2014/10/31")], otc=True, security_type="ETF"
        ),
    }
    requested: list[str] = []

    def snapshot(url, provider_response=False):
        requested.append(url)
        return official[url]

    adapter = FinLabAdapter()
    adapter.gateway = lambda observe: pytest.fail("no SDK request expected")  # type: ignore[method-assign]
    context = UniverseContext(
        dataset_keys=[dataset],
        observed_at=FETCHED,
        zones={dataset: TAIPEI},
        snapshot=snapshot,
        catalogue=lambda **kwargs: pytest.fail("no catalogue expected"),
        catalogue_evidence=lambda dataset: {},
        persist=lambda *args, **kwargs: pytest.fail("no persist expected"),
        observe_response=lambda *args, **kwargs: None,
        snapshot_objects={},
        refresh_limits=lambda: None,
        pace=lambda: None,
    )

    groups = adapter.build_universe([{"dataset_key": dataset}], context)

    assert set(groups) == {dataset}
    snapshot_result = groups[dataset]
    assert {m["symbol"]: m["mapping_status"] for m in snapshot_result.members} == expected
    # Only this dataset's own lists were downloaded, and they are its evidence.
    assert set(snapshot_result.evidence) == set(requested)
    assert all(member["provider_symbol"] == member["symbol"] for member in snapshot_result.members)


def test_a_refresh_of_the_whole_finlab_scope_builds_daily_and_intraday_universes() -> None:
    official = {
        TW_COMPANY_URLS[0]: '[{"公司代號":"2330"},{"公司代號":"7848"}]'.encode(),
        TW_COMPANY_URLS[1]: b'[{"SecuritiesCompanyCode":"6488"}]',
        TW_ISIN_STOCK_URLS[0]: class_page(
            [("2330", "ESVUFR", "1994/09/05"), ("7848", "ESVUFR", "2026/11/10")]
        ),
        TW_ISIN_STOCK_URLS[1]: class_page([("6488", "ESVUFR", "2014/03/26")], otc=True),
    }
    catalogue: list[tuple[str, ...]] = []

    class Gateway:
        def fetch_catalogue(self, *, symbols):
            catalogue.append(tuple(symbols))
            # The daily price table does not carry the new listing yet.
            return FinLabDatasetTable(dates=(), symbols=("2330", "6488"), values=())

    adapter = FinLabAdapter()
    adapter.gateway = lambda observe: Gateway()  # type: ignore[method-assign]
    datasets = ["tw_equity_eod", "tw_equity_bar_1m", "tw_equity_tick"]
    context = UniverseContext(
        dataset_keys=datasets,
        observed_at=FETCHED,
        zones={dataset: TAIPEI for dataset in datasets},
        snapshot=lambda url, provider_response=False: official[url],
        catalogue=lambda **kwargs: pytest.fail("no catalogue expected"),
        catalogue_evidence=lambda dataset: {},
        persist=lambda raw, dataset, kind: SimpleNamespace(ref=f"{kind}/{dataset}"),
        observe_response=lambda *args, **kwargs: None,
        snapshot_objects={},
        refresh_limits=lambda: None,
        pace=lambda: None,
    )

    groups = adapter.build_universe([{"dataset_key": dataset} for dataset in datasets], context)

    assert set(groups) == set(datasets)
    statuses = {
        dataset: {m["symbol"]: m["mapping_status"] for m in groups[dataset].members}
        for dataset in datasets
    }
    # The daily dataset follows FinLab's price table; intraday attempts every classified member.
    assert statuses["tw_equity_eod"] == {"2330": "mapped", "7848": "gap", "6488": "mapped"}
    assert (
        statuses["tw_equity_bar_1m"]
        == statuses["tw_equity_tick"]
        == {
            "2330": "mapped",
            "7848": "mapped",
            "6488": "mapped",
        }
    )
    assert catalogue == [("2330", "7848", "6488")]


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (409, {"detail": {"code": "no_data_not_corroborated"}}, "no_data_not_corroborated"),
        (409, {"detail": "No-data evidence does not match provider symbol/date/session"}, None),
        (422, {"detail": {"code": "no_data_not_corroborated"}}, None),
    ],
)
def test_only_an_exact_refusal_leaves_a_no_record_claim_retryable(status, body, code) -> None:
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient

    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    ) as http:
        source = FullMarketSourceClient(
            FetcherConfig("https://source.example", "offline-key", max_attempts=1), client=http
        )
        with pytest.raises(FullMarketProtocolError) as refused:
            source.outcome(
                "plan", work_item_id="fp1:x", member_key="2330", reason="no_trade", evidence={}
            )

    assert refused.value.code == code


def test_a_manifest_only_question_never_downloads(tmp_path, monkeypatch) -> None:
    raw = provider_file("bar_1m")
    replies = _published(tmp_path, "bar_1m", raw)
    # No download reply at all: asking for one would fail the call.
    _sdk(tmp_path, monkeypatch, {"manifest": replies["manifest"]})

    status, manifest, rows, original, mapped = supervised_call(
        "finlab.intraday", _payload(manifest_only=True), timeout=30
    )

    assert (status, rows, original, mapped) == ("ready", 3, b"", b"")
    assert manifest["sha256"] == hashlib.sha256(raw).hexdigest()


EARLIER = date(2026, 11, 12)
COHORT = [
    {"member_key": symbol, "symbol": symbol, "provider_symbol": symbol, "mapping_status": "mapped"}
    for symbol in ("2330", "1538", "1589", "6488")
]
# The previous trade date: 2330 delivered, the other three settled as no trade.
DELIVERED = {"2330": {"close": "1000"}, "1538": None, "1589": None, "6488": None}


def _recheck(
    monkeypatch, answers, *, dataset="tw_equity_bar_1m", delivered=DELIVERED, earlier=EARLIER
):
    calls: list[dict[str, Any]] = []

    def fetch(gateway, **arguments):
        calls.append(arguments)
        answer = answers[arguments["provider_symbol"]]
        if isinstance(answer, BaseException):
            raise answer
        return IntradayPartition(answer, {"date": EARLIER.isoformat(), "status": answer})

    monkeypatch.setattr(adapter_module, "fetch_partition", fetch)
    adapter = FinLabAdapter()
    adapter.gateway = lambda observe: object()  # type: ignore[method-assign]
    context, _ = _context(dataset)
    context = replace(
        context,
        cohort=COHORT,
        delivered=lambda day: delivered if day == earlier else None,
        earlier_plan_date=lambda: earlier,
    )
    return adapter, context, calls


def _lines(capsys) -> dict[str, dict]:
    return {
        key: value
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
        for key, value in json.loads(line).items()
    }


def test_a_previous_no_trade_member_with_a_partition_now_raises_an_alert(
    monkeypatch, capsys
) -> None:
    adapter, context, calls = _recheck(
        monkeypatch, {"1538": "ready", "1589": "absent", "6488": "no_trades"}
    )

    adapter.round_started(context)

    # Only the no-trade members of the previous date, by manifest only.
    assert [(c["provider_symbol"], c["trade_date"], c["manifest_only"]) for c in calls] == [
        ("1538", EARLIER, True),
        ("1589", EARLIER, True),
        ("6488", EARLIER, True),
    ]
    lines = _lines(capsys)
    expected = {
        "dataset": "tw_equity_bar_1m",
        "trade_date": "2026-11-12",
        "no_trade_members": 3,
        "checked": 3,
        "unanswered": 0,
        "now_published": 1,
        "symbols": ["1538"],
    }
    assert lines == {
        "finlab_no_trade_recheck": expected,
        "finlab_no_trade_partition_found": expected,
    }


def test_no_partition_since_then_is_only_reported(monkeypatch, capsys) -> None:
    adapter, context, _ = _recheck(
        monkeypatch, {"1538": "absent", "1589": "absent", "6488": "no_trades"}
    )

    adapter.round_started(context)

    lines = _lines(capsys)
    assert lines["finlab_no_trade_recheck"]["now_published"] == 0
    assert "finlab_no_trade_partition_found" not in lines


def test_a_feed_day_is_rechecked_once_and_only_for_intraday_feeds(monkeypatch, capsys) -> None:
    adapter, context, calls = _recheck(
        monkeypatch, dict.fromkeys(("1538", "1589", "6488"), "absent")
    )

    adapter.round_started(context)
    adapter.round_started(context)  # A no-data retry round of the same day.
    assert len(calls) == 3

    daily, eod_context, eod_calls = _recheck(monkeypatch, {}, dataset="tw_equity_eod")
    daily.round_started(eod_context)
    assert eod_calls == []


@pytest.mark.parametrize(
    ("earlier", "delivered"),
    [(None, DELIVERED), (EARLIER, None)],
    ids=["no earlier plan", "earlier plan not held"],
)
def test_nothing_to_recheck_asks_nothing(monkeypatch, capsys, earlier, delivered) -> None:
    adapter, context, calls = _recheck(monkeypatch, {}, delivered=delivered, earlier=earlier)
    if earlier is None:
        context = replace(context, earlier_plan_date=lambda: None)

    adapter.round_started(context)

    assert calls == [] and "finlab_no_trade_recheck" not in _lines(capsys)


def test_a_failing_provider_stops_the_recheck_without_failing_the_round(
    monkeypatch, capsys
) -> None:
    adapter, context, calls = _recheck(
        monkeypatch,
        {
            "1538": FinLabIntradayNotReadyError("partition"),
            "1589": FinLabOperationalError("x", "network"),
            "6488": "ready",
        },
    )

    adapter.round_started(context)

    # Still being built counts as unanswered; a failing provider ends the questions.
    assert [c["provider_symbol"] for c in calls] == ["1538", "1589"]
    line = _lines(capsys)["finlab_no_trade_recheck"]
    assert (line["unanswered"], line["now_published"]) == (3, 0)


def test_a_stopped_control_ends_the_recheck_silently(monkeypatch, capsys) -> None:
    from findb_fetcher.provider_work import ProviderWorkAcquisitionStopped

    adapter, context, _ = _recheck(
        monkeypatch, dict.fromkeys(("1538", "1589", "6488"), ProviderWorkAcquisitionStopped("x"))
    )

    adapter.round_started(context)  # Must not raise: the stop is a BaseException.

    assert _lines(capsys) == {}
