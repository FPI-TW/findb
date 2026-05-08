"""
Data ingestion service.
Handles raw data storage and triggers normalization.
"""

import copy
import logging
from datetime import timedelta
from typing import Any, Optional, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.base import async_session_maker
from app.models.raw import RawMarketPayload
from app.models.registry import DatasetRegistry, IngestionRun
from app.schemas.source import IngestRequest, IngestRequestV2
from app.services.normalize import (
    BaseNormalizer,
    CNEquityNormalizer,
    CNIndexNormalizer,
    CorporateActionNormalizer,
    CryptoBloombergNormalizer,
    CryptoIndexNormalizer,
    CryptoNormalizer,
    EquityNormalizer,
    FuturesContinuousNormalizer,
    FuturesContractNormalizer,
    FXBloombergNormalizer,
    FXNormalizer,
    GlobalStockNormalizer,
    HKChinaIndexNormalizer,
    HKChinaMixedNormalizer,
    HKEquityNormalizer,
    HKIndexNormalizer,
    IndexNormalizer,
    MacroBloombergNormalizer,
    MacroNormalizer,
    TWEquityNormalizer,
    TWIndexNormalizer,
    TWStockMultichartsNormalizer,
    USIndexNormalizer,
    USStockNormalizer,
    WTXBloombergNormalizer,
)
from app.utils import utc_now, uuid7
from app.utils.datetime_utils import ensure_utc

settings = get_settings()
logger = logging.getLogger(__name__)

_NORMALIZATION_FALLBACK_FAILURE_STATUSES = {"pending", "processing", "running"}
_NORMALIZATION_ERROR_MESSAGE_LIMIT = 1000


class NormalizerFactory(Protocol):
    def __call__(
        self,
        db: AsyncSession,
        dataset_config: dict | None = None,
    ) -> BaseNormalizer: ...


NORMALIZER_MAP: dict[str, NormalizerFactory] = {
    "crypto_eod": CryptoNormalizer,
    "crypto_index_eod": CryptoIndexNormalizer,
    "us_equity_eod": EquityNormalizer,
    "us_index_eod": IndexNormalizer,
    "fx_eod": FXNormalizer,
    "us_equity_corporate_actions": CorporateActionNormalizer,
    "macro_observation": MacroNormalizer,
    "futures_contracts": FuturesContractNormalizer,
    "futures_continuous_eod": FuturesContinuousNormalizer,
    # Bloomberg US Stock API format
    "us_stock_eod": USStockNormalizer,
    "us_stock_index_eod": USIndexNormalizer,
    "global_stock_eod": GlobalStockNormalizer,
    "hkchina_stock_eod": GlobalStockNormalizer,
    "hkchina_mixed_eod": HKChinaMixedNormalizer,
    "tw_equity_eod": TWEquityNormalizer,
    "tw_equity_multicharts_eod": TWStockMultichartsNormalizer,
    "hk_equity_eod": HKEquityNormalizer,
    "cn_equity_eod": CNEquityNormalizer,
    "tw_index_eod": TWIndexNormalizer,
    "hk_index_eod": HKIndexNormalizer,
    "cn_index_eod": CNIndexNormalizer,
    "hkchina_index_eod": HKChinaIndexNormalizer,
    # Bloomberg direct format — other markets
    "fx_bloomberg_eod": FXBloombergNormalizer,
    "crypto_bloomberg_eod": CryptoBloombergNormalizer,
    "wtx_bloomberg_eod": WTXBloombergNormalizer,
    "macro_bloomberg_observation": MacroBloombergNormalizer,
}

