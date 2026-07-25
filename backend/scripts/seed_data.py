"""
Script to seed initial dataset registry and crypto instruments.
"""

import asyncio
import logging

from sqlalchemy import cast, func, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.base import Base
from app.models.canonical import Instrument, InstrumentIdentifier
from app.models.registry import DatasetRegistry
from app.utils import utc_now, uuid7

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


# Initial dataset configurations
DATASETS = [
    {
        "dataset_key": "crypto_eod",
        "name": "加密貨幣日K",
        "description": "Bloomberg 加密貨幣每日價格資料",
        "asset_class": "crypto",
        "market": "CRYPTO",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_crypto",
            "field_mapping": {
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "trade_date": "timestamp.last_update",
            },
            "identifier_type": "bloomberg",
            "identifier_field": "ticker",
        },
    },
    {
        "dataset_key": "us_equity_eod",
        "name": "美股日K",
        "description": "美國股票每日價格資料",
        "asset_class": "equity",
        "market": "US",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "schema_id": "market_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "audit",
            "defaults": {
                "market": "US",
                "asset_class": "equity",
                "currency": "USD",
            },
            "delivery_expectation": {
                "delivery_mode": "incremental",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 1,
                    "maximum_count_drop_ratio": 0.0,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "US",
                    "timezone": "America/New_York",
                    "market_close_time": "16:00:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
            },
            "source_format": "bloomberg_equity_api",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.last_update",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "volume",
                "turnover": "turnover",
            },
        },
    },
    {
        "dataset_key": "us_index_eod",
        "name": "美股指數日K",
        "description": "美國指數每日價格資料",
        "asset_class": "index",
        "market": "US",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_index_api",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.last_update",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "volume",
                "turnover": "turnover",
            },
        },
    },
    {
        "dataset_key": "tw_equity_eod",
        "name": "台股日K — FinLab Direct",
        "description": "FinLab Direct 格式台股每日 OHLCV 資料",
        "asset_class": "equity",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "schema_id": "market_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "audit",
            "defaults": {
                "market": "TW",
                "asset_class": "equity",
                "currency": "TWD",
            },
            "delivery_expectation": {
                "delivery_mode": "full_snapshot",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 2100,
                    "maximum_count_drop_ratio": 0.1,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "TW",
                    "timezone": "Asia/Taipei",
                    "market_close_time": "13:30:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
            },
            "source_format": "finlab_twstock_direct",
            "data_path": "data",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "total_volume",
                "total_ticks": "total_ticks",
            },
        },
    },
    {
        "dataset_key": "tw_etf_eod",
        "name": "台股 ETF 日K — FinLab Direct",
        "description": "FinLab Direct 格式台股 ETF 每日 OHLCV 資料",
        "asset_class": "etf",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "schema_id": "market_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "audit",
            "defaults": {
                "market": "TW",
                "asset_class": "etf",
                "currency": "TWD",
            },
            "delivery_expectation": {
                "delivery_mode": "full_snapshot",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 190,
                    "maximum_count_drop_ratio": 0.1,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "TW",
                    "timezone": "Asia/Taipei",
                    "market_close_time": "13:30:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
            },
            "source_format": "finlab_twstock_direct",
            "data_path": "data",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "total_volume",
                "total_ticks": "total_ticks",
            },
        },
    },
    {
        "dataset_key": "tw_equity_bloomberg_eod",
        "name": "台股日K — Bloomberg",
        "description": "Bloomberg API 格式台灣股票每日價格資料",
        "asset_class": "equity",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "hk_equity_eod",
        "name": "港股日K",
        "description": "香港股票每日價格資料",
        "asset_class": "equity",
        "market": "HK",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "cn_equity_eod",
        "name": "陸股日K",
        "description": "中國股票每日價格資料",
        "asset_class": "equity",
        "market": "CN",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "tw_index_eod",
        "name": "台股指數日K",
        "description": "台灣指數每日價格資料",
        "asset_class": "index",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "hk_index_eod",
        "name": "港股指數日K",
        "description": "香港指數每日價格資料",
        "asset_class": "index",
        "market": "HK",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "cn_index_eod",
        "name": "陸股指數日K",
        "description": "中國指數每日價格資料",
        "asset_class": "index",
        "market": "CN",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "fx_eod",
        "name": "外匯日K",
        "description": "全球外匯每日價格資料",
        "asset_class": "fx",
        "market": "FX",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_fx_api",
            "symbol_field": "pair",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.last_update",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "volume",
                "turnover": "turnover",
            },
        },
    },
    {
        "dataset_key": "macro_observation",
        "name": "宏觀指標",
        "description": "宏觀經濟指標時間序列資料",
        "asset_class": "macro",
        "market": "MACRO",
        "frequency": "varies",
        "is_active": True,
        "config": {
            "data_path": "data",
            "field_mapping": {
                "source_code": "source_code",
                "name": "name",
                "unit": "unit",
                "frequency": "frequency",
                "market": "market",
                "obs_date": "date",
                "value": "value",
                "source": "source",
            },
        },
    },
    {
        "dataset_key": "futures_contracts",
        "name": "台指期期貨合約",
        "description": "台指期期貨合約資料",
        "asset_class": "future",
        "market": "WTX",
        "frequency": "contract",
        "is_active": True,
        "config": {
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "contract_code": "contract_code",
                "contract_month": "contract_month",
                "expiry_date": "expiry_date",
                "currency": "currency",
                "extra": "extra",
            },
        },
    },
    {
        "dataset_key": "futures_continuous_eod",
        "name": "台指期連續日K",
        "description": "台指期期貨連續日K資料",
        "asset_class": "future",
        "market": "WTX",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "schema_id": "futures_continuous_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "audit",
            "defaults": {
                "market": "WTX",
                "asset_class": "future",
                "currency": "TWD",
            },
            "delivery_expectation": {
                "delivery_mode": "full_snapshot",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 1,
                    "maximum_count_drop_ratio": 0.5,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "WTX",
                    "timezone": "Asia/Taipei",
                    "market_close_time": "13:45:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
            },
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "trade_date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "turnover": "turnover",
                "roll_rule": "roll_rule",
                "roll_rule_name": "roll_rule.name",
                "roll_rule_description": "roll_rule.description",
                "roll_rule_config": "roll_rule.config",
            },
        },
    },
    {
        "dataset_key": "us_equity_corporate_actions",
        "name": "美股公司行為",
        "description": "美股公司行為（除權息/分割）",
        "asset_class": "equity",
        "market": "US",
        "frequency": "event",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_corporate_actions",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "action_type": "action.type",
                "ex_date": "action.ex_date",
                "record_date": "action.record_date",
                "pay_date": "action.pay_date",
                "ratio": "action.ratio",
                "cash_amount": "action.cash_amount",
                "currency": "action.currency",
            },
        },
    },
    {
        "dataset_key": "crypto_index_eod",
        "name": "加密貨幣指數日K",
        "description": "加密貨幣指數每日價格資料",
        "asset_class": "index",
        "market": "CRYPTO",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_crypto_index_api",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.last_update",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "volume",
                "turnover": "turnover",
            },
        },
    },
    {
        "dataset_key": "us_stock_eod",
        "name": "美股日K (Bloomberg API)",
        "description": "Bloomberg API 美國股票每日價格資料",
        "asset_class": "equity",
        "market": "US",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_usstock_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "us_stock_index_eod",
        "name": "美股指數日K (Bloomberg API)",
        "description": "Bloomberg API 美國指數每日價格資料",
        "asset_class": "index",
        "market": "US",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_usstock_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "global_stock_eod",
        "name": "全球股票日K (Bloomberg API)",
        "description": "Bloomberg API 全球股票每日價格資料（包含 US, DE, TW, JP, CN 等市場）",
        "asset_class": "equity",
        "market": "GLOBAL",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_usstock_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    {
        "dataset_key": "hkchina_stock_eod",
        "name": "港中股票日K (Bloomberg API)",
        "description": "Bloomberg API 港股與中國相關股票每日價格資料",
        "asset_class": "equity",
        "market": "GLOBAL",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_api",
            "data_path": "data",
            "symbol_field": "symbol",
            "name_field": "name",
            "source_field": "metadata.source",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "volume": "price.volume",
            },
        },
    },
    # Bloomberg direct format datasets
    {
        "dataset_key": "crypto_bloomberg_eod",
        "name": "加密貨幣日K — Bloomberg Direct",
        "description": "Bloomberg Direct 格式加密貨幣每日價格資料",
        "asset_class": "crypto",
        "market": "CRYPTO",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "bloomberg_crypto_direct"},
    },
    {
        "dataset_key": "fx_bloomberg_eod",
        "name": "外匯日K — Bloomberg Direct",
        "description": "Bloomberg Direct 格式外匯每日價格資料",
        "asset_class": "fx",
        "market": "FX",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "bloomberg_fx_direct"},
    },
    {
        "dataset_key": "wtx_eod",
        "name": "WTX 期貨日K",
        "description": "台灣加權指數期貨每日 OHLCV 資料（支援 FinLab 與 Bloomberg 來源）",
        "asset_class": "future",
        "market": "WTX",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "schema_id": "futures_continuous_eod",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "audit",
            "defaults": {
                "market": "WTX",
                "asset_class": "future",
                "currency": "TWD",
            },
            "delivery_expectation": {
                "delivery_mode": "full_snapshot",
                "baseline": {
                    "strategy": "rolling_median",
                    "scope": "dataset_source_schema",
                    "window_size": 7,
                    "minimum_history": 3,
                },
                "record_count": {
                    "minimum_record_count": 1,
                    "maximum_count_drop_ratio": 0.5,
                    "action": "warn",
                },
                "freshness": {
                    "maximum_fetch_age_hours": 36,
                    "allowed_clock_skew_minutes": 5,
                    "action": "warn",
                },
                "latest_date": {
                    "calendar_market": "WTX",
                    "timezone": "Asia/Taipei",
                    "market_close_time": "13:45:00",
                    "availability_grace_minutes": 120,
                    "action": "warn",
                },
            },
            "source_format": "direct",
        },
    },
    {
        "dataset_key": "macro_bloomberg_observation",
        "name": "總經觀測值 — Bloomberg Direct",
        "description": "Bloomberg Direct 格式宏觀經濟指標觀測值",
        "asset_class": "macro",
        "market": "MACRO",
        "frequency": "various",
        "is_active": True,
        "config": {"source_format": "bloomberg_macro_direct"},
    },
]


