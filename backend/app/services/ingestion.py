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
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    DQIssue,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
)
from app.schemas.ingress import IngressRequestV1
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
    validate_ingress_request,
    validate_request_currency,
)
from app.services.normalize import (
    BaseNormalizer,
    MarketEODContractNormalizer,
    MarketMinuteContractNormalizer,
)
from app.utils import utc_now, uuid7
from app.utils.datetime_utils import ensure_utc

settings = get_settings()
logger = logging.getLogger(__name__)


class NormalizerFactory(Protocol):
    def __call__(
        self,
        db: AsyncSession,
        dataset_config: dict | None = None,
    ) -> BaseNormalizer: ...


CONTRACT_NORMALIZER_MAP: dict[tuple[str, int], NormalizerFactory] = {
    ("market_eod", 1): MarketEODContractNormalizer,
    ("market_minute", 1): MarketMinuteContractNormalizer,
}


def _minute_sequence_identity(payload: Any) -> dict[str, Any]:
    """Extract typed minute sequence identity without touching raw payload JSON.

    Contract requests expose a validated ``MarketMinuteBatch`` object while
    reruns pass a retained JSON document.  This small adapter keeps identity
    population bounded and leaves every non-minute/legacy path NULL.
    """
    batch = getattr(payload, "batch", None)
    if batch is None and isinstance(payload, dict):
        batch = payload.get("batch")
    mode = getattr(batch, "delivery_mode", None)
    if mode is None and isinstance(batch, dict):
        mode = batch.get("delivery_mode")
    mode = getattr(mode, "value", mode)
    if mode != "sequenced_snapshot":
        return {}

    def value(name: str) -> Any:
        item = getattr(batch, name, None)
        if item is None and isinstance(batch, dict):
            item = batch.get(name)
        return item

    identity = {
        "snapshot_id": value("snapshot_id"),
        "daily_update_id": value("daily_update_id"),
        "sequence": value("sequence"),
        "sequence_count": value("sequence_count"),
    }
    if any(item is None for item in identity.values()):
        return {}
    return identity


def _rerun_batch_metadata(
    payload: Any,
    *,
    dataset_key: str,
    schema_id: str | None = None,
) -> dict[str, Any]:
    """Extract bounded batch lineage from a retained raw contract payload.

    Reruns do not pass through the request Pydantic model again.  Treat raw
    metadata as untrusted: malformed values return an empty projection so the
    legacy all-NULL run shape remains DB-compatible instead of creating a
    partial sequenced identity.
    """
    if not isinstance(payload, dict):
        return {}
    batch = payload.get("batch")
    if not isinstance(batch, dict):
        return {}

    mode = batch.get("delivery_mode")
    if not isinstance(mode, str):
        return {}
    mode = mode.strip()
    data_date = batch.get("data_date")
    if isinstance(data_date, date):
        parsed_date = data_date
    elif isinstance(data_date, str) and len(data_date) == 10:
        try:
            parsed_date = date.fromisoformat(data_date)
        except ValueError:
            return {}
    else:
        return {}

    if mode in {"full_snapshot", "incremental", "backfill"}:
        return {"batch_data_date": parsed_date, "delivery_mode": mode}
    if mode != "sequenced_snapshot" or not (
        schema_id == "market_minute" or dataset_key in {"tw_equity_minute", "tw_etf_minute"}
    ):
        return {}

    def bounded_identifier(name: str) -> str | None:
        value = batch.get(name)
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value if 1 <= len(value) <= 100 else None

    def bounded_sequence(name: str) -> int | None:
        value = batch.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 99_999:
            return None
        return value

    snapshot_id = bounded_identifier("snapshot_id")
    daily_update_id = bounded_identifier("daily_update_id")
    sequence = bounded_sequence("sequence")
    sequence_count = bounded_sequence("sequence_count")
    if (
        snapshot_id is None
        or daily_update_id is None
        or sequence is None
        or sequence_count is None
        or sequence > sequence_count
    ):
        return {}
    return {
        "batch_data_date": parsed_date,
        "delivery_mode": mode,
        "snapshot_id": snapshot_id,
        "daily_update_id": daily_update_id,
        "sequence": sequence,
        "sequence_count": sequence_count,
    }


def _select_normalizer_for_payload(
    dataset_key: str,
    payload: dict,
    schema_id: str | None = None,
    schema_version: int | None = None,
) -> Optional[NormalizerFactory]:
    """Resolve only an explicitly declared, supported contract normalizer.

    ``dataset_key`` and the retained JSON payload are deliberately not used to
    guess a legacy provider format.  Missing/unknown schema metadata fails
    closed in both the worker and rerun paths.
    """
    if not schema_id or schema_version is None:
        return None
    return CONTRACT_NORMALIZER_MAP.get((schema_id, schema_version))


class DatasetNotFoundError(ValueError):
    """Raised when a dataset key is not found."""


class DatasetInactiveError(ValueError):
    """Raised when a dataset is inactive."""


class PayloadValidationError(ValueError):
    """Raised when payload schema is invalid."""


