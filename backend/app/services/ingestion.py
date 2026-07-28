"""
Data ingestion service.
Handles raw data storage and triggers normalization.
"""

import hashlib
import json
import logging
from datetime import date, timedelta
from typing import Any, Optional, Protocol
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.base import async_session_maker
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    DQIssue,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
)
from app.schemas.ingress import IngressRequestV1
from app.schemas.source import IngestRequest, ensure_data_items_count_within_limit
from app.services.delivery_monitor import resolve_missing_delivery_for_run
from app.services.delivery_policy import (
    DeliveryPolicyRejectedError,
    evaluate_delivery_policy,
    lock_delivery_policy_scope,
)
from app.services.ingestion_attempts import IngestionAttemptService
from app.services.ingress_contracts import (
    parse_dataset_contract_declaration,
    validate_dataset_contract_scope,
    validate_request_currency,
)
from app.services.normalize import (
    BaseNormalizer,
    CNEquityNormalizer,
    CNIndexNormalizer,
    CorporateActionNormalizer,
    CryptoBloombergNormalizer,
    CryptoIndexNormalizer,
    CryptoNormalizer,
    EquityNormalizer,
    FuturesContinuousEODContractNormalizer,
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
    MarketEODContractNormalizer,
    TWEquityNormalizer,
    TWETFFinlabNormalizer,
    TWIndexNormalizer,
    TWStockFinlabNormalizer,
    USIndexNormalizer,
    USStockNormalizer,
    WTXBloombergNormalizer,
    WTXFinlabNormalizer,
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
    "tw_equity_eod": TWStockFinlabNormalizer,
    "tw_etf_eod": TWETFFinlabNormalizer,
    "tw_equity_bloomberg_eod": TWEquityNormalizer,
    "hk_equity_eod": HKEquityNormalizer,
    "cn_equity_eod": CNEquityNormalizer,
    "tw_index_eod": TWIndexNormalizer,
    "hk_index_eod": HKIndexNormalizer,
    "cn_index_eod": CNIndexNormalizer,
    "hkchina_index_eod": HKChinaIndexNormalizer,
    # Bloomberg direct format — other markets
    "fx_bloomberg_eod": FXBloombergNormalizer,
    "crypto_bloomberg_eod": CryptoBloombergNormalizer,
    # WTX 期貨：依 payload metadata.source 選擇 FinLab 或 Bloomberg normalizer。
    "wtx_eod": WTXFinlabNormalizer,
    "macro_bloomberg_observation": MacroBloombergNormalizer,
}


_WTX_SOURCE_NORMALIZERS: dict[str, NormalizerFactory] = {
    "finlab": WTXFinlabNormalizer,
    "bloomberg": WTXBloombergNormalizer,
}

CONTRACT_NORMALIZER_MAP: dict[tuple[str, int], NormalizerFactory] = {
    ("market_eod", 1): MarketEODContractNormalizer,
    ("futures_continuous_eod", 1): FuturesContinuousEODContractNormalizer,
}


def _normalize_provider_key(value: Any) -> str | None:
    """Normalize provider labels used for payload-aware normalizer routing."""
    if not isinstance(value, str):
        return None
    source = value.strip().lower()
    if not source:
        return None
    if source.startswith("bloomberg"):
        return "bloomberg"
    return source


def _select_normalizer_for_payload(
    dataset_key: str,
    payload: dict,
    schema_id: str | None = None,
    schema_version: int | None = None,
) -> Optional[NormalizerFactory]:
    """Resolve normalizer by dataset_key, falling back to payload-aware routing."""
    if schema_id is not None:
        if schema_version is None:
            return None
        return CONTRACT_NORMALIZER_MAP.get((schema_id, schema_version))
    if dataset_key == "wtx_eod":
        source = _get_nested_value(payload, "metadata.source")
        provider_key = _normalize_provider_key(source)
        if provider_key is not None:
            override = _WTX_SOURCE_NORMALIZERS.get(provider_key)
            if override is not None:
                return override
    return NORMALIZER_MAP.get(dataset_key)


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


class IdempotencyPayloadMismatchError(ValueError):
    """Raised when an idempotency key is reused for different content."""


