"""Application service for the versioned canonical ingest endpoint."""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, OperationalError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ingress import IngressRequestV1
from app.services.delivery_policy import DeliveryPolicyRejectedError
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
    IngestionAttemptClaimInternalError,
    IngestionAttemptService,
)
from app.services.ingress_contracts import (
    CurrencyRequiredError,
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


_VALIDATION_ERROR_PRIORITY = (
    ("declared_record_count_mismatch", "DECLARED_RECORD_COUNT_MISMATCH"),
    ("duplicate_delivery_key", "DUPLICATE_DELIVERY_KEY"),
)


def _select_validation_error(
    errors: Sequence[Mapping[str, Any]],
) -> tuple[str, Mapping[str, Any] | None]:
    """Select one error deterministically so its code and message stay aligned."""
    for error_type, code in _VALIDATION_ERROR_PRIORITY:
        for error in errors:
            if error.get("type") == error_type:
                return code, error
    return "INGRESS_SCHEMA_INVALID", errors[0] if errors else None


def _validation_rejection(exc: ValidationError) -> tuple[str, str]:
    """Return the stable code/message pair for one selected structured error."""
    errors = exc.errors(include_input=False)
    code, selected = _select_validation_error(errors)
    if selected is None:
        return code, "Ingress request does not match the declared schema"
    location = ".".join(str(part) for part in selected.get("loc", ())) or "request"
    message = str(selected.get("msg") or "invalid value")
    suffix = f" ({len(errors)} validation errors)" if len(errors) > 1 else ""
    return code, f"{location}: {message}{suffix}"[:1_000]


async def _reject(
    service: IngestionAttemptService,
    attempt_id: UUID,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    details: dict[str, Any] | None = None,
) -> NoReturn:
    try:
        await service.reject(
            attempt_id,
            http_status=status_code,
            failure_code=code,
            error_message=message,
            failure_details=details,
        )
    except (DBAPIError, PendingRollbackError):
        logger.exception(
            "Database unavailable while persisting rejection for ingestion attempt %s",
            attempt_id,
        )
    except Exception as exc:
        logger.exception(
            "Unexpected rejection audit failure for ingestion attempt %s",
            attempt_id,
        )
        raise CanonicalIngestRejectionError(
            attempt_id=attempt_id,
            status_code=500,
            code="INTERNAL_ERROR",
            message="Ingestion failed due to an internal server error",
        ) from exc
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
    except IngestionAttemptClaimInternalError as exc:
        raise CanonicalIngestRejectionError(
            attempt_id=exc.attempt_id,
            status_code=500,
            code="INTERNAL_ERROR",
            message="Ingestion failed due to an internal server error",
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
        code, message = _validation_rejection(exc)
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code=code,
            message=message,
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
    except CurrencyRequiredError as exc:
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code="CURRENCY_REQUIRED",
            message=str(exc),
        )
    except DeliveryPolicyRejectedError as exc:
        code = exc.result.primary_code or "BATCH_RECORD_COUNT_DROP"
        await _reject(
            attempt_service,
            attempt_id,
            status_code=422,
            code=code,
            message="Delivery rejected by dataset completeness policy",
            details=exc.result.bounded_details(),
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