DIRECT_DATASET_DEFAULTS: dict[str, dict] = {
    "us_stock_eod": {
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
    "hkchina_mixed_eod": {
        "name": "港中股票與指數日K (Bloomberg API)",
        "description": "Bloomberg API 港股與中國相關股票及指數每日價格資料",
        "asset_class": "mixed",
        "market": "GLOBAL",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_mixed_api",
            "data_path": "data",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            },
        },
    },
    "hkchina_stock_eod": {
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
    "hkchina_index_eod": {
        "name": "港中指數日K (Bloomberg API)",
        "description": "Bloomberg API 港股與中國相關指數每日價格資料",
        "asset_class": "index",
        "market": "GLOBAL",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "bloomberg_hkchina_index_api",
            "data_path": "data",
            "identifier_field": "ticker",
            "identifier_type": "bloomberg",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            },
        },
    },
    "crypto_bloomberg_eod": {
        "name": "加密貨幣日K — Bloomberg Direct",
        "description": "Bloomberg Direct 格式加密貨幣每日價格資料",
        "asset_class": "crypto",
        "market": "CRYPTO",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "bloomberg_crypto_direct"},
    },
    "fx_bloomberg_eod": {
        "name": "外匯日K — Bloomberg Direct",
        "description": "Bloomberg Direct 格式外匯每日價格資料",
        "asset_class": "fx",
        "market": "FX",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "bloomberg_fx_direct"},
    },
    "wtx_bloomberg_eod": {
        "name": "WTX 期貨日K — Bloomberg Direct",
        "description": "Bloomberg Direct 格式台灣加權指數期貨每日價格資料",
        "asset_class": "future",
        "market": "WTX",
        "frequency": "daily",
        "is_active": True,
        "config": {"source_format": "bloomberg_wtx_direct"},
    },
    "macro_bloomberg_observation": {
        "name": "總經觀測值 — Bloomberg Direct",
        "description": "Bloomberg Direct 格式宏觀經濟指標觀測值",
        "asset_class": "macro",
        "market": "MACRO",
        "frequency": "various",
        "is_active": True,
        "config": {"source_format": "bloomberg_macro_direct"},
    },
    "tw_equity_multicharts_eod": {
        "name": "台股日K — MultiCharts Direct",
        "description": "MultiCharts Direct 格式台股每日價格與成交拆分資料",
        "asset_class": "equity",
        "market": "TW",
        "frequency": "daily",
        "is_active": True,
        "config": {
            "source_format": "multicharts_twstock_direct",
            "data_path": "data",
            "field_mapping": {
                "trade_date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "total_volume",
                "up_volume": "up_volume",
                "down_volume": "down_volume",
                "up_ticks": "up_ticks",
                "down_ticks": "down_ticks",
                "total_ticks": "total_ticks",
            },
        },
    },
}


async def ensure_direct_dataset_exists(db: AsyncSession, dataset_key: str) -> None:
    """Upsert a DatasetRegistry row for built-in direct ingest datasets if missing."""
    defaults = DIRECT_DATASET_DEFAULTS.get(dataset_key)
    if defaults is None:
        return
    existing = await db.get(DatasetRegistry, dataset_key)
    if existing is not None:
        return
    dataset = DatasetRegistry(
        dataset_key=dataset_key,
        name=defaults["name"],
        description=defaults["description"],
        asset_class=defaults["asset_class"],
        market=defaults["market"],
        frequency=defaults["frequency"],
        is_active=defaults["is_active"],
        config=copy.deepcopy(defaults["config"]),
    )
    try:
        async with db.begin_nested():
            db.add(dataset)
            await db.flush()
    except IntegrityError:
        pass


class DatasetNotFoundError(ValueError):
    """Raised when a dataset key is not found."""


class DatasetInactiveError(ValueError):
    """Raised when a dataset is inactive."""


class PayloadValidationError(ValueError):
    """Raised when payload schema is invalid."""


class RawPayloadNotFoundError(ValueError):
    """Raised when raw payload for a run is not found."""


class MarketMismatchError(ValueError):
    """Raised when dataset market does not match ingest API market."""


def _normalize_market(market: str) -> str:
    """Normalize market code for comparisons."""
    return market.strip().upper()


def _get_nested_value(data: dict, path: str | None) -> Any:
    """Get value from nested dict using dot notation."""
    if not path:
        return None
    value: Any = data
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return None
    return value


