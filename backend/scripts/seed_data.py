"""Seed the four staging feeds and their versioned contract declarations."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.base import Base
from app.models.registry import DatasetRegistry
from app.utils import utc_now

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


SUPPORTED_DATASETS = (
    "us_equity_eod",
    "tw_equity_eod",
    "tw_equity_minute",
    "tw_etf_minute",
)


def _eod_delivery_expectation(
    *,
    mode: str,
    market: str,
    timezone: str,
    close_time: str,
    source: str,
    minimum_count: int,
    slot_id: str,
    local_time: str,
    monitor_deadline: str = "17:00:00",
    target_date_lag_days: int = 0,
) -> dict:
    return {
        "delivery_mode": mode,
        "baseline": {
            "strategy": "rolling_median",
            "scope": "dataset_source_schema",
            "window_size": 7,
            "minimum_history": 3,
        },
        "record_count": {
            "minimum_record_count": minimum_count,
            "maximum_count_drop_ratio": 0.1,
            "action": "warn",
        },
        "freshness": {
            "maximum_fetch_age_hours": 36,
            "allowed_clock_skew_minutes": 5,
            "action": "warn",
        },
        "latest_date": {
            "calendar_market": market,
            "timezone": timezone,
            "market_close_time": close_time,
            "availability_grace_minutes": 120,
            "action": "warn",
        },
        "missing_delivery": {
            "action": "warn",
            "expected_sources": [source],
            "deadline_local_time": monitor_deadline,
        },
        "schedule": {
            "enabled": True,
            "slot_id": slot_id,
            "local_time": local_time,
            "timezone": "Asia/Taipei",
            "target_date_lag_days": target_date_lag_days,
            "expected_sources": [source],
        },
    }


def _minute_delivery_expectation() -> dict:
    return {
        "delivery_mode": "sequenced_snapshot",
        "baseline": {
            "enabled": False,
            "strategy": "rolling_median",
            "scope": "dataset_source_schema",
            "window_size": 7,
            "minimum_history": 3,
        },
        "record_count": {
            "minimum_record_count": 0,
            "maximum_count_drop_ratio": 0.0,
            "action": "disabled",
        },
        "freshness": {
            "maximum_fetch_age_hours": 6,
            "allowed_clock_skew_minutes": 5,
            "action": "warn",
        },
        "latest_date": {
            "calendar_market": "TW",
            "timezone": "Asia/Taipei",
            "market_close_time": "13:30:00",
            "availability_grace_minutes": 60,
            "action": "warn",
        },
        "missing_delivery": {
            "action": "warn",
            "expected_sources": ["shioaji"],
            "deadline_local_time": "17:00:00",
        },
        "schedule": {
            "enabled": True,
            "slot_id": "taiwan_market_window",
            "local_time": "14:30:00",
            "timezone": "Asia/Taipei",
            "target_date_lag_days": 0,
            "expected_sources": ["shioaji"],
        },
    }


def _contract_config(
    *,
    schema_id: str,
    market: str,
    asset_class: str,
    currency: str,
    allowed_sources: list[str],
    delivery_expectation: dict,
) -> dict:
    """Return only contract, scope, and delivery policy keys.

    Provider-specific ``source_format``/``field_mapping``/``data_path`` keys
    are intentionally not seeded; adapters must produce the versioned body.
    """
    return {
        "schema_id": schema_id,
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": allowed_sources,
        "defaults": {
            "market": market,
            "asset_class": asset_class,
            "currency": currency,
            **(
                {"market_timezone": "Asia/Taipei", "price_adjustment": "none"}
                if schema_id == "market_minute"
                else {}
            ),
        },
        "delivery_expectation": delivery_expectation,
    }


DATASETS = [
    {
        "dataset_key": "us_equity_eod",
        "name": "美股日K",
        "description": "Twelve Data provider-neutral market_eod.v1 美國股票每日 OHLCV 資料",
        "asset_class": "equity",
        "market": "US",
        "frequency": "daily",
        "is_active": True,
        "config": _contract_config(
            schema_id="market_eod",
            market="US",
            asset_class="equity",
            currency="USD",
            allowed_sources=["twelve_data"],
            delivery_expectation=_eod_delivery_expectation(
                mode="incremental",
                market="US",
                timezone="America/New_York",
                close_time="16:00:00",
                source="twelve_data",
                minimum_count=1,
                slot_id="western_markets_window",
                local_time="09:00:00",
                monitor_deadline="10:00:00",
                target_date_lag_days=1,
            ),
        ),
    },
    {
        "dataset_key": "tw_equity_eod",
        "name": "台股日K",
        "description": "FinLab provider-neutral market_eod.v1 台灣股票每日 OHLCV 資料",
        "asset_class": "equity",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": _contract_config(
            schema_id="market_eod",
            market="TW",
            asset_class="equity",
            currency="TWD",
            allowed_sources=["finlab"],
            delivery_expectation=_eod_delivery_expectation(
                mode="full_snapshot",
                market="TW",
                timezone="Asia/Taipei",
                close_time="13:30:00",
                source="finlab",
                minimum_count=2,
                slot_id="taiwan_market_window",
                local_time="14:30:00",
                monitor_deadline="17:00:00",
                target_date_lag_days=0,
            ),
        ),
    },
    {
        "dataset_key": "tw_equity_minute",
        "name": "台股股票分鐘 K",
        "description": "Shioaji provider-neutral market_minute.v1 台灣股票一分鐘 OHLCV 資料",
        "asset_class": "equity",
        "market": "TW",
        "frequency": "minute",
        "is_active": True,
        "config": {
            **_contract_config(
                schema_id="market_minute",
                market="TW",
                asset_class="equity",
                currency="TWD",
                allowed_sources=["shioaji"],
                delivery_expectation=_minute_delivery_expectation(),
            ),
            "governance": {
                "sequence_symbol_limit": 50,
                "sequence_row_limit": 15000,
                "request_rate_limit": {"requests": 50, "window_seconds": 60},
                "request_max_attempts": 3,
                "universe_change_limit": {
                    "absolute": 20,
                    "ratio": 0.02,
                    "fail_when_either_exceeded": True,
                },
                "schedule": {
                    "timezone": "Asia/Taipei",
                    "universe_refresh": "14:00:00",
                    "acquisition_start": "14:30:00",
                    "retry_deadline": "17:00:00",
                },
            },
        },
    },
    {
        "dataset_key": "tw_etf_minute",
        "name": "台股 ETF 分鐘 K",
        "description": "Shioaji provider-neutral market_minute.v1 台灣 ETF 一分鐘 OHLCV 資料",
        "asset_class": "etf",
        "market": "TW",
        "frequency": "minute",
        "is_active": True,
        "config": {
            **_contract_config(
                schema_id="market_minute",
                market="TW",
                asset_class="etf",
                currency="TWD",
                allowed_sources=["shioaji"],
                delivery_expectation=_minute_delivery_expectation(),
            ),
            "governance": {
                "sequence_symbol_limit": 50,
                "sequence_row_limit": 15000,
                "request_rate_limit": {"requests": 50, "window_seconds": 60},
                "request_max_attempts": 3,
                "universe_change_limit": {
                    "absolute": 20,
                    "ratio": 0.02,
                    "fail_when_either_exceeded": True,
                },
                "schedule": {
                    "timezone": "Asia/Taipei",
                    "universe_refresh": "14:00:00",
                    "acquisition_start": "14:30:00",
                    "retry_deadline": "17:00:00",
                },
            },
        },
    },
]


_LEGACY_CONFIG_KEYS = {
    "source_format",
    "field_mapping",
    "data_path",
    "symbol_field",
    "name_field",
    "source_field",
    "identifier_field",
    "identifier_type",
}
_CONTRACT_CONFIG_KEYS = {
    "schema_id",
    "accepted_schema_versions",
    "current_schema_version",
    "schema_enforcement",
    "allowed_sources",
    "defaults",
}


def _merge_operator_config(
    dataset_key: str,
    seed_config: dict,
    existing_config: dict | None,
) -> dict:
    """Apply seed declarations while retaining operator-owned policy metadata.

    Contract discriminators/defaults are authoritative seed values.  Delivery
    policy and governance are recursively merged with existing values winning,
    so an operator override survives a repeatable seed.  Retired provider
    mapping keys (and the old minute universe limit key) are always removed.
    """
    merged = deepcopy(seed_config)
    if not isinstance(existing_config, dict):
        return merged

    def merge_mapping(defaults: dict, overrides: dict) -> dict:
        result = deepcopy(defaults)
        for key, value in overrides.items():
            if isinstance(result.get(key), dict) and isinstance(value, dict):
                result[key] = merge_mapping(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result

    for key, value in existing_config.items():
        if key in _LEGACY_CONFIG_KEYS or key in _CONTRACT_CONFIG_KEYS:
            continue
        if key == "governance" and isinstance(value, dict):
            governance = dict(value)
            governance.pop("universe_symbol_limit", None)
            merged[key] = merge_mapping(merged.get(key, {}), governance)
            continue
        if key == "delivery_expectation" and isinstance(value, dict):
            merged[key] = merge_mapping(merged.get(key, {}), value)
            if "missing_delivery" in value:
                historical_disabled = {
                    "action": "disabled",
                    "expected_sources": [],
                }
                if (
                    dataset_key != "tw_equity_eod"
                    or value["missing_delivery"] != historical_disabled
                ):
                    merged[key]["missing_delivery"] = deepcopy(value["missing_delivery"])
                else:
                    merged[key]["missing_delivery"] = deepcopy(seed_config[key]["missing_delivery"])
            continue
        merged[key] = deepcopy(value)

    return merged


async def seed_datasets(session: AsyncSession) -> None:
    """Upsert exactly the four supported feed declarations."""
    logger.info("Seeding supported datasets: %s", ", ".join(SUPPORTED_DATASETS))
    for dataset in DATASETS:
        existing = await session.get(DatasetRegistry, dataset["dataset_key"])
        if existing is None:
            session.add(
                DatasetRegistry(
                    dataset_key=dataset["dataset_key"],
                    name=dataset["name"],
                    description=dataset["description"],
                    asset_class=dataset["asset_class"],
                    market=dataset["market"],
                    frequency=dataset["frequency"],
                    is_active=dataset["is_active"],
                    config=deepcopy(dataset["config"]),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            continue

        existing.name = dataset["name"]
        existing.description = dataset["description"]
        existing.asset_class = dataset["asset_class"]
        existing.market = dataset["market"]
        existing.frequency = dataset["frequency"]
        existing.is_active = dataset["is_active"]
        existing.config = _merge_operator_config(
            dataset["dataset_key"],
            dataset["config"],
            existing.config,
        )
        existing.updated_at = utc_now()
    await session.commit()
    logger.info("Seeded %d datasets", len(DATASETS))


async def main() -> None:
    """Create the local schema and seed supported registry rows."""
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS raw"))
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as session:
        await seed_datasets(session)
    await engine.dispose()
    logger.info("Database seeding completed")


if __name__ == "__main__":
    asyncio.run(main())
