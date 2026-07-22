"""Application service for the versioned canonical ingest endpoint."""

import json
import logging
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ingress import IngressRequestV1
from app.services.ingestion import (
    DatasetAccessDeniedError,
    DatasetContractNotConfiguredError,
    DatasetInactiveError,
    DatasetNotFoundError,
    IdempotencyPayloadMismatchError,
    IngestionService,
    IngressSchemaNotAllowedError,
    SourceIdentityMismatchError,
)
from app.services.ingestion_attempts import (
    IngestionAttemptClaimError,
    IngestionAttemptService,
)
from app.services.ingress_contracts import (
    UnsupportedIngressContractError,
    validate_ingress_request,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanonicalIngestAccepted:
    """Successful canonical ingest application result."""

    attempt_id: UUID
    run_id: UUID
    run_status: str
    is_duplicate: bool
    request: IngressRequestV1


class CanonicalIngestRejectionError(RuntimeError):
    """Stable HTTP-facing rejection produced after attempt persistence."""

    def __init__(
        self,
        *,
        attempt_id: UUID,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.attempt_id = attempt_id
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def _validation_message(exc: ValidationError) -> str:
    errors = exc.errors(include_input=False)
    if not errors:
        return "Ingress request does not match the declared schema"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "request"
    message = str(first.get("msg") or "invalid value")
    suffix = f" ({len(errors)} validation errors)" if len(errors) > 1 else ""
    return f"{location}: {message}{suffix}"[:1_000]


async def _reject(
    service: IngestionAttemptService,
    attempt_id: UUID,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> NoReturn:
    try:
        await service.reject(
            attempt_id,
            http_status=status_code,
            failure_code=code,
            error_message=message,
        )
    except Exception:
        logger.exception(
            "Failed to persist rejection for ingestion attempt %s",
            attempt_id,
        )
    raise CanonicalIngestRejectionError(
        attempt_id=attempt_id,
        status_code=status_code,
        code=code,
        message=message,
        headers=headers,
    )


async def accept_canonical_ingest(
    db: AsyncSession,
    raw_body: bytes,
) -> CanonicalIngestAccepted:
    """Validate, audit, persist, and enqueue one canonical request."""
    json_valid = True
    try:
        body: Any = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
        json_valid = False

    attempt_service = IngestionAttemptService(db)
    try:
        attempt = await attempt_service.begin(body, raw_body)
    except IngestionAttemptClaimError as exc:
        raise CanonicalIngestRejectionError(
            attempt_id=exc.attempt_id,
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            message="Ingestion is temporarily unavailable",
            headers={"Retry-After": "30"},
        ) from exc
    attempt_id = attempt.attempt_id

    if not json_valid:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code="INGRESS_SCHEMA_INVALID",
            message="Request body must be valid JSON",
        )
    if not isinstance(body, dict):
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code="INGRESS_SCHEMA_INVALID",
            message="Request body must be a JSON object",
        )

    try:
        contract_request = validate_ingress_request(body)
    except UnsupportedIngressContractError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code="INGRESS_SCHEMA_UNSUPPORTED",
            message=str(exc),
        )
    except ValidationError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code="INGRESS_SCHEMA_INVALID",
            message=_validation_message(exc),
        )

    ingestion_service = IngestionService(db)
    try:
        run_id, run_status, is_duplicate = await ingestion_service.ingest_contract(
            contract_request,
            attempt_id,
        )
    except DatasetAccessDeniedError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=403,
            code="DATASET_ACCESS_DENIED",
            message=str(exc),
        )
    except SourceIdentityMismatchError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=403,
            code="SOURCE_IDENTITY_MISMATCH",
            message=str(exc),
        )
    except DatasetNotFoundError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=400,
            code="DATASET_NOT_FOUND",
            message=str(exc),
        )
    except DatasetInactiveError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=409,
            code="DATASET_INACTIVE",
            message=str(exc),
        )
    except DatasetContractNotConfiguredError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=409,
            code="DATASET_CONTRACT_NOT_CONFIGURED",
            message=str(exc),
        )
    except IngressSchemaNotAllowedError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code="INGRESS_SCHEMA_NOT_ALLOWED",
            message=str(exc),
        )
    except IdempotencyPayloadMismatchError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=409,
            code="IDEMPOTENCY_PAYLOAD_MISMATCH",
            message=str(exc),
        )
    except (OperationalError, DBAPIError):
        try:
            await db.rollback()
        except Exception:
            logger.exception("Database rollback failed for ingestion attempt %s", attempt_id)
        message = "Ingestion is temporarily unavailable"
        await _reject(
            attempt_service,
            attempt_id,
            status_code=503,
            code="DATABASE_UNAVAILABLE",
            message=message,
            headers={"Retry-After": "30"},
        )
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Database rollback failed for ingestion attempt %s", attempt_id)
        logger.exception("Unexpected canonical ingestion failure attempt_id=%s", attempt_id)
        await _reject(
            attempt_service,
            attempt_id,
            status_code=500,
            code="INTERNAL_ERROR",
            message="Ingestion failed due to an internal server error",
        )

    return CanonicalIngestAccepted(
        attempt_id=attempt_id,
        run_id=run_id,
        run_status=run_status,
        is_duplicate=is_duplicate,
        request=contract_request,
    )