def _has_value(value: Any) -> bool:
    """Check if a value is present and non-empty."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _candidate_paths(primary: str | None, fallbacks: list[str]) -> list[str]:
    """Build candidate paths with fallbacks."""
    paths: list[str] = []
    if primary:
        paths.append(primary)
    for fallback in fallbacks:
        if fallback not in paths:
            paths.append(fallback)
    return paths


def _any_item_has_any_path(items: list[dict], paths: list[str]) -> bool:
    """Check if any item contains a value for any candidate path."""
    for path in paths:
        for item in items:
            if _has_value(_get_nested_value(item, path)):
                return True
    return False


def _format_normalization_error(exc: Exception) -> str:
    """Build a bounded run-level error message for unexpected normalization failures."""
    error_type = type(exc).__name__
    error_detail = str(exc).strip()
    message = f"Normalization failed: {error_type}"
    if error_detail:
        message = f"{message}: {error_detail}"
    if len(message) > _NORMALIZATION_ERROR_MESSAGE_LIMIT:
        return f"{message[: _NORMALIZATION_ERROR_MESSAGE_LIMIT - 3]}..."
    return message


async def _mark_unhandled_normalization_failure(
    session: AsyncSession,
    run_id: UUID,
    exc: Exception,
) -> None:
    """Persist a failed run status when a normalizer crashes before doing so."""
    try:
        await session.rollback()
    except Exception:
        logger.exception(
            "Rollback failed after normalization error (run_id=%s)",
            run_id,
        )
        return

    try:
        run = await session.get(IngestionRun, run_id)
        if not run:
            logger.warning(
                "Normalization failed but ingestion run was not found (run_id=%s)",
                run_id,
            )
            return
        if run.status not in _NORMALIZATION_FALLBACK_FAILURE_STATUSES:
            logger.info(
                "Normalization failed but run already has status %s (run_id=%s)",
                run.status,
                run_id,
            )
            return

        run.status = "failed"
        run.error_message = _format_normalization_error(exc)
        run.completed_at = utc_now()
        await session.commit()
    except Exception:
        logger.exception(
            "Failed to persist normalization failure status (run_id=%s)",
            run_id,
        )


async def trigger_normalization(
    dataset_key: str,
    payload: dict,
    run_id: UUID,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Trigger normalization for a dataset in the background."""
    normalizer_cls = NORMALIZER_MAP.get(dataset_key)
    session_factory = session_factory or async_session_maker

    async with session_factory() as session:
        if not normalizer_cls:
            run = await session.get(IngestionRun, run_id)
            if run:
                run.status = "failed"
                run.error_message = f"No normalizer configured for {dataset_key}"
                run.completed_at = utc_now()
            await session.commit()
            logger.warning("No normalizer configured for %s", dataset_key)
            return

        dataset = await session.get(DatasetRegistry, dataset_key)
        if not dataset:
            run = await session.get(IngestionRun, run_id)
            if run:
                run.status = "failed"
                run.error_message = f"Dataset {dataset_key} not found"
                run.completed_at = utc_now()
            await session.commit()
            logger.warning("Dataset not found for normalization: %s", dataset_key)
            return

        normalizer = normalizer_cls(session, dataset.config or {})
        try:
            await normalizer.process(payload, run_id)
        except Exception as exc:
            await _mark_unhandled_normalization_failure(session, run_id, exc)
            logger.exception("Normalization failed for %s (run_id=%s)", dataset_key, run_id)


