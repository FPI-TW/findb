"""Strict, versioned FinLab pilot universe configuration.

The production FinLab feed is intentionally much narrower than a generic
symbol-universe manifest.  This loader is the only place where the reviewed
pilot membership is declared: exactly ``2330`` and ``2317`` are accepted and
their provider-to-canonical mapping is explicit.  No SDK, network client, or
cache is involved while loading this file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabDatasetConfig,
    FinLabSymbol,
)
from findb_fetcher.universe import SymbolUniverse, UniverseLimits, UniverseSymbol

_MAX_CONFIG_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[a-z0-9_]{1,64}$")
_PILOT_SYMBOLS = frozenset({"2330", "2317"})
_ROOT_KEYS = {
    "manifest_version",
    "manifest_id",
    "provider",
    "dataset",
    "market",
    "asset_class",
    "currency",
    "field_datasets",
    "symbols",
}
_SYMBOL_KEYS = {"source_symbol", "canonical_symbol"}
_FIELD_DATASETS = {
    "open": "price:開盤價",
    "high": "price:最高價",
    "low": "price:最低價",
    "close": "price:收盤價",
    "volume": "price:成交股數",
}


class FinLabUniverseError(FinLabConfigError):
    """The reviewed FinLab pilot universe is malformed or out of scope."""


class _DuplicateJSONKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FinLabPilotUniverse:
    """Reviewed FinLab mapping plus a one-work-item SchedulerState view."""

    manifest_version: int
    manifest_id: str
    provider: str
    dataset: str
    market: str
    asset_class: str
    currency: str
    field_datasets: dict[str, str]
    symbols: tuple[FinLabSymbol, ...]

    @property
    def dataset_key(self) -> str:
        """Alias used by Fetcher contracts and SchedulerState identities."""

        return self.dataset

    @property
    def universe_version(self) -> int:
        return self.manifest_version

    @property
    def universe_id(self) -> str:
        return self.manifest_id

    @property
    def instrument_type(self) -> str:
        return "Common Stock"

    @property
    def credit_cost_per_symbol(self) -> int:
        return 5

    @property
    def limits(self) -> UniverseLimits:
        return self.as_scheduler_universe().limits

    @property
    def estimated_credits(self) -> int:
        return self.credit_cost_per_symbol

    @property
    def dataset_config(self) -> FinLabDatasetConfig:
        return FinLabDatasetConfig(
            dataset_key=self.dataset,
            market=self.market,
            asset_class=self.asset_class,
            currency=self.currency,
            field_datasets=self.field_datasets,
            symbols=self.symbols,
        )

    @property
    def expected_record_count(self) -> int:
        return len(self.symbols)

    def as_scheduler_universe(self) -> SymbolUniverse:
        """Expose exactly one durable dataset work item to generic scheduler state.

        ``SchedulerState`` is deliberately symbol-shaped for the older Twelve
        Data pilot.  A FinLab bundle is one work item, so this adapter gives it
        a single synthetic member while retaining the two reviewed symbols in
        ``dataset_config`` for acquisition and completeness checks.
        """

        state_symbol = UniverseSymbol(
            symbol=self.dataset,
            canonical_symbol=self.dataset,
            exchange=self.market,
        )
        return SymbolUniverse(
            universe_version=self.manifest_version,
            universe_id=self.manifest_id,
            provider=self.provider,
            dataset_key=self.dataset,
            market=self.market,
            asset_class=self.asset_class,
            instrument_type="Common Stock",
            # One work item consumes five FinLab SDK datasets per cycle.
            credit_cost_per_symbol=5,
            limits=UniverseLimits(
                max_symbols_per_run=1,
                max_records_per_symbol=2,
                max_total_records_per_run=2,
                max_date_span_days=1,
                max_credits_per_run=5,
            ),
            symbols=(state_symbol,),
        )


def load_finlab_universe(path: Path) -> FinLabPilotUniverse:
    """Load one exact ``finlab_tw_equity_eod`` pilot manifest.

    Unknown keys, duplicate JSON keys, missing fields, duplicate symbols, and
    any membership other than the two reviewed pilot rows fail closed.
    """

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FinLabUniverseError(f"unable to read FinLab universe config: {path}") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise FinLabUniverseError("FinLab universe config exceeds size limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError) as exc:
        raise FinLabUniverseError(
            "FinLab universe config must be UTF-8 JSON with unique keys"
        ) from exc
    if not isinstance(value, dict) or set(value) != _ROOT_KEYS:
        raise FinLabUniverseError(f"FinLab universe keys must be exactly {sorted(_ROOT_KEYS)}")

    manifest_version = _exact_int(value, "manifest_version", 1)
    manifest_id = _identifier(value, "manifest_id")
    provider = _required_string(value, "provider")
    dataset = _required_string(value, "dataset")
    market = _required_string(value, "market")
    asset_class = _required_string(value, "asset_class")
    currency = _required_string(value, "currency")
    if provider != "finlab":
        raise FinLabUniverseError("provider must be finlab")
    if dataset != "tw_equity_eod":
        raise FinLabUniverseError("dataset must be tw_equity_eod")
    if market != "TW":
        raise FinLabUniverseError("market must be TW")
    if asset_class != "equity":
        raise FinLabUniverseError("asset_class must be equity")
    if currency != "TWD":
        raise FinLabUniverseError("currency must be TWD")

    field_datasets = _field_datasets(value["field_datasets"])
    symbols = _symbols(value["symbols"])
    config = FinLabDatasetConfig(
        dataset_key=dataset,
        market=market,
        asset_class=asset_class,
        currency=currency,
        field_datasets=field_datasets,
        symbols=symbols,
    )
    # Constructing the config above applies the provider boundary a second
    # time, while this loader owns the stricter fixed pilot membership check.
    return FinLabPilotUniverse(
        manifest_version=manifest_version,
        manifest_id=manifest_id,
        provider=provider,
        dataset=dataset,
        market=market,
        asset_class=asset_class,
        currency=currency,
        field_datasets=dict(config.field_datasets),
        symbols=config.symbols,
    )


# A descriptive alias keeps call sites readable and allows tests to use the
# shorter name without introducing a second parser.
load_finlab_pilot_universe = load_finlab_universe


def _field_datasets(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(_FIELD_DATASETS):
        raise FinLabUniverseError(f"field_datasets keys must be exactly {sorted(_FIELD_DATASETS)}")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise FinLabUniverseError("field_datasets values must be strings")
    if value != _FIELD_DATASETS:
        raise FinLabUniverseError("field_datasets do not match reviewed FinLab datasets")
    return {key: value[key] for key in _FIELD_DATASETS}


def _symbols(value: Any) -> tuple[FinLabSymbol, ...]:
    if not isinstance(value, list) or len(value) != len(_PILOT_SYMBOLS):
        raise FinLabUniverseError("symbols must contain exactly the two reviewed pilot rows")
    symbols: list[FinLabSymbol] = []
    seen_source: set[str] = set()
    seen_canonical: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != _SYMBOL_KEYS:
            raise FinLabUniverseError(
                f"symbols[{index}] keys must be exactly {sorted(_SYMBOL_KEYS)}"
            )
        source = _required_symbol(item, "source_symbol", index)
        canonical = _required_symbol(item, "canonical_symbol", index)
        if source in seen_source or canonical in seen_canonical:
            raise FinLabUniverseError("reviewed FinLab symbols must be unique")
        if source not in _PILOT_SYMBOLS or canonical not in _PILOT_SYMBOLS:
            raise FinLabUniverseError("symbols must be exactly 2330 and 2317")
        if source != canonical:
            raise FinLabUniverseError("pilot canonical mapping must be explicit identity mapping")
        seen_source.add(source)
        seen_canonical.add(canonical)
        symbols.append(FinLabSymbol(source_symbol=source, canonical_symbol=canonical))
    if seen_source != _PILOT_SYMBOLS or seen_canonical != _PILOT_SYMBOLS:
        raise FinLabUniverseError("symbols must include exactly 2330 and 2317")
    return tuple(symbols)


def _required_symbol(parent: dict[str, Any], key: str, index: int) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}", value) is None:
        raise FinLabUniverseError(f"symbols[{index}].{key} is invalid")
    return value


def _required_string(parent: dict[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FinLabUniverseError(f"{key} must be a non-empty string")
    return value.strip()


def _identifier(parent: dict[str, Any], key: str) -> str:
    value = _required_string(parent, key)
    if _IDENTIFIER.fullmatch(value) is None:
        raise FinLabUniverseError(f"{key} must be a stable lowercase identifier")
    return value


def _exact_int(parent: dict[str, Any], key: str, expected: int) -> int:
    value = parent.get(key)
    if type(value) is not int or value != expected:
        raise FinLabUniverseError(f"{key} must be {expected}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError(key)
        result[key] = value
    return result