# Initial crypto instruments
CRYPTO_INSTRUMENTS = [
    {
        "symbol": "BTC",
        "name": "Bitcoin",
        "bloomberg_ticker": "XBTUSD BGN Curncy",
        "currency": "USD",
    },
    {
        "symbol": "ETH",
        "name": "Ethereum",
        "bloomberg_ticker": "XETUSD BGN Curncy",
        "currency": "USD",
    },
    {
        "symbol": "XRP",
        "name": "Ripple",
        "bloomberg_ticker": "XRP Curncy",
        "currency": "USD",
    },
    {
        "symbol": "SOL",
        "name": "Solana",
        "bloomberg_ticker": "XSO Curncy",
        "currency": "USD",
    },
    {
        "symbol": "ADA",
        "name": "Cardano",
        "bloomberg_ticker": "XAD BGN Curncy",
        "currency": "USD",
    },
]


async def seed_datasets(session: AsyncSession):
    """Seed dataset registry."""
    logger.info("Seeding datasets...")

    for ds in DATASETS:
        stmt = (
            insert(DatasetRegistry)
            .values(
                dataset_key=ds["dataset_key"],
                name=ds["name"],
                description=ds["description"],
                asset_class=ds["asset_class"],
                market=ds["market"],
                frequency=ds["frequency"],
                is_active=ds["is_active"],
                config=ds["config"],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            .on_conflict_do_update(
                index_elements=["dataset_key"],
                set_={
                    "name": ds["name"],
                    "description": ds["description"],
                    # Seed defaults fill missing top-level keys, while operator-owned
                    # production config always wins. Alembic handles nested policy
                    # evolution without destructive seed overwrites.
                    "config": cast(ds["config"], JSONB).op("||")(
                        func.coalesce(DatasetRegistry.config, cast({}, JSONB))
                    ),
                    "is_active": ds["is_active"],
                    "updated_at": utc_now(),
                },
            )
        )
        await session.execute(stmt)

        if ds["dataset_key"] in {
            "us_equity_eod",
            "tw_equity_eod",
            "tw_etf_eod",
            "futures_continuous_eod",
            "wtx_eod",
        }:
            await session.execute(
                text("""
                    UPDATE dataset_registry
                    SET config = jsonb_set(
                        config,
                        '{delivery_expectation,missing_delivery}',
                        '{"action":"disabled","expected_sources":[]}'::jsonb,
                        true
                    )
                    WHERE dataset_key = :dataset_key
                      AND jsonb_typeof(config->'delivery_expectation') = 'object'
                      AND NOT (config->'delivery_expectation' ? 'missing_delivery')
                """),
                {"dataset_key": ds["dataset_key"]},
            )

    await session.commit()
    logger.info(f"Seeded {len(DATASETS)} datasets")


async def seed_crypto_instruments(session: AsyncSession):
    """Seed initial crypto instruments."""
    logger.info("Seeding crypto instruments...")

    for crypto in CRYPTO_INSTRUMENTS:
        # Create instrument
        instrument_id = uuid7()
        stmt = (
            insert(Instrument)
            .values(
                instrument_id=instrument_id,
                asset_class="crypto",
                market="CRYPTO",
                symbol=crypto["symbol"],
                name=crypto["name"],
                currency=crypto["currency"],
                status="active",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            .on_conflict_do_nothing()
        )

        result = await session.execute(stmt)

        # If inserted (not a conflict), add identifier
        if result.rowcount > 0:
            # Add Bloomberg ticker identifier
            ident_stmt = (
                insert(InstrumentIdentifier)
                .values(
                    id=uuid7(),
                    instrument_id=instrument_id,
                    id_type="bloomberg",
                    id_value=crypto["bloomberg_ticker"],
                    source="seed",
                    created_at=utc_now(),
                )
                .on_conflict_do_nothing()
            )
            await session.execute(ident_stmt)

    await session.commit()
    logger.info(f"Seeded {len(CRYPTO_INSTRUMENTS)} crypto instruments")


async def main():
    """Main entry point."""
    logger.info("Starting database seeding...")

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    async_session = async_sessionmaker(engine, class_=AsyncSession)

    async with engine.begin() as conn:
        # Ensure schema exists
        await conn.execute(text("CREATE SCHEMA IF NOT EXISTS raw"))
        # Create tables
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        await seed_datasets(session)
        await seed_crypto_instruments(session)

    await engine.dispose()
    logger.info("Database seeding completed!")


if __name__ == "__main__":
    asyncio.run(main())