async def trigger_normalization_v2(
    dataset_key: str,
    payload: dict,
    run_id: UUID,
    session: AsyncSession | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Trigger normalization for a dataset."""
    normalizer_cls = NORMALIZER_MAP.get(dataset_key)

    if session is not None:
        await _execute_normalization(
            session=session,
            normalizer_cls=normalizer_cls,
            dataset_key=dataset_key,
            payload=payload,
            run_id=run_id,
        )
    else:
        session_factory = session_factory or async_session_maker
        async with session_factory() as new_session:
            await _execute_normalization(
                session=new_session,
                normalizer_cls=normalizer_cls,
                dataset_key=dataset_key,
                payload=payload,
                run_id=run_id,
            )


async def _execute_normalization(
    session: AsyncSession, normalizer_cls: Any, dataset_key: str, payload: dict, run_id: UUID
) -> None:
    """Internal functions perform the actual normalization logic."""
    if not normalizer_cls:
        run = await session.get(IngestionRun, run_id)
        if run:
            run.status = "failed"
            run.error_message = f"No normalizer configured for {dataset_key}"
            run.completed_at = utc_now()
        await session.commit()
        logger.warning("No normalizer configured for %s", dataset_key)
        return

    dataset = await session.get(DatasetRegistry, dataset_key)
    if not dataset:
        run = await session.get(IngestionRun, run_id)
        if run:
            run.status = "failed"
            run.error_message = f"Dataset {dataset_key} not found"
            run.completed_at = utc_now()
        await session.commit()
        logger.warning("Dataset not found for normalization: %s", dataset_key)
        return

    normalizer = normalizer_cls(session, dataset.config or {})
    try:
        await normalizer.process(payload, run_id)
    except Exception:
        logger.exception("Normalization failed for %s (run_id=%s)", dataset_key, run_id)
        run = await session.get(IngestionRun, run_id)
        if run:
            run.status = "failed"
            run.error_message = "Normalization process failed during execution"
            run.completed_at = utc_now()
        await session.commit()


class IngestionService:
    """Service for handling data ingestion."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_raw_payload_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> Optional[RawMarketPayload]:
        """Get raw payload by idempotency key."""
        stmt = select(RawMarketPayload).where(
            RawMarketPayload.idempotency_key == idempotency_key,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_raw_payload_by_message_id(
        self,
        message_id: str,
    ) -> Optional[RawMarketPayload]:
        """Get raw payload by message_id."""
        stmt = select(RawMarketPayload).where(
            RawMarketPayload.message_id == message_id,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_ingestion_run_by_message_id(
        self,
        message_id: str,
    ) -> Optional[IngestionRun]:
        """Get ingestion run by message_id."""
        stmt = select(IngestionRun).where(IngestionRun.message_id == message_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_dataset(
        self,
        dataset_key: str,
        include_inactive: bool = False,
    ) -> Optional[DatasetRegistry]:
        """Get dataset configuration."""
        stmt = select(DatasetRegistry).where(DatasetRegistry.dataset_key == dataset_key)
        if not include_inactive:
            stmt = stmt.where(DatasetRegistry.is_active.is_(True))
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    def validate_payload_schema(self, payload: dict, dataset: DatasetRegistry) -> list[dict]:
        """Validate payload schema using dataset config."""
        if not isinstance(payload, dict):
            raise PayloadValidationError("Payload must be an object")

        config = dataset.config or {}
        data_path = config.get("data_path", "data")
        data_items = _get_nested_value(payload, data_path) if data_path else payload.get("data")
        data_label = data_path or "data"

        if data_items is None:
            raise PayloadValidationError(f"Payload missing data list at '{data_label}'")
        if not isinstance(data_items, list):
            raise PayloadValidationError(f"Payload data at '{data_label}' must be a list")
        if not data_items:
            return []
        if not all(isinstance(item, dict) for item in data_items):
            raise PayloadValidationError("Payload data items must be objects")

        field_mapping = config.get("field_mapping", {}) or {}
        if "action_type" in field_mapping or dataset.dataset_key.endswith("corporate_actions"):
            required = {
                "action_type": _candidate_paths(
                    field_mapping.get("action_type"),
                    ["action_type", "action.type", "type"],
                ),
                "ex_date": _candidate_paths(
                    field_mapping.get("ex_date"),
                    ["ex_date", "exDate", "action.ex_date"],
                ),
            }
        elif (
            "obs_date" in field_mapping
            or dataset.asset_class == "macro"
            or dataset.dataset_key.startswith("macro")
        ):
            required = {
                "obs_date": _candidate_paths(
                    field_mapping.get("obs_date"),
                    ["obs_date", "date", "observation_date", "timestamp"],
                )
            }
        elif (
            "contract_code" in field_mapping
            and dataset.asset_class == "future"
            and dataset.dataset_key.endswith("contracts")
        ):
            required = {
                "contract_code": _candidate_paths(
                    field_mapping.get("contract_code"),
                    ["contract_code", "contractCode", "code"],
                )
            }
        else:
            required = {
                "trade_date": _candidate_paths(
                    field_mapping.get("trade_date"),
                    ["trade_date", "timestamp.last_update", "date"],
                )
            }

        for field_name, paths in required.items():
            if not _any_item_has_any_path(data_items, paths):
                raise PayloadValidationError(f"Payload missing required field: {field_name}")

        return data_items

    async def create_ingestion_run(
        self,
        dataset_key: str,
        source: str | None = None,
        request_key: str | None = None,
        raw_records: int = 0,
        metadata: dict | None = None,
    ) -> IngestionRun:
        """Create a new ingestion run record."""
        run = IngestionRun(
            run_id=uuid7(),
            dataset_key=dataset_key,
            source=source,
            request_key=request_key,
            raw_records=raw_records,
            status="pending",
            metadata_=metadata,
            created_at=utc_now(),
        )
        self.db.add(run)
        await self.db.flush()
        return run

    async def create_ingestion_run_v2(
        self,
        dataset_key: str,
        source: str | None = None,
        request_key: str | None = None,
        message_id: str | None = None,
        raw_records: int = 0,
        metadata: dict | None = None,
        status: str = "pending",
    ) -> IngestionRun:
        """Create a new ingestion run record for v2 path."""
        run = IngestionRun(
            run_id=uuid7(),
            dataset_key=dataset_key,
            source=source,
            request_key=request_key,
            message_id=message_id,
            raw_records=raw_records,
            status=status,
            started_at=utc_now() if status == "processing" else None,
            metadata_=metadata,
            created_at=utc_now(),
        )
        self.db.add(run)
        await self.db.flush()
        return run

    async def store_raw_payload(
        self,
        request: IngestRequest,
        run_id: UUID,
    ) -> RawMarketPayload:
        """Store raw payload in the database."""
        fetched_at = ensure_utc(request.fetched_at)
        expire_at = fetched_at + timedelta(days=settings.RAW_RETENTION_DAYS)

        raw_payload = RawMarketPayload(
            dataset_key=request.dataset_key,
            source=request.source,
            request_key=request.request_key,
            idempotency_key=request.idempotency_key,
            payload=request.payload,
            fetched_at=fetched_at,
            expire_at=expire_at,
            run_id=run_id,
            created_at=utc_now(),
        )

        self.db.add(raw_payload)
        await self.db.flush()
        return raw_payload

    async def store_raw_payload_v2(
        self,
        request: IngestRequestV2,
        run_id: UUID,
    ) -> RawMarketPayload:
        """Store raw payload for v2 path."""
        fetched_at = ensure_utc(request.fetched_at)
        expire_at = fetched_at + timedelta(days=settings.RAW_RETENTION_DAYS)

        # idempotency_key is the PK (NOT NULL, unique constraint), so it must be populated.
        # For v2, we write message_id into both fields: idempotency_key keeps the PK unique
        # constraint that guards against race conditions (IntegrityError fallback in ingest_v2),
        # while message_id is the dedicated queryable column with clear semantics.
        raw_payload = RawMarketPayload(
            dataset_key=request.dataset_key,
            source=request.source,
            request_key=request.request_key,
            idempotency_key=request.message_id,
            message_id=request.message_id,
            payload=request.payload,
            fetched_at=fetched_at,
            expire_at=expire_at,
            run_id=run_id,
            created_at=utc_now(),
        )

        self.db.add(raw_payload)
        await self.db.flush()
        return raw_payload

    async def get_raw_payload_by_run(self, run_id: UUID) -> Optional[RawMarketPayload]:
        """Get raw payload by ingestion run id."""
        stmt = select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def rerun_from_raw(self, run_id: UUID) -> tuple[UUID, str, str, dict]:
        """Create a new ingestion run from a stored raw payload."""
        raw_payload = await self.get_raw_payload_by_run(run_id)
        if not raw_payload:
            raise RawPayloadNotFoundError(f"Raw payload not found for run {run_id}")

        dataset = await self.get_dataset(raw_payload.dataset_key, include_inactive=True)
        if not dataset:
            raise DatasetNotFoundError(f"Dataset {raw_payload.dataset_key} not found")
        if not dataset.is_active:
            raise DatasetInactiveError(f"Dataset {raw_payload.dataset_key} is inactive")

        data_items = self.validate_payload_schema(raw_payload.payload, dataset)
        raw_records = len(data_items)
        metadata = {
            "source": raw_payload.source,
            "request_key": raw_payload.request_key,
            "raw_records": raw_records,
            "rerun_from_run_id": str(run_id),
            "rerun_from_idempotency_key": raw_payload.idempotency_key,
        }

        run = await self.create_ingestion_run(
            raw_payload.dataset_key,
            source=raw_payload.source,
            request_key=raw_payload.request_key,
            raw_records=raw_records,
            metadata=metadata,
        )
        await self.db.commit()

        return run.run_id, run.status, raw_payload.dataset_key, raw_payload.payload

    async def ingest(
        self,
        request: IngestRequest,
        expected_market: str | None = None,
    ) -> tuple[UUID, str, bool]:
        """
        Process an ingestion request.

        Returns:
            Tuple of (run_id, status, is_duplicate)
        """
        # Validate dataset exists and active
        dataset = await self.get_dataset(request.dataset_key, include_inactive=True)
        if not dataset:
            logger.warning("Ingestion rejected: dataset not found %s", request.dataset_key)
            raise DatasetNotFoundError(f"Dataset {request.dataset_key} not found")
        if not dataset.is_active:
            run = await self.create_ingestion_run(
                request.dataset_key,
                source=request.source,
                request_key=request.request_key,
                raw_records=0,
                metadata={
                    "source": request.source,
                    "request_key": request.request_key,
                    "raw_records": 0,
                },
            )
            run.status = "failed"
            run.error_message = f"Dataset {request.dataset_key} is inactive"
            run.completed_at = utc_now()
            await self.db.commit()
            logger.warning("Ingestion rejected: dataset inactive %s", request.dataset_key)
            raise DatasetInactiveError(f"Dataset {request.dataset_key} is inactive")

        if expected_market is not None:
            dataset_market = _normalize_market(dataset.market)
            requested_market = _normalize_market(expected_market)
            if dataset_market != requested_market:
                logger.warning(
                    (
                        "Ingestion rejected: dataset market mismatch "
                        "dataset=%s dataset_market=%s requested_market=%s"
                    ),
                    request.dataset_key,
                    dataset_market,
                    requested_market,
                )
                raise MarketMismatchError(
                    (
                        f"Dataset {request.dataset_key} belongs to market "
                        f"{dataset_market}, not {requested_market}"
                    )
                )

        # Check for duplicate request
        existing_raw = await self.get_raw_payload_by_idempotency_key(request.idempotency_key)
        if existing_raw:
            existing_run = await self.db.get(IngestionRun, existing_raw.run_id)
            status = existing_run.status if existing_run else "unknown"
            logger.info(
                "Duplicate idempotency_key %s, returning existing run %s",
                request.idempotency_key,
                existing_raw.run_id,
            )
            return existing_raw.run_id, status, True

        # Validate payload schema
        try:
            data_items = self.validate_payload_schema(request.payload, dataset)
        except PayloadValidationError as exc:
            run = await self.create_ingestion_run(
                request.dataset_key,
                source=request.source,
                request_key=request.request_key,
                raw_records=0,
                metadata={
                    "source": request.source,
                    "request_key": request.request_key,
                    "raw_records": 0,
                },
            )
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = utc_now()
            await self.db.commit()
            logger.warning("Ingestion rejected: payload invalid %s", exc)
            raise
        raw_records = len(data_items)

        metadata = {
            "source": request.source,
            "request_key": request.request_key,
            "raw_records": raw_records,
        }

        # Create ingestion run
        run = await self.create_ingestion_run(
            request.dataset_key,
            source=request.source,
            request_key=request.request_key,
            raw_records=raw_records,
            metadata=metadata,
        )

        try:
            # Store raw payload
            await self.store_raw_payload(request, run.run_id)

            # Commit transaction
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing_raw = await self.get_raw_payload_by_idempotency_key(request.idempotency_key)
            if existing_raw:
                existing_run = await self.db.get(IngestionRun, existing_raw.run_id)
                status = existing_run.status if existing_run else "unknown"
                logger.info(
                    "Idempotency conflict on commit for %s, returning existing run %s",
                    request.idempotency_key,
                    existing_raw.run_id,
                )
                return existing_raw.run_id, status, True
            raise

        logger.info(
            "Ingestion run created: run_id=%s dataset=%s source=%s raw_records=%s request_key=%s",
            run.run_id,
            request.dataset_key,
            request.source,
            raw_records,
            request.request_key,
        )

        # TODO: Trigger normalization (sync or async)
        # For now, we'll leave status as "pending"

        return run.run_id, run.status, False

    async def ingest_v2(
        self,
        request: IngestRequestV2,
        expected_market: str | None = None,
    ) -> tuple[UUID, str, bool]:
        """
        Process an ingestion request for api/v2/source.
        message_id serves as the idempotency key (stored in RawMarketPayload.idempotency_key).

        Returns:
            Tuple of (run_id, status, is_duplicate)
        """
        # Validate dataset exists and active
        dataset = await self.get_dataset(request.dataset_key, include_inactive=True)
        if not dataset:
            logger.warning("Ingestion rejected: dataset not found %s", request.dataset_key)
            raise DatasetNotFoundError(f"Dataset {request.dataset_key} not found")
        if not dataset.is_active:
            run = await self.create_ingestion_run_v2(
                request.dataset_key,
                source=request.source,
                request_key=request.request_key,
                message_id=request.message_id,
                raw_records=0,
                metadata={"source": request.source, "request_key": request.request_key},
                status="failed",
            )
            run.error_message = f"Dataset {request.dataset_key} is inactive"
            run.completed_at = utc_now()
            await self.db.commit()
            logger.warning("Ingestion rejected: dataset inactive %s", request.dataset_key)
            raise DatasetInactiveError(f"Dataset {request.dataset_key} is inactive")

        if expected_market is not None:
            dataset_market = _normalize_market(dataset.market)
            requested_market = _normalize_market(expected_market)
            if dataset_market != requested_market:
                logger.warning(
                    (
                        "Ingestion rejected: dataset market mismatch "
                        "dataset=%s dataset_market=%s requested_market=%s"
                    ),
                    request.dataset_key,
                    dataset_market,
                    requested_market,
                )
                raise MarketMismatchError(
                    (
                        f"Dataset {request.dataset_key} belongs to market "
                        f"{dataset_market}, not {requested_market}"
                    )
                )

        # Check for duplicate request via message_id (stored as idempotency_key)
        existing_raw = await self.get_raw_payload_by_message_id(request.message_id)
        if existing_raw:
            existing_run = await self.db.get(IngestionRun, existing_raw.run_id)
            status = existing_run.status if existing_run else "unknown"
            logger.info(
                "Duplicate message_id %s, returning existing run %s",
                request.message_id,
                existing_raw.run_id,
            )
            return existing_raw.run_id, status, True

        # Validate payload schema
        try:
            data_items = self.validate_payload_schema(request.payload, dataset)
        except PayloadValidationError as exc:
            run = await self.create_ingestion_run_v2(
                request.dataset_key,
                source=request.source,
                request_key=request.request_key,
                message_id=request.message_id,
                raw_records=0,
                metadata={"source": request.source, "request_key": request.request_key},
                status="failed",
            )
            run.error_message = str(exc)
            run.completed_at = utc_now()
            await self.db.commit()
            logger.warning("Ingestion rejected: payload invalid %s", exc)
            raise
        raw_records = len(data_items)

        # Create ingestion run — worker owns the full lifecycle, starts as "processing"
        run = await self.create_ingestion_run_v2(
            request.dataset_key,
            source=request.source,
            request_key=request.request_key,
            message_id=request.message_id,
            raw_records=raw_records,
            metadata={
                "source": request.source,
                "request_key": request.request_key,
                "raw_records": raw_records,
            },
            status="processing",
        )

        try:
            # Store raw payload; message_id is written as idempotency_key (PK)
            await self.store_raw_payload_v2(request, run.run_id)

            # Commit run + raw payload before normalization begins
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing_raw = await self.get_raw_payload_by_message_id(request.message_id)
            if existing_raw:
                existing_run = await self.db.get(IngestionRun, existing_raw.run_id)
                status = existing_run.status if existing_run else "unknown"
                logger.info(
                    "Idempotency conflict on commit for message_id=%s, returning existing run %s",
                    request.message_id,
                    existing_raw.run_id,
                )
                return existing_raw.run_id, status, True
            raise

        logger.info(
            "Ingestion run created: run_id=%s dataset=%s source=%s raw_records=%s request_key=%s",
            run.run_id,
            request.dataset_key,
            request.source,
            raw_records,
            request.request_key,
        )
        await trigger_normalization_v2(
            request.dataset_key, request.payload, run.run_id, session=self.db
        )

        return run.run_id, run.status, False

    async def get_run_status(self, run_id: UUID) -> Optional[IngestionRun]:
        """Get ingestion run status."""
        stmt = select(IngestionRun).where(IngestionRun.run_id == run_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_datasets(self) -> list[DatasetRegistry]:
        """List all active datasets."""
        stmt = select(DatasetRegistry).where(DatasetRegistry.is_active.is_(True))
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
