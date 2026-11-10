"""Seed bounded and staged full-market feed contract declarations."""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.execution_profiles import scheduler_key
from app.models.base import Base
from app.models.registry import DatasetRegistry, SchedulerControl, SchedulerDataset
from app.provider_catalog import FeedDefinition
from app.services.effective_calendar import MARKETS
from app.services.provider_metadata import acquisition_feeds, provider_catalog
from app.utils import utc_now
from scripts.preflight_catalog_schedule import migration_module

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


SUPPORTED_DATASETS = (
    "us_equity_eod",
    "tw_equity_eod",
    "tw_equity_minute",
    "tw_etf_minute",
    "hk_equity_eod",
    "tw_etf_eod",
    "tw_futures_eod",
)


def _eod_delivery_expectation(
    *,
    mode: str,
    market: str,
    source: str,
    minimum_count: int,
    slot_id: str,
    local_time: str,
    monitor_deadline: str = "17:00:00",
    target_date_lag_days: int = 0,
) -> dict:
    timezone, _, close = MARKETS[market]
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
            "market_close_time": close.isoformat(),
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
    timezone, _, close = MARKETS["TW"]
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
            "timezone": timezone,
            "market_close_time": close.isoformat(),
            "availability_grace_minutes": 60,
            "action": "warn",
        },
        "missing_delivery": {
            "action": "disabled",
            "expected_sources": [],
            "deadline_local_time": "17:00:00",
        },
        "schedule": {
            "enabled": False,
            "slot_id": "taiwan_market_window",
            "local_time": "14:30:00",
            "timezone": "Asia/Taipei",
            "target_date_lag_days": 0,
            "expected_sources": [],
        },
    }