class SourceIdentityMismatchError(ValueError):
    """Raised when a credential-bound source does not match the request."""


class DatasetAccessDeniedError(ValueError):
    """Raised when a source client cannot ingest the requested dataset."""


class DatasetContractNotConfiguredError(ValueError):
    """Raised when a dataset has no versioned ingress declaration."""


class IngressSchemaNotAllowedError(ValueError):
    """Raised when a dataset does not accept the requested contract."""


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


def payload_sha256(payload: dict) -> str:
    """Return a stable digest for idempotency content comparison."""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def lock_ingestion_idempotency_scope(
    db: AsyncSession,
    *,
    source_client_id: UUID | None,
    dataset_key: str,
    idempotency_key: str,
) -> None:
    """Lock the exact PostgreSQL unique scope before canonical policy locks."""
    client_scope = f"uuid:{source_client_id}" if source_client_id is not None else "legacy:null"
    scope = ":".join(("canonical-idempotency", client_scope, dataset_key, idempotency_key))
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": scope},
    )


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
    normalizer_cls = _select_normalizer_for_payload(dataset_key, payload)
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


class IngestionService:
    """Service for handling data ingestion."""

    def __init__(self, db: AsyncSession):
        self.db = db
        session_info = db.info if isinstance(db.info, dict) else {}
        self.ownership_enforced = "source_client_id" in session_info
        self.source_client_id: UUID | None = session_info.get("source_client_id")
        self.bound_source: str | None = session_info.get("source_name")
        self.allowed_datasets: list[str] | None = session_info.get("allowed_datasets")

    async def get_raw_payload_by_idempotency_key(
        self,
        idempotency_key: str,
        dataset_key: str | None = None,
    ) -> Optional[RawMarketPayload]:
        """Get raw payload in the authenticated provider idempotency scope."""
        stmt = select(RawMarketPayload).where(RawMarketPayload.idempotency_key == idempotency_key)
        if self.source_client_id is None:
            stmt = stmt.where(RawMarketPayload.source_client_id.is_(None))
        else:
            stmt = stmt.where(RawMarketPayload.source_client_id == self.source_client_id)
        if dataset_key is not None:
            stmt = stmt.where(RawMarketPayload.dataset_key == dataset_key)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _accept_existing_contract_delivery(
        self,
        existing_raw: RawMarketPayload,
        request: IngressRequestV1,
        request_payload_sha256: str,
        attempt_id: UUID,
    ) -> tuple[UUID, str, bool]:
        """Validate and return one exact idempotent canonical delivery."""
        existing_digest = existing_raw.payload_sha256 or payload_sha256(existing_raw.payload)
        if (
            existing_digest != request_payload_sha256
            or existing_raw.source != request.source
            or existing_raw.schema_id != request.schema_id
            or existing_raw.schema_version != request.schema_version
        ):
            raise IdempotencyPayloadMismatchError(
                "idempotency_key is already associated with different contract content"
            )
        existing_run = await self.db.get(IngestionRun, existing_raw.run_id)
        status = existing_run.status if existing_run else "unknown"
        await IngestionAttemptService(self.db).mark_accepted(
            attempt_id,
            existing_raw.run_id,
            duplicate=True,
        )
        await self.db.commit()
        return existing_raw.run_id, status, True

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
        try:
            ensure_data_items_count_within_limit(len(data_items))
        except ValueError as exc:
            raise PayloadValidationError(str(exc)) from exc

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
        run_id: UUID | None = None,
        raw_payload_id: UUID | None = None,
        status: str = "queued",
        schema_id: str | None = None,
        schema_version: int | None = None,
        batch_data_date: date | None = None,
        delivery_mode: str | None = None,
        policy_outcome: str | None = None,
        policy_details: dict | None = None,
        is_rerun: bool = False,
    ) -> IngestionRun:
        """Create a new ingestion run record."""
        run = IngestionRun(
            run_id=run_id or uuid7(),
            dataset_key=dataset_key,
            source=source,
            source_client_id=self.source_client_id,
            raw_payload_id=raw_payload_id,
            request_key=request_key,
            schema_id=schema_id,
            schema_version=schema_version,
            batch_data_date=batch_data_date,
            delivery_mode=delivery_mode,
            policy_outcome=policy_outcome,
            policy_details=policy_details,
            is_rerun=is_rerun,
            raw_records=raw_records,
            status=status,
            max_attempts=settings.NORMALIZATION_MAX_ATTEMPTS,
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
        *,
        schema_id: str | None = None,
        schema_version: int | None = None,
    ) -> RawMarketPayload:
        """Store raw payload in the database."""
        fetched_at = ensure_utc(request.fetched_at)
        accepted_at = utc_now()
        expire_at = accepted_at + timedelta(days=settings.RAW_RETENTION_DAYS)

        raw_payload = RawMarketPayload(
            raw_payload_id=uuid7(),
            source_client_id=self.source_client_id,
            dataset_key=request.dataset_key,
            source=request.source,
            request_key=request.request_key,
            idempotency_key=request.idempotency_key,
            schema_id=schema_id,
            schema_version=schema_version,
            payload_sha256=payload_sha256(request.payload),
            payload=request.payload,
            fetched_at=fetched_at,
            expire_at=expire_at,
            run_id=run_id,
            created_at=accepted_at,
        )

        self.db.add(raw_payload)
        await self.db.flush()
        return raw_payload

    async def get_raw_payload_by_run(self, run_id: UUID) -> Optional[RawMarketPayload]:
        """Get raw payload by ingestion run id."""
        run = await self.db.get(IngestionRun, run_id)
        if run and self.ownership_enforced and run.source_client_id != self.source_client_id:
            return None
        if run and run.raw_payload_id:
            raw = await self.db.get(RawMarketPayload, run.raw_payload_id)
            if raw is not None:
                return raw
        stmt = select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
        if self.ownership_enforced:
            if self.source_client_id is None:
                stmt = stmt.where(RawMarketPayload.source_client_id.is_(None))
            else:
                stmt = stmt.where(RawMarketPayload.source_client_id == self.source_client_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def create_normalization_job(
        self,
        run: IngestionRun,
        *,
        available_at=None,
    ) -> NormalizationJob:
        """Create a durable job and its transactional outbox event."""
        now = utc_now()
        delivery_id = uuid7()
        job = NormalizationJob(
            job_id=uuid7(),
            run_id=run.run_id,
            dataset_key=run.dataset_key,
            status="queued",
            delivery_id=delivery_id,
            attempt_count=0,
            max_attempts=settings.NORMALIZATION_MAX_ATTEMPTS,
            available_at=available_at or now,
            created_at=now,
            updated_at=now,
        )
        self.db.add(job)
        await self.db.flush()
        self.db.add(
            NormalizationOutbox(
                outbox_id=uuid7(),
                job_id=job.job_id,
                run_id=run.run_id,
                delivery_id=delivery_id,
                event_type="normalize_run",
                status="pending",
                available_at=available_at or now,
                publish_attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
        await self.db.flush()
        return job

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
            "schema_id": raw_payload.schema_id,
            "schema_version": raw_payload.schema_version,
            "rerun_from_run_id": str(run_id),
            "rerun_from_idempotency_key": raw_payload.idempotency_key,
        }

        run = await self.create_ingestion_run(
            raw_payload.dataset_key,
            source=raw_payload.source,
            request_key=raw_payload.request_key,
            raw_records=raw_records,
            metadata=metadata,
            raw_payload_id=raw_payload.raw_payload_id,
            schema_id=raw_payload.schema_id,
            schema_version=raw_payload.schema_version,
            is_rerun=True,
        )
        await self.create_normalization_job(run)
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
        if self.bound_source is not None and request.source.strip().lower() != self.bound_source:
            raise SourceIdentityMismatchError(
                f"Credential is bound to source {self.bound_source}, not {request.source}"
            )
        if self.allowed_datasets is not None and request.dataset_key not in self.allowed_datasets:
            raise DatasetNotFoundError(
                f"Dataset {request.dataset_key} is not allowed for this source client"
            )

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
                status="failed",
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
        request_payload_sha256 = payload_sha256(request.payload)
        existing_raw = await self.get_raw_payload_by_idempotency_key(
            request.idempotency_key,
            request.dataset_key,
        )
        if existing_raw:
            existing_digest = existing_raw.payload_sha256 or payload_sha256(existing_raw.payload)
            if existing_digest != request_payload_sha256:
                raise IdempotencyPayloadMismatchError(
                    "idempotency_key is already associated with a different payload"
                )
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
                status="failed",
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

        try:
            run_id = uuid7()
            raw_payload = await self.store_raw_payload(request, run_id)
            run = await self.create_ingestion_run(
                request.dataset_key,
                source=request.source,
                request_key=request.request_key,
                raw_records=raw_records,
                metadata=metadata,
                run_id=run_id,
                raw_payload_id=raw_payload.raw_payload_id,
            )
            await self.create_normalization_job(run)
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing_raw = await self.get_raw_payload_by_idempotency_key(
                request.idempotency_key,
                request.dataset_key,
            )
            if existing_raw:
                existing_digest = existing_raw.payload_sha256 or payload_sha256(
                    existing_raw.payload
                )
                if existing_digest != request_payload_sha256:
                    raise IdempotencyPayloadMismatchError(
                        "idempotency_key is already associated with a different payload"
                    )
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

        return run.run_id, run.status, False

    async def ingest_contract(
        self,
        request: IngressRequestV1,
        attempt_id: UUID,
    ) -> tuple[UUID, str, bool]:
        """Persist a validated canonical contract and enqueue schema-based normalization."""
        if self.bound_source is not None and request.source != self.bound_source:
            raise SourceIdentityMismatchError(
                f"Credential is bound to source {self.bound_source}, not {request.source}"
            )
        if self.allowed_datasets is not None and request.dataset_key not in self.allowed_datasets:
            raise DatasetAccessDeniedError(
                f"Source client is not allowed to ingest dataset {request.dataset_key}"
            )

        dataset = await self.get_dataset(request.dataset_key, include_inactive=True)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {request.dataset_key} not found")
        if not dataset.is_active:
            raise DatasetInactiveError(f"Dataset {request.dataset_key} is inactive")

        try:
            declaration = parse_dataset_contract_declaration(dataset.config)
            if declaration is not None:
                validate_dataset_contract_scope(
                    declaration,
                    market=dataset.market,
                    asset_class=dataset.asset_class,
                )
        except ValueError as exc:
            raise DatasetContractNotConfiguredError(
                f"Dataset {request.dataset_key} has an invalid ingress contract declaration: {exc}"
            ) from exc
        if declaration is None:
            raise DatasetContractNotConfiguredError(
                f"Dataset {request.dataset_key} has no ingress contract declaration"
            )
        if (
            declaration.schema_id != request.schema_id
            or request.schema_version not in declaration.accepted_schema_versions
        ):
            raise IngressSchemaNotAllowedError(
                f"Dataset {request.dataset_key} does not accept "
                f"{request.schema_id}.v{request.schema_version}"
            )

        validate_request_currency(declaration, request)

        canonical_payload = request.payload.model_dump(mode="json")
        request_payload_sha256 = payload_sha256(canonical_payload)
        existing_raw = await self.get_raw_payload_by_idempotency_key(
            request.idempotency_key,
            request.dataset_key,
        )
        if existing_raw is not None:
            return await self._accept_existing_contract_delivery(
                existing_raw,
                request,
                request_payload_sha256,
                attempt_id,
            )

        # Fixed global lock ordering: the exact DB unique scope is acquired and
        # rechecked before the broader delivery-policy scope. Both transaction
        # locks remain held until accept/reject commit, so no third recheck is
        # needed after the policy lock. The evaluator never acquires locks.
        await lock_ingestion_idempotency_scope(
            self.db,
            source_client_id=self.source_client_id,
            dataset_key=request.dataset_key,
            idempotency_key=request.idempotency_key,
        )
        existing_raw = await self.get_raw_payload_by_idempotency_key(
            request.idempotency_key,
            request.dataset_key,
        )
        if existing_raw is not None:
            return await self._accept_existing_contract_delivery(
                existing_raw,
                request,
                request_payload_sha256,
                attempt_id,
            )

        await lock_delivery_policy_scope(self.db, request)

        policy_result = await evaluate_delivery_policy(
            self.db,
            request,
            declaration.delivery_expectation,
        )
        policy_details = policy_result.bounded_details()
        if policy_result.outcome == "reject":
            raise DeliveryPolicyRejectedError(policy_result)

        raw_records = len(request.payload.data)
        metadata = {
            "source": request.source,
            "request_key": request.request_key,
            "raw_records": raw_records,
            "schema_id": request.schema_id,
            "schema_version": request.schema_version,
            "delivery_policy": policy_details,
        }
        legacy_request = IngestRequest(
            dataset_key=request.dataset_key,
            source=request.source,
            request_key=request.request_key,
            idempotency_key=request.idempotency_key,
            payload=canonical_payload,
            fetched_at=request.fetched_at,
        )

        try:
            run_id = uuid7()
            raw_payload = await self.store_raw_payload(
                legacy_request,
                run_id,
                schema_id=request.schema_id,
                schema_version=request.schema_version,
            )
            run = await self.create_ingestion_run(
                request.dataset_key,
                source=request.source,
                request_key=request.request_key,
                raw_records=raw_records,
                metadata=metadata,
                run_id=run_id,
                raw_payload_id=raw_payload.raw_payload_id,
                schema_id=request.schema_id,
                schema_version=request.schema_version,
                batch_data_date=request.payload.batch.data_date,
                delivery_mode=request.payload.batch.delivery_mode.value,
                policy_outcome=policy_result.outcome,
                policy_details=policy_details,
            )
            if policy_result.outcome == "warn":
                self.db.add(
                    DQIssue(
                        id=uuid7(),
                        run_id=run.run_id,
                        issue_type="INGRESS_DELIVERY_POLICY_WARNING",
                        severity="warning",
                        description=("Canonical delivery accepted with aggregate policy warnings"),
                        raw_data=policy_details,
                        resolved=False,
                        created_at=utc_now(),
                    )
                )
            await self.create_normalization_job(run)
            if request.payload.batch.delivery_mode.value == "full_snapshot":
                await resolve_missing_delivery_for_run(
                    self.db,
                    dataset_key=request.dataset_key,
                    source=request.source,
                    schema_id=request.schema_id,
                    schema_version=request.schema_version,
                    data_date=request.payload.batch.data_date,
                )
            await IngestionAttemptService(self.db).mark_accepted(
                attempt_id,
                run_id,
                details=policy_details if policy_result.outcome == "warn" else None,
            )
            await self.db.commit()
        except IntegrityError:
            await self.db.rollback()
            existing_raw = await self.get_raw_payload_by_idempotency_key(
                request.idempotency_key,
                request.dataset_key,
            )
            if existing_raw is None:
                raise
            return await self._accept_existing_contract_delivery(
                existing_raw,
                request,
                request_payload_sha256,
                attempt_id,
            )

        logger.info(
            "Canonical ingestion accepted: attempt_id=%s run_id=%s dataset=%s contract=%s.v%s",
            attempt_id,
            run.run_id,
            request.dataset_key,
            request.schema_id,
            request.schema_version,
        )
        return run.run_id, run.status, False

    async def get_run_status(self, run_id: UUID) -> Optional[IngestionRun]:
        """Get ingestion run status."""
        stmt = select(IngestionRun).where(IngestionRun.run_id == run_id)
        if self.ownership_enforced:
            if self.source_client_id is None:
                stmt = stmt.where(IngestionRun.source_client_id.is_(None))
            else:
                stmt = stmt.where(IngestionRun.source_client_id == self.source_client_id)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_datasets(self) -> list[DatasetRegistry]:
        """List all active datasets."""
        stmt = select(DatasetRegistry).where(DatasetRegistry.is_active.is_(True))
        if self.allowed_datasets is not None:
            stmt = stmt.where(DatasetRegistry.dataset_key.in_(self.allowed_datasets))
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