class RawPayloadNotFoundError(ValueError):
    """Raised when raw payload for a run is not found."""


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
    client_scope = (
        f"uuid:{source_client_id}" if source_client_id is not None else "unattributed:null"
    )
    scope = ":".join(("canonical-idempotency", client_scope, dataset_key, idempotency_key))
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": scope},
    )


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
        snapshot_id: str | None = None,
        daily_update_id: str | None = None,
        sequence: int | None = None,
        sequence_count: int | None = None,
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
            snapshot_id=snapshot_id,
            daily_update_id=daily_update_id,
            sequence=sequence,
            sequence_count=sequence_count,
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
        request: IngressRequestV1,
        run_id: UUID,
        *,
        schema_id: str | None = None,
        schema_version: int | None = None,
    ) -> RawMarketPayload:
        """Store raw payload in the database."""
        fetched_at = ensure_utc(request.fetched_at)
        payload = request.payload.model_dump(mode="json")
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
            payload_sha256=payload_sha256(payload),
            payload=payload,
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
        if (
            self.bound_source is not None
            and raw_payload.source.strip().lower() != self.bound_source
        ):
            raise SourceIdentityMismatchError(
                f"Credential is bound to source {self.bound_source}, not {raw_payload.source}"
            )
        if not self.allowed_datasets or raw_payload.dataset_key not in self.allowed_datasets:
            raise DatasetAccessDeniedError(
                f"Source client is not allowed to rerun dataset {raw_payload.dataset_key}"
            )

        dataset = await self.get_dataset(raw_payload.dataset_key, include_inactive=True)
        if not dataset:
            raise DatasetNotFoundError(f"Dataset {raw_payload.dataset_key} not found")
        if not dataset.is_active:
            raise DatasetInactiveError(f"Dataset {raw_payload.dataset_key} is inactive")

        if not isinstance(raw_payload.schema_id, str) or not isinstance(
            raw_payload.schema_version, int
        ):
            raise PayloadValidationError(
                "Rerun is supported only for retained versioned contract payloads"
            )
        try:
            retained_request = validate_ingress_request(
                {
                    "dataset_key": raw_payload.dataset_key,
                    "schema_id": raw_payload.schema_id,
                    "schema_version": raw_payload.schema_version,
                    "source": raw_payload.source,
                    "request_key": raw_payload.request_key,
                    "idempotency_key": raw_payload.idempotency_key,
                    "fetched_at": raw_payload.fetched_at,
                    "payload": raw_payload.payload,
                }
            )
        except Exception as exc:
            raise PayloadValidationError(
                f"Retained contract payload is invalid; rerun refused: {exc}"
            ) from exc

        try:
            declaration = parse_dataset_contract_declaration(dataset.config)
            if declaration is None:
                raise ValueError("missing contract declaration")
            validate_dataset_contract_scope(
                declaration,
                market=dataset.market,
                asset_class=dataset.asset_class,
            )
            if (
                declaration.schema_id != retained_request.schema_id
                or retained_request.schema_version not in declaration.accepted_schema_versions
            ):
                raise ValueError("retained schema is not accepted by the dataset")
            if raw_payload.source.strip().lower() not in declaration.provider_scope():
                raise ValueError("retained source is not allowed for the dataset")
        except ValueError as exc:
            raise PayloadValidationError(f"Retained contract scope is invalid: {exc}") from exc

        raw_records = len(retained_request.payload.data)
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
            **_rerun_batch_metadata(
                retained_request.payload.model_dump(mode="json"),
                dataset_key=raw_payload.dataset_key,
                schema_id=raw_payload.schema_id,
            ),
        )
        await self.create_normalization_job(run)
        await self.db.commit()

        return run.run_id, run.status, raw_payload.dataset_key, raw_payload.payload

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
        # A NULL allowlist is a historical provider-wide scope.  Contract
        # ingestion fails closed until an operator explicitly scopes it to the
        # four supported datasets.
        if not self.allowed_datasets or request.dataset_key not in self.allowed_datasets:
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
        allowed_sources = declaration.provider_scope()
        if not allowed_sources or request.source.strip().lower() not in allowed_sources:
            raise DatasetAccessDeniedError(
                f"Source {request.source} is not allowed for dataset {request.dataset_key}"
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
        if request.delivery is not None:
            metadata["delivery"] = request.delivery.model_dump(mode="json", exclude_none=True)
        try:
            run_id = uuid7()
            raw_payload = await self.store_raw_payload(
                request,
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
                delivery_mode=getattr(
                    request.payload.batch.delivery_mode,
                    "value",
                    request.payload.batch.delivery_mode,
                ),
                **_minute_sequence_identity(request.payload),
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
            if (
                getattr(
                    request.payload.batch.delivery_mode,
                    "value",
                    request.payload.batch.delivery_mode,
                )
                == "full_snapshot"
            ):
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
        """List active datasets within the authenticated exact credential scope."""
        if not isinstance(self.allowed_datasets, list) or not self.allowed_datasets:
            # A NULL/invalid historical scope is provider-wide ambiguity, not
            # permission to enumerate every active registry row.
            return []
        stmt = select(DatasetRegistry).where(DatasetRegistry.is_active.is_(True))
        stmt = stmt.where(DatasetRegistry.dataset_key.in_(self.allowed_datasets))
        result = await self.db.execute(stmt)
        return list(result.scalars().all())