def _contract_config(
    *,
    schema_id: str,
    market: str,
    asset_class: str,
    currency: str | None,
    allowed_sources: list[str],
    delivery_expectation: dict,
    accepted_schema_versions: list[int] | None = None,
    current_schema_version: int = 1,
) -> dict:
    """Return only contract, scope, and delivery policy keys.

    Provider-specific ``source_format``/``field_mapping``/``data_path`` keys
    are intentionally not seeded; adapters must produce the versioned body.
    """
    return {
        "schema_id": schema_id,
        "accepted_schema_versions": accepted_schema_versions or [1],
        "current_schema_version": current_schema_version,
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


def _catalog_feed(dataset_key: str) -> FeedDefinition:
    """Return the dataset's only Source feed; its identity is catalog-owned.

    Provider, market, asset class, frequency, calendar market and contract
    versions come from ``provider_catalog.v1.json``. A multi-provider dataset
    would need an explicit seed precedence decision, so it fails loudly here.
    """
    feeds = [
        feed
        for feed in provider_catalog().feeds
        if feed.dataset_key == dataset_key and feed.capabilities.source
    ]
    if len(feeds) != 1:
        raise ValueError(f"Seed requires exactly one catalog Source feed: {dataset_key}")
    return feeds[0]


def _catalog_contract_config(
    dataset_key: str, *, currency: str | None, delivery_expectation: dict
) -> dict:
    feed = _catalog_feed(dataset_key)
    return _contract_config(
        schema_id=feed.contract.schema_id,
        market=feed.market,
        asset_class=feed.asset_class,
        currency=currency,
        allowed_sources=[feed.provider],
        delivery_expectation=delivery_expectation,
        accepted_schema_versions=sorted(feed.contract.accepted_versions),
        current_schema_version=feed.contract.current_version,
    )


def _catalog_dataset(dataset_key: str, *, name: str, description: str, config: dict) -> dict:
    feed = _catalog_feed(dataset_key)
    return {
        "dataset_key": dataset_key,
        "name": name,
        "description": description,
        "asset_class": feed.asset_class,
        "market": feed.market,
        "frequency": feed.frequency,
        "is_active": True,
        "config": config,
    }


DATASETS: list[dict[str, Any]] = [
    _catalog_dataset(
        "us_equity_eod",
        name="美股日K",
        description="Twelve Data provider-neutral market_eod.v1 美國股票每日 OHLCV 資料",
        config=_catalog_contract_config(
            "us_equity_eod",
            currency="USD",
            delivery_expectation=_eod_delivery_expectation(
                mode="incremental",
                market=_catalog_feed("us_equity_eod").calendar_market,
                source=_catalog_feed("us_equity_eod").provider,
                minimum_count=1,
                slot_id="western_markets_window",
                local_time="08:15:00",
                monitor_deadline="09:15:00",
                target_date_lag_days=1,
            ),
        ),
    ),
    _catalog_dataset(
        "tw_equity_eod",
        name="台股日K",
        description="FinLab provider-neutral market_eod.v1 台灣股票每日 OHLCV 資料",
        config=_catalog_contract_config(
            "tw_equity_eod",
            currency="TWD",
            delivery_expectation=_eod_delivery_expectation(
                mode="full_snapshot",
                market=_catalog_feed("tw_equity_eod").calendar_market,
                source=_catalog_feed("tw_equity_eod").provider,
                minimum_count=2,
                slot_id="taiwan_market_window",
                local_time="14:30:00",
                monitor_deadline="17:00:00",
                target_date_lag_days=0,
            ),
        ),
    ),
    # Retained historical minute datasets have no catalog feed (Shioaji retired).
    {
        "dataset_key": "tw_equity_minute",
        "name": "台股股票分鐘 K",
        "description": "Retained historical provider-neutral market_minute.v1 台灣股票一分鐘 OHLCV 資料",
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
                allowed_sources=[],
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
        "description": "Retained historical provider-neutral market_minute.v1 台灣 ETF 一分鐘 OHLCV 資料",
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
                allowed_sources=[],
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


for _dataset in DATASETS:
    if _dataset["dataset_key"] in {
        "us_equity_eod",
        "tw_equity_eod",
        "tw_equity_minute",
        "tw_etf_minute",
    }:
        _dataset["config"]["full_market"] = {
            "enabled": False,
            "required": False,
            "activation_date": None,
        }


def _staged_eod_dataset(dataset_key: str, name: str, *, currency: str | None) -> dict[str, Any]:
    feed = _catalog_feed(dataset_key)
    provider, calendar_market = feed.provider, feed.calendar_market
    expectation = _eod_delivery_expectation(
        mode="full_snapshot",
        market=calendar_market,
        source=provider,
        minimum_count=0,
        slot_id="taiwan_market_window",
        local_time="14:30:00",
        monitor_deadline="23:00:00",
    )
    expectation["schedule"]["enabled"] = False
    expectation["missing_delivery"] = {"action": "disabled", "expected_sources": []}
    config = _catalog_contract_config(
        dataset_key, currency=currency, delivery_expectation=expectation
    )
    config["full_market"] = {
        "enabled": False,
        "required": True,
        "activation_date": None,
        "calendar_market": calendar_market,
    }
    config["source_precedence"] = [provider]
    return _catalog_dataset(
        dataset_key,
        name=name,
        description=f"Staged {provider} {feed.contract.schema_id}.v1 full-market daily feed",
        config=config,
    )


DATASETS.extend(
    [
        # HK contract versions (current v2, accepted v1/v2 so retained v1 raw stays
        # schema-valid) come from the catalog; new FactSet acquisition is separately
        # gated by the explicit HK cutover transaction.
        _staged_eod_dataset("hk_equity_eod", "港股日K", currency=None),
        _staged_eod_dataset("tw_etf_eod", "台灣 ETF 日K", currency="TWD"),
        _staged_eod_dataset("tw_futures_eod", "台灣期貨契約日K", currency="TWD"),
    ]
)


# Only schedule/deadline representation is catalog-owned. Other policies remain
# operator-owned, including disabled monitors and legal delivery slot values.
def _project_schedule(dataset_key: str, config: dict) -> dict:
    result = deepcopy(config)
    feed = next((f for f in acquisition_feeds() if f.dataset_key == dataset_key), None)
    if feed is None or feed.schedule is None:
        return result
    expectation = result["delivery_expectation"]
    expectation["schedule"].update(
        local_time=time.fromisoformat(feed.schedule.local_time).isoformat(),
        timezone=feed.schedule.timezone,
        target_date_lag_days=feed.schedule.target_date_lag_days,
        expected_sources=[feed.provider],
    )
    if feed.bounded_monitor.deadline is not None:
        policy = feed.bounded_monitor.deadline
        expectation["missing_delivery"].update(
            deadline_local_time=time.fromisoformat(policy.local_time).isoformat(),
            deadline_timezone=policy.timezone,
            deadline_day_offset=policy.trade_date_day_offset,
        )
    return result


for _dataset in DATASETS:
    _dataset["config"] = _project_schedule(_dataset["dataset_key"], _dataset["config"])


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

    migration = migration_module()
    if dataset_key in migration.FEEDS and any(
        field in existing_config.get("delivery_expectation", {})
        for field in ("schedule", "missing_delivery")
    ):
        status, _ = migration.project_config(dataset_key, existing_config)
        if status == "unknown_difference":
            raise ValueError(f"Unknown schedule differences: {dataset_key}")

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
                merged[key]["missing_delivery"] = deepcopy(value["missing_delivery"])
            continue
        merged[key] = deepcopy(value)

    # Operator overrides must never resurrect a retired acquisition provider.
    for name in ("schedule", "missing_delivery"):
        policy = merged.get("delivery_expectation", {}).get(name, {})
        sources = policy.get("expected_sources", [])
        if "shioaji" in sources:
            policy["expected_sources"] = [source for source in sources if source != "shioaji"]
            if not policy["expected_sources"]:
                if name == "schedule":
                    policy["enabled"] = False
                else:
                    policy["action"] = "disabled"
    return _project_schedule(dataset_key, merged)


async def seed_datasets(session: AsyncSession) -> None:
    """Upsert bounded feeds and inactive new full-market declarations."""
    logger.info("Seeding supported datasets: %s", ", ".join(SUPPORTED_DATASETS))
    # Validate the complete projection before seed can overwrite any clocks.
    report, _, _ = await session.run_sync(
        lambda sync: migration_module().inspect(sync.connection(), lock=True)
    )
    unknown = [row["key"] for row in report if row["status"] == "unknown_difference"]
    if unknown:
        raise ValueError("Unknown schedule differences: " + ", ".join(unknown))
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
        if dataset["dataset_key"] not in {"hk_equity_eod", "tw_etf_eod", "tw_futures_eod"}:
            existing.is_active = dataset["is_active"]
        existing.config = _merge_operator_config(
            dataset["dataset_key"],
            dataset["config"],
            existing.config,
        )
        existing.updated_at = utc_now()
    await session.flush()
    for provider in sorted({feed.provider for feed in acquisition_feeds()}):
        feeds = [feed for feed in acquisition_feeds() if feed.provider == provider]
        feed = feeds[0]
        assert feed.schedule is not None
        datasets = [item.dataset_key for item in feeds]
        slot = feed.delivery.slot_id
        clock = time.fromisoformat(feed.schedule.local_time)
        zone = feed.schedule.timezone
        key = scheduler_key(provider)
        await session.execute(
            insert(SchedulerControl)
            .values(
                scheduler_key=key,
                provider=provider,
                execution_profile="full_market",
                slot_id=slot,
                scheduled_local_time=clock,
                timezone=zone,
                desired_state="stopped",
                observed_state="stopped",
                revision=1,
            )
            .on_conflict_do_update(
                index_elements=["scheduler_key"],
                set_={"scheduled_local_time": clock, "timezone": zone},
                where=SchedulerControl.retired_at.is_(None),
            )
        )
        for dataset_key in datasets:
            await session.execute(
                insert(SchedulerDataset)
                .values(
                    scheduler_key=key,
                    dataset_key=dataset_key,
                )
                .on_conflict_do_nothing()
            )
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
