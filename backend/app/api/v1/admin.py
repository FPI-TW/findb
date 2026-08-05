"""
Admin API 端點。
提供 canonical 層資料的人工修正與資料品質問題處理能力。
"""

from base64 import b64decode
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    AdminPrincipal,
    enforce_admin_login_rate_limit,
    get_admin_break_glass_api_key,
    require_operator,
    require_owner,
    require_viewer,
    verify_admin_api_key,
)
from app.config import get_settings
from app.dependencies import get_db
from app.models.canonical import CalendarImportBatch, CalendarMarket, CalendarYearRevision
from app.models.raw import RawMarketPayload
from app.models.registry import (
    AdminSession,
    AdminUser,
    APIKey,
    IngestionRun,
    SourceClient,
)
from app.schemas.admin import (
    AdminUserCreateRequest,
    AdminUserEnvelope,
    AdminUserListResponse,
    AdminUserResponse,
    AdminUserUpdateRequest,
    APIKeyCreateRequest,
    APIKeyCreateResponse,
    APIKeyListResponse,
    APIKeyResponse,
    BootstrapRequest,
    BulkRerunResponse,
    CacheTriggerResponse,
    CalendarApplyRequest,
    CalendarCsvPreviewRequest,
    CalendarDayInput,
    CalendarImportResponse,
    CalendarJsonPreviewRequest,
    CalendarManagedDayResponse,
    CalendarMarketRequest,
    CalendarMarketResponse,
    CalendarMutationRequest,
    CalendarPreviewResponse,
    CalendarPublishRequest,
    CalendarRevisionResponse,
    CalendarRollbackRequest,
    CalendarYearResponse,
    ChangePasswordRequest,
    CorrectionListResponse,
    CorrectionResponse,
    CredentialCreateRequest,
    CredentialCreateResponse,
    CredentialListResponse,
    CredentialOverviewResponse,
    CredentialResponse,
    DQIssueListResponse,
    DQIssueResponse,
    InstrumentCacheDocument,
    InstrumentCacheItem,
    InstrumentCacheItemPatchRequest,
    InstrumentCacheItemUpdateResponse,
    InstrumentCacheReplaceRequest,
    InstrumentCacheWriteResponse,
    LoginRequest,
    MarketFreshnessListResponse,
    MarketFreshnessResponse,
    MissingDeliveryAlertListResponse,
    MissingDeliveryAlertResponse,
    PasswordResetRequest,
    PatchEODRequest,
    PatchEODResponse,
    QueueHealthResponse,
    RawPayloadListResponse,
    RawPayloadResponse,
    ResolveDQIssueRequest,
    ResolveDQIssueResponse,
    SchedulerControlListResponse,
    SchedulerControlMutationResponse,
    SchedulerControlResponse,
    SchedulerControlUpdateRequest,
    SessionResponse,
    SourceClientCreateRequest,
    SourceClientCreateResponse,
    SourceClientListResponse,
    SourceClientResponse,
    SuccessResponse,
)
from app.schemas.common import PaginationInfo
from app.services.admin import (
    AlreadyResolvedError,
    InvalidCorrectionError,
    NoChangesError,
    RecordNotFoundError,
    list_corrections,
    list_dq_issues,
    patch_eod_record,
    present_correction_actor,
    resolve_dq_issue,
)
from app.services.admin_audit import record_admin_audit
from app.services.admin_identity import (
    active_owner_count,
    authenticate_user,
    create_user,
    issue_session,
    lock_admin_identity,
    revoke_session,
    set_password,
    temporary_password,
    update_user,
    verify_password,
)
from app.services.api_keys import (
    create_api_key,
    list_api_keys,
    revoke_api_key,
    rotate_api_key,
)
from app.services.calendar_management import (
    CalendarError,
    RevisionConflictError,
    _apply_rows,
    apply_preview,
    clone_with_day_change,
    create_preview,
    latest_revision,
    parse_twse_csv,
    publish_revision,
    revision_days,
    rollback_revision,
)
from app.services.credentials import (
    credential_overview,
    list_credentials,
    present_api_key,
    present_source,
)
from app.services.delivery_monitor import list_missing_delivery_alerts
from app.services.ingestion import IngestionService, RawPayloadNotFoundError
from app.services.instrument_cache import (
    InstrumentCacheItemNotFoundError,
    InstrumentCacheNotFoundError,
    InstrumentCacheValidationError,
    read_instrument_cache,
    replace_instrument_cache,
    update_instrument_cache_item,
)
from app.services.market_freshness import list_market_freshness
from app.services.normalization_queue import queue_health
from app.services.scheduler_control import (
    SchedulerControlNotFoundError,
    SchedulerControlRevisionConflictError,
    list_scheduler_controls,
    present_scheduler_control,
    update_scheduler_desired_state,
)
from app.services.source_clients import (
    create_source_client,
    list_source_clients,
    revoke_source_client,
    rotate_source_client,
)
from app.utils import utc_now
from app.vocabulary import normalize_market
from scripts.generate_instrument_cache import main as run_cache_generation

router = APIRouter()
settings = get_settings()


def _calendar_revision_response(row: CalendarYearRevision) -> CalendarRevisionResponse:
    return CalendarRevisionResponse(
        market=row.market,
        year=row.year,
        revision=row.revision,
        status=row.status,
        expected_days=row.expected_days,
        actual_days=row.actual_days,
        timezone=row.timezone,
        source_kind=row.source_kind,
        source_filename=row.source_filename,
        published_at=row.published_at,
        updated_at=row.updated_at,
        coverage_complete=row.actual_days == row.expected_days,
    )


def _session_response(user: AdminUser, session: AdminSession, token: str) -> SessionResponse:
    return SessionResponse(
        access_token=token,
        expires_at=session.expires_at,
        user=AdminUserResponse.model_validate(user),
    )


@router.post("/auth/bootstrap", response_model=SessionResponse)
async def bootstrap_admin(
    body: BootstrapRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create the first Owner only, authorized by the break-glass key."""
    supplied = request.headers.get(settings.API_KEY_HEADER)
    if supplied and request.headers.get("authorization"):
        raise HTTPException(status_code=400, detail="Provide exactly one admin credential")
    expected = get_admin_break_glass_api_key()
    if not supplied:
        raise HTTPException(status_code=401, detail="Missing break-glass credential")
    from hmac import compare_digest

    if not expected or not compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid break-glass credential")
    await lock_admin_identity(db)
    if (
        await active_owner_count(db)
        or (await db.execute(select(AdminUser.user_id).limit(1))).first()
    ):
        await db.rollback()
        raise HTTPException(status_code=409, detail="Admin bootstrap is already complete")
    try:
        user = await create_user(
            db,
            username=body.username,
            display_name=body.display_name,
            password=body.password,
            role="owner",
            commit=False,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists")
    session, token = await issue_session(db, user, commit=False)
    await record_admin_audit(
        db,
        AdminPrincipal(
            "break_glass",
            sha256(supplied.encode("utf-8")).hexdigest()[:12],
            "break-glass",
            "owner",
        ),
        action="bootstrap",
        resource_type="admin_user",
        resource_id=str(user.user_id),
    )
    return _session_response(user, session, token)


@router.post("/auth/login", response_model=SessionResponse)
async def login_admin(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    enforce_admin_login_rate_limit(request, body.username)
    user = await authenticate_user(db, body.username, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    session, token = await issue_session(db, user)
    return _session_response(user, session, token)


@router.get("/auth/me", response_model=AdminUserResponse)
async def get_current_admin(
    principal: AdminPrincipal = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    if principal.actor_type != "user" or principal.actor_id is None:
        raise HTTPException(status_code=403, detail="User session required")
    user = await db.get(AdminUser, UUID(principal.actor_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Admin user not found")
    return AdminUserResponse.model_validate(user)


@router.post("/auth/logout", response_model=SuccessResponse)
async def logout_admin(
    principal: AdminPrincipal = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    if principal.session_id:
        await revoke_session(db, UUID(principal.session_id))
    return SuccessResponse()


@router.get("/schedulers", response_model=SchedulerControlListResponse)
async def list_schedulers(
    _: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """List durable scheduler state for Dashboard and operations tooling."""
    rows = await list_scheduler_controls(db)
    return SchedulerControlListResponse(
        data=[SchedulerControlResponse(**present_scheduler_control(row)) for row in rows]
    )


@router.patch("/schedulers/{scheduler_key}", response_model=SchedulerControlMutationResponse)
async def patch_scheduler(
    scheduler_key: str,
    body: SchedulerControlUpdateRequest,
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Change desired scheduler state with optimistic revision control."""
    try:
        row = await update_scheduler_desired_state(
            db,
            scheduler_key=scheduler_key,
            desired_state=body.desired_state,
            expected_revision=body.expected_revision,
            principal=principal,
        )
    except SchedulerControlNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Scheduler not found") from exc
    except SchedulerControlRevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SchedulerControlMutationResponse(
        data=SchedulerControlResponse(**present_scheduler_control(row))
    )


@router.post("/auth/change-password", response_model=SuccessResponse)
async def change_admin_password(
    body: ChangePasswordRequest,
    principal: AdminPrincipal = Depends(verify_admin_api_key),
    db: AsyncSession = Depends(get_db),
):
    if principal.actor_type != "user" or principal.actor_id is None:
        raise HTTPException(status_code=403, detail="User session required")
    user = await db.get(AdminUser, UUID(principal.actor_id))
    if user is None or not verify_password(user.password_hash, body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    await set_password(db, user, body.new_password, must_change=False, commit=False)
    await record_admin_audit(
        db,
        principal,
        action="change_password",
        resource_type="admin_user",
        resource_id=str(user.user_id),
    )
    return SuccessResponse()


@router.get("/users", response_model=AdminUserListResponse)
async def list_admin_users(
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    rows = list((await db.execute(select(AdminUser).order_by(AdminUser.username))).scalars().all())
    return AdminUserListResponse(data=[AdminUserResponse.model_validate(row) for row in rows])


@router.post("/users", response_model=AdminUserEnvelope)
async def create_admin_user(
    body: AdminUserCreateRequest,
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    plaintext = body.password or temporary_password()
    try:
        row = await create_user(
            db,
            username=body.username,
            display_name=body.display_name,
            password=plaintext,
            role=body.role,
            must_change_password=body.must_change_password or body.password is None,
            commit=False,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists")
    await record_admin_audit(
        db,
        principal,
        action="create",
        resource_type="admin_user",
        resource_id=str(row.user_id),
        details={"role": row.role},
    )
    return AdminUserEnvelope(
        data=AdminUserResponse.model_validate(row),
        temporary_password=plaintext if body.password is None else None,
    )


@router.patch("/users/{user_id}", response_model=AdminUserEnvelope)
async def update_admin_user(
    user_id: UUID,
    body: AdminUserUpdateRequest,
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(AdminUser, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Admin user not found")
    try:
        row = await update_user(
            db,
            row,
            display_name=body.display_name,
            role=body.role,
            is_active=body.is_active,
            commit=False,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    await record_admin_audit(
        db,
        principal,
        action="update",
        resource_type="admin_user",
        resource_id=str(row.user_id),
        details=body.model_dump(exclude_none=True),
    )
    return AdminUserEnvelope(data=AdminUserResponse.model_validate(row))


@router.post("/users/{user_id}/reset-password", response_model=AdminUserEnvelope)
async def reset_admin_user_password(
    user_id: UUID,
    body: PasswordResetRequest,
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(AdminUser, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Admin user not found")
    plaintext = body.password or temporary_password()
    row = await set_password(db, row, plaintext, must_change=True, commit=False)
    await record_admin_audit(
        db,
        principal,
        action="reset_password",
        resource_type="admin_user",
        resource_id=str(row.user_id),
    )
    return AdminUserEnvelope(
        data=AdminUserResponse.model_validate(row),
        temporary_password=plaintext,
    )


@router.get("/credentials", response_model=CredentialListResponse)
async def list_credentials_endpoint(
    kind: Optional[Literal["source", "serve", "admin"]] = None,
    credential_status: Optional[Literal["active", "expiring", "expired", "revoked"]] = Query(
        None, alias="status"
    ),
    owner: Optional[str] = None,
    principal: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_credentials(db, kind=kind, status=credential_status, owner=owner)
    return CredentialListResponse(data=rows)


@router.get("/credentials/overview", response_model=CredentialOverviewResponse)
async def credentials_overview_endpoint(
    principal: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    return CredentialOverviewResponse(**await credential_overview(db))


@router.post("/credentials", response_model=CredentialCreateResponse)
async def create_credential_endpoint(
    body: CredentialCreateRequest,
    principal: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    if body.kind == "admin" and principal.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required for Admin credentials")
    if body.kind == "source":
        if not body.name or not body.owner or not body.source_name:
            raise HTTPException(
                status_code=422,
                detail="Source credentials require name, owner, and source_name",
            )
        source_row, plaintext = await create_source_client(
            db,
            name=body.name,
            owner=body.owner,
            description=body.description,
            source_name=body.source_name,
            allowed_datasets=body.allowed_datasets,
            rate_limit_requests=body.rate_limit_requests,
            rate_limit_window=body.rate_limit_window,
            expires_at=body.expires_at,
            commit=False,
        )
        data = present_source(source_row)
        resource_id = str(source_row.client_id)
    else:
        if not body.owner:
            raise HTTPException(status_code=422, detail="Serve/Admin credentials require owner")
        api_key_row, plaintext = await create_api_key(
            db,
            owner=body.owner,
            tier=body.tier,
            scopes=body.scopes or (["serve"] if body.kind == "serve" else ["admin"]),
            rate_limit_requests=body.rate_limit_requests,
            rate_limit_window=body.rate_limit_window,
            page_size_limit=body.page_size_limit,
            kind=body.kind,
            name=body.name,
            description=body.description,
            role=body.role or ("viewer" if body.kind == "admin" else None),
            expires_at=body.expires_at,
            commit=False,
        )
        data = present_api_key(api_key_row)
        resource_id = str(api_key_row.key_id)
    await record_admin_audit(
        db,
        principal,
        action="create",
        resource_type=f"{body.kind}_credential",
        resource_id=resource_id,
    )
    return CredentialCreateResponse(api_key=plaintext, data=data)


@router.post(
    "/credentials/{kind}/{credential_id}/rotate",
    response_model=CredentialCreateResponse,
)
async def rotate_credential_endpoint(
    kind: Literal["source", "serve", "admin"],
    credential_id: UUID,
    principal: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    if kind == "admin" and principal.role != "owner":
        raise HTTPException(status_code=403, detail="Owner role required for Admin credentials")
    if kind == "source":
        source = await db.get(SourceClient, credential_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        rotated_source, plaintext = await rotate_source_client(db, source, commit=False)
        data = present_source(rotated_source)
        rotated_id = str(rotated_source.client_id)
    else:
        key = await db.get(APIKey, credential_id)
        if key is None or key.kind != kind:
            raise HTTPException(status_code=404, detail="Credential not found")
        rotated_key, plaintext = await rotate_api_key(db, key, commit=False)
        data = present_api_key(rotated_key)
        rotated_id = str(rotated_key.key_id)
    await record_admin_audit(
        db,
        principal,
        action="rotate",
        resource_type=f"{kind}_credential",
        resource_id=rotated_id,
        details={"rotated_from_id": str(credential_id)},
    )
    return CredentialCreateResponse(api_key=plaintext, data=data)


@router.delete(
    "/credentials/{kind}/{credential_id}",
    response_model=CredentialResponse,
)
async def revoke_credential_endpoint(
    kind: Literal["source", "serve", "admin"],
    credential_id: UUID,
    principal: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    if kind == "admin":
        if principal.role != "owner":
            raise HTTPException(status_code=403, detail="Owner role required for Admin credentials")
        if principal.actor_type == "machine" and principal.actor_id == str(credential_id):
            raise HTTPException(status_code=409, detail="Cannot revoke the credential in use")
    if kind == "source":
        revoked_source = await revoke_source_client(db, credential_id, commit=False)
        if revoked_source is None:
            raise HTTPException(status_code=404, detail="Credential not found")
        await record_admin_audit(
            db,
            principal,
            action="revoke",
            resource_type="source_credential",
            resource_id=str(credential_id),
        )
        return present_source(revoked_source)
    api_key_row = await db.get(APIKey, credential_id)
    if api_key_row is None or api_key_row.kind != kind:
        raise HTTPException(status_code=404, detail="Credential not found")
    revoked_key = await revoke_api_key(db, credential_id, commit=False)
    if revoked_key is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    await record_admin_audit(
        db,
        principal,
        action="revoke",
        resource_type=f"{kind}_credential",
        resource_id=str(credential_id),
    )
    return present_api_key(revoked_key)


@router.post("/source-clients", response_model=SourceClientCreateResponse)
async def create_source_client_endpoint(
    body: SourceClientCreateRequest,
    api_key: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """Create a provider identity; the plaintext key is returned once."""
    try:
        row, plaintext = await create_source_client(
            db,
            name=body.name,
            owner=body.owner,
            description=body.description,
            source_name=body.source_name,
            allowed_datasets=body.allowed_datasets,
            rate_limit_requests=body.rate_limit_requests,
            rate_limit_window=body.rate_limit_window,
            expires_at=body.expires_at,
            commit=False,
        )
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Source client conflicts with an existing key")
    await record_admin_audit(
        db,
        api_key,
        action="create",
        resource_type="source_credential",
        resource_id=str(row.client_id),
    )
    return SourceClientCreateResponse(
        api_key=plaintext,
        data=SourceClientResponse.model_validate(row),
    )


@router.get("/source-clients", response_model=SourceClientListResponse)
async def list_source_clients_endpoint(
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    rows = await list_source_clients(db)
    return SourceClientListResponse(data=[SourceClientResponse.model_validate(row) for row in rows])


@router.delete("/source-clients/{client_id}", response_model=SourceClientResponse)
async def revoke_source_client_endpoint(
    client_id: UUID,
    api_key: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    row = await revoke_source_client(db, client_id, commit=False)
    if row is None:
        raise HTTPException(status_code=404, detail="Source client not found")
    await record_admin_audit(
        db,
        api_key,
        action="revoke",
        resource_type="source_credential",
        resource_id=str(client_id),
    )
    return SourceClientResponse.model_validate(row)


@router.get("/queue/health", response_model=QueueHealthResponse)
async def queue_health_endpoint(
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """Return DB-authoritative delivery and worker health."""
    return QueueHealthResponse(**await queue_health(db))


@router.get("/missing-deliveries", response_model=MissingDeliveryAlertListResponse)
async def list_missing_deliveries_endpoint(
    alert_status: Optional[Literal["open", "resolved"]] = Query(None, alias="status"),
    dataset_key: Optional[str] = Query(None, min_length=1, max_length=50),
    source: Optional[str] = Query(None, min_length=1, max_length=50),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """List durable missing-delivery alerts without mutating their state."""
    rows, total = await list_missing_delivery_alerts(
        db,
        status=alert_status,
        dataset_key=dataset_key,
        source=source,
        page=page,
        page_size=page_size,
    )
    return MissingDeliveryAlertListResponse(
        data=[MissingDeliveryAlertResponse.model_validate(row) for row in rows],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=(total + page_size - 1) // page_size if total else 0,
        ),
    )


@router.get("/market-freshness", response_model=MarketFreshnessListResponse)
async def list_market_freshness_endpoint(
    market: Optional[str] = Query(None, min_length=1, max_length=10),
    slot_id: Optional[str] = Query(None, min_length=1, max_length=50),
    freshness_status: Optional[
        Literal["not_due", "fresh", "partial", "late", "failed", "never_received"]
    ] = Query(None, alias="status"),
    include_feeds: bool = Query(True),
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """Read the configured market-delivery freshness projection."""
    rows = await list_market_freshness(db, market=market, slot_id=slot_id, status=freshness_status)
    data = []
    for row in rows:
        payload = {
            "market": row.market,
            "slot_id": row.slot_id,
            "scheduled_local_time": row.scheduled_local_time,
            "timezone": row.timezone,
            "status": row.status,
            "expected_data_date": row.expected_data_date,
            "coverage_data_date": row.coverage_data_date,
            "last_successful_update_at": row.last_successful_update_at,
            "last_complete_at": row.last_complete_at,
            "next_scheduled_at": row.next_scheduled_at,
            "feed_count": row.feed_count,
            "fresh_feed_count": row.fresh_feed_count,
            "late_feed_count": row.late_feed_count,
            "feeds": list(row.feeds) if include_feeds else [],
        }
        data.append(MarketFreshnessResponse.model_validate(payload))
    return MarketFreshnessListResponse(data=data)


@router.post("/api-keys", response_model=APIKeyCreateResponse)
async def create_api_key_endpoint(
    body: APIKeyCreateRequest,
    api_key: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """Issue a hashed Serve API key. Plaintext is returned once."""
    row, plaintext = await create_api_key(
        db,
        owner=body.owner,
        tier=body.tier,
        scopes=body.scopes,
        rate_limit_requests=body.rate_limit_requests,
        rate_limit_window=body.rate_limit_window,
        page_size_limit=body.page_size_limit,
        name=body.name,
        description=body.description,
        expires_at=body.expires_at,
        commit=False,
    )
    await record_admin_audit(
        db,
        api_key,
        action="create",
        resource_type="serve_credential",
        resource_id=str(row.key_id),
    )
    return APIKeyCreateResponse(api_key=plaintext, data=APIKeyResponse.model_validate(row))


@router.get("/api-keys", response_model=APIKeyListResponse)
async def list_api_keys_endpoint(
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """List API key metadata without hashes or plaintext."""
    rows = await list_api_keys(db)
    return APIKeyListResponse(data=[APIKeyResponse.model_validate(row) for row in rows])


@router.delete("/api-keys/{key_id}", response_model=APIKeyResponse)
async def revoke_api_key_endpoint(
    key_id: UUID,
    api_key: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """Revoke an API key."""
    existing = await db.get(APIKey, key_id)
    if existing is None or existing.kind != "serve":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    row = await revoke_api_key(db, key_id, commit=False)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    await record_admin_audit(
        db,
        api_key,
        action="revoke",
        resource_type="serve_credential",
        resource_id=str(key_id),
    )
    return APIKeyResponse.model_validate(row)


@router.get("/instrument-cache", response_model=InstrumentCacheDocument)
async def get_instrument_cache(
    api_key: AdminPrincipal = Depends(require_viewer),
):
    """回傳已產生的靜態商品查詢快取。"""
    try:
        return read_instrument_cache()
    except InstrumentCacheNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstrumentCacheValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.put("/instrument-cache", response_model=InstrumentCacheWriteResponse)
async def put_instrument_cache(
    body: InstrumentCacheReplaceRequest,
    api_key: AdminPrincipal = Depends(require_operator),
):
    """以已驗證且正規化的文件替換 instruments.json。"""
    try:
        payload = replace_instrument_cache(body.model_dump(mode="json"))
    except InstrumentCacheValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return InstrumentCacheWriteResponse(
        message="Instrument cache updated successfully",
        data=InstrumentCacheDocument.model_validate(payload),
    )


@router.patch(
    "/instrument-cache/items/{instrument_id}",
    response_model=InstrumentCacheItemUpdateResponse,
)
async def patch_instrument_cache_item(
    instrument_id: str,
    body: InstrumentCacheItemPatchRequest,
    api_key: AdminPrincipal = Depends(require_operator),
):
    """修補產生後 instruments.json 快取中的單一商品。"""
    try:
        item = update_instrument_cache_item(
            instrument_id,
            body.model_dump(mode="json", exclude_unset=True),
        )
    except InstrumentCacheNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstrumentCacheItemNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InstrumentCacheValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return InstrumentCacheItemUpdateResponse(
        message="Instrument cache item updated successfully",
        data=InstrumentCacheItem.model_validate(item),
    )


@router.get("/dq-issues", response_model=DQIssueListResponse)
async def list_dq_issues_endpoint(
    resolved: Optional[bool] = Query(None, description="依是否已解決篩選"),
    instrument_id: Optional[UUID] = Query(None, description="依商品 UUID 篩選"),
    severity: Optional[str] = Query(None, description="依嚴重程度篩選：warning / error"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """列出資料品質問題，可依解決狀態、商品或嚴重程度篩選。"""
    rows, total = await list_dq_issues(db, resolved, instrument_id, severity, page, page_size)
    total_pages = (total + page_size - 1) // page_size if total else 0
    return DQIssueListResponse(
        data=[DQIssueResponse.model_validate(r) for r in rows],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=total_pages,
        ),
    )


@router.patch("/eod/{instrument_id}/{trade_date}", response_model=PatchEODResponse)
async def patch_eod(
    instrument_id: UUID,
    trade_date: date,
    body: PatchEODRequest,
    api_key: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """修補 MarketDataEOD 紀錄的 OHLCV 欄位，並建立不可變的修正稽核紀錄。"""
    try:
        correction, eod = await patch_eod_record(db, instrument_id, trade_date, body, api_key)
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except NoChangesError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except InvalidCorrectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return PatchEODResponse(
        correction_id=correction.id,
        record_id=correction.record_id,
        instrument_id=eod.instrument_id,
        trade_date=eod.trade_date,
        message="EOD record corrected successfully",
    )


@router.patch("/dq-issues/{issue_id}/resolve", response_model=ResolveDQIssueResponse)
async def resolve_dq_issue_endpoint(
    issue_id: UUID,
    body: ResolveDQIssueRequest,
    api_key: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """以稽核原因將資料品質問題標記為已解決，並建立修正紀錄。"""
    try:
        correction, issue = await resolve_dq_issue(db, issue_id, body, api_key)
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except AlreadyResolvedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    resolved_at = issue.resolved_at
    if resolved_at is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="DQ issue resolution did not set resolved_at",
        )

    return ResolveDQIssueResponse(
        correction_id=correction.id,
        issue_id=issue.id,
        resolved_at=resolved_at,
        message="DQ issue resolved successfully",
    )


@router.get("/raw-payloads", response_model=RawPayloadListResponse)
async def list_raw_payloads(
    dataset_key: Optional[str] = Query(None, description="依資料集 key 篩選"),
    run_id: Optional[UUID] = Query(None, description="依匯入執行 UUID 篩選"),
    date_from: Optional[date] = Query(
        None, description="篩選 created_at 大於或等於此日期 (YYYY-MM-DD, UTC)"
    ),
    date_to: Optional[date] = Query(
        None, description="篩選 created_at 小於或等於此日期 (YYYY-MM-DD, UTC)"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """列出原始市場資料，最新資料在前；可依 dataset_key、run_id 或日期區間篩選。"""
    stmt = select(RawMarketPayload)
    count_stmt = select(func.count()).select_from(RawMarketPayload)

    if dataset_key:
        stmt = stmt.where(RawMarketPayload.dataset_key == dataset_key)
        count_stmt = count_stmt.where(RawMarketPayload.dataset_key == dataset_key)
    if run_id:
        stmt = stmt.where(RawMarketPayload.run_id == run_id)
        count_stmt = count_stmt.where(RawMarketPayload.run_id == run_id)
    if date_from:
        dt_from = datetime(date_from.year, date_from.month, date_from.day, tzinfo=timezone.utc)
        stmt = stmt.where(RawMarketPayload.created_at >= dt_from)
        count_stmt = count_stmt.where(RawMarketPayload.created_at >= dt_from)
    if date_to:
        dt_to = datetime(date_to.year, date_to.month, date_to.day, tzinfo=timezone.utc) + timedelta(
            days=1
        )
        stmt = stmt.where(RawMarketPayload.created_at < dt_to)
        count_stmt = count_stmt.where(RawMarketPayload.created_at < dt_to)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(RawMarketPayload.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    total_pages = (total + page_size - 1) // page_size if total else 0
    return RawPayloadListResponse(
        data=[RawPayloadResponse.model_validate(r) for r in rows],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=total_pages,
        ),
    )


@router.get("/raw-payloads/{run_id}", response_model=RawPayloadResponse)
async def get_raw_payload_by_run(
    run_id: UUID,
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """取得指定匯入執行的原始資料。"""
    stmt = select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
    row = (await db.execute(stmt)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Raw payload not found")
    return RawPayloadResponse.model_validate(row)


@router.get("/corrections", response_model=CorrectionListResponse)
async def list_corrections_endpoint(
    table_name: Optional[str] = Query(None, description="依 canonical 資料表名稱篩選"),
    instrument_id: Optional[UUID] = Query(None, description="依商品 UUID 篩選"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    api_key: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    """列出修正稽核紀錄，最新資料在前；可依資料表或商品篩選。"""
    rows, total = await list_corrections(db, table_name, instrument_id, page, page_size)
    total_pages = (total + page_size - 1) // page_size if total else 0

    return CorrectionListResponse(
        data=[
            CorrectionResponse(
                id=c.id,
                table_name=c.table_name,
                record_id=c.record_id,
                instrument_id=c.instrument_id,
                trade_date=c.trade_date,
                corrected_by=present_correction_actor(c.corrected_by),
                correction_reason=c.correction_reason,
                before_snapshot=c.before_snapshot,
                after_snapshot=c.after_snapshot,
                created_at=c.created_at,
            )
            for c in rows
        ],
        pagination=PaginationInfo(
            page=page,
            page_size=page_size,
            total_records=total,
            total_pages=total_pages,
        ),
    )


@router.post("/runs/bulk-rerun", response_model=BulkRerunResponse)
async def bulk_rerun_runs(
    dataset_key: Optional[str] = Query(None, description="依 dataset_key 篩選"),
    run_status: Optional[str] = Query(
        "completed",
        alias="status",
        description="要包含的執行狀態：completed / failed / all",
    ),
    limit: int = Query(
        100,
        ge=1,
        le=1000,
        description="單次最多排入的 run 數量，避免無界重跑全部歷史資料",
    ),
    api_key: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """針對符合篩選條件的既有執行紀錄重新執行正規化。

    會為每筆符合條件的紀錄建立新的匯入執行、job 與 outbox event。
    此端點會立即回傳摘要。
    """
    stmt = select(IngestionRun.run_id)
    if dataset_key:
        stmt = stmt.where(IngestionRun.dataset_key == dataset_key)
    if run_status and run_status != "all":
        stmt = stmt.where(IngestionRun.status == run_status)
    stmt = stmt.order_by(IngestionRun.created_at).limit(limit)

    result = await db.execute(stmt)
    run_ids = [row[0] for row in result.all()]

    service = IngestionService(db)
    queued = 0
    skipped = 0
    errors = 0
    new_run_ids: list[str] = []
    error_details: list[str] = []

    for run_id in run_ids:
        try:
            new_run_id, _, _, _ = await service.rerun_from_raw(run_id)
            new_run_ids.append(str(new_run_id))
            queued += 1
        except RawPayloadNotFoundError:
            await db.rollback()
            skipped += 1
        except Exception as e:
            await db.rollback()
            errors += 1
            error_details.append(f"{run_id}: {e}")

    return BulkRerunResponse(
        queued=queued,
        skipped=skipped,
        errors=errors,
        new_run_ids=new_run_ids,
        error_details=error_details,
    )


@router.post(
    "/instrument-cache/refresh",
    response_model=CacheTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_instrument_cache(
    background_tasks: BackgroundTasks,
    api_key: AdminPrincipal = Depends(require_operator),
):
    """
    手動刷新靜態商品快取 instruments.json。
    這個長時間執行的任務會在背景處理。
    """

    def task_wrapper():
        print("Starting background cache generation...")
        exit_code = run_cache_generation()
        if exit_code == 0:
            print("Background cache generation completed successfully.")
        else:
            print("Background cache generation failed.")

    background_tasks.add_task(task_wrapper)

    return CacheTriggerResponse(
        message="Cache generation task has been queued in the background.", status="accepted"
    )


# ── Managed market/year calendars ───────────────────────────────────────────


@router.get("/calendars/markets", response_model=list[CalendarMarketResponse])
async def list_calendar_markets(
    _: AdminPrincipal = Depends(require_viewer), db: AsyncSession = Depends(get_db)
):
    return (
        (await db.execute(select(CalendarMarket).order_by(CalendarMarket.market))).scalars().all()
    )


@router.put("/calendars/markets/{market}", response_model=CalendarMarketResponse)
async def upsert_calendar_market(
    market: str,
    body: CalendarMarketRequest,
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    try:
        normalized = normalize_market(market)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if normalized != normalize_market(body.market):
        raise HTTPException(status_code=422, detail="market path and body must match")
    row = await db.get(CalendarMarket, normalized)
    if row is None:
        row = CalendarMarket(
            market=normalized,
            **body.model_dump(exclude={"market"}),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.add(row)
        action = "create_calendar_market"
    else:
        for field, value in body.model_dump(exclude={"market"}).items():
            setattr(row, field, value)
        row.updated_at = utc_now()
        action = "update_calendar_market"
    await record_admin_audit(
        db, principal, action=action, resource_type="calendar_market", resource_id=normalized
    )
    await db.refresh(row)
    return row


@router.get("/calendars/years", response_model=list[CalendarRevisionResponse])
async def list_calendar_years(
    market: str = Query(..., min_length=1, max_length=10),
    _: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    normalized = normalize_market(market)
    rows = (
        (
            await db.execute(
                select(CalendarYearRevision)
                .where(CalendarYearRevision.market == normalized)
                .order_by(CalendarYearRevision.year.desc(), CalendarYearRevision.revision.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_calendar_revision_response(row) for row in rows]


@router.get("/calendars/days", response_model=CalendarYearResponse)
async def get_managed_calendar_year(
    market: str = Query(..., min_length=1, max_length=10),
    year: int = Query(..., ge=1900, le=2200),
    _: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    row = await latest_revision(db, normalize_market(market), year)
    if row is None:
        raise HTTPException(status_code=404, detail="Managed calendar year not found")
    days = await revision_days(db, row.id)
    published = (
        await db.execute(
            select(CalendarYearRevision).where(
                CalendarYearRevision.market == row.market,
                CalendarYearRevision.year == row.year,
                CalendarYearRevision.status == "published",
            )
        )
    ).scalar_one_or_none()
    return CalendarYearResponse(
        revision=_calendar_revision_response(row),
        days=[
            CalendarManagedDayResponse(
                market=row.market,
                trade_date=day.trade_date,
                status=day.day_status,
                is_open=day.is_open,
                session_open=day.session_open,
                session_close=day.session_close,
                holiday_name=day.holiday_name,
                description=day.description,
                revision=row.revision,
                source_kind=day.source_kind,
            )
            for day in days
        ],
        published_revision=_calendar_revision_response(published) if published else None,
    )


@router.get("/calendars/imports", response_model=list[CalendarImportResponse])
async def list_calendar_imports(
    market: str = Query(..., min_length=1, max_length=10),
    year: int = Query(..., ge=1900, le=2200),
    _: AdminPrincipal = Depends(require_viewer),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        (
            await db.execute(
                select(CalendarImportBatch)
                .where(
                    CalendarImportBatch.market == normalize_market(market),
                    CalendarImportBatch.year == year,
                )
                .order_by(CalendarImportBatch.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        CalendarImportResponse.model_validate(row).model_copy(
            update={"revision": row.base_revision + 1 if row.status == "applied" else None}
        )
        for row in rows
    ]


def _preview_response(batch: CalendarImportBatch) -> CalendarPreviewResponse:
    return CalendarPreviewResponse(
        batch_id=batch.id,
        market=batch.market,
        year=batch.year,
        input_format=batch.input_format,
        detected_encoding=batch.detected_encoding,
        base_revision=batch.base_revision,
        summary=batch.summary,
        warnings=batch.warnings,
        errors=batch.errors,
        days=[
            CalendarDayInput(
                trade_date=row["trade_date"],
                status=row["status"],
                holiday_name=row.get("holiday_name"),
                description=row.get("description"),
                session_open=row.get("session_open"),
                session_close=row.get("session_close"),
            )
            for row in batch.candidate_rows
        ],
        expires_at=batch.expires_at,
    )


@router.post("/calendars/imports/json/preview", response_model=CalendarPreviewResponse)
async def preview_calendar_json(
    body: CalendarJsonPreviewRequest,
    principal: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    market = await db.get(CalendarMarket, normalize_market(body.market))
    if market is None or not market.active:
        raise HTTPException(status_code=404, detail="Active calendar market not found")
    try:
        rows = _apply_rows(
            year=body.year,
            market=market,
            rows=[item.model_dump(mode="json") for item in body.days],
            coverage_mode=body.coverage_mode,
        )
    except CalendarError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    batch = await create_preview(
        db,
        market=market,
        year=body.year,
        input_format="json",
        rows=rows,
        source_bytes=None,
        source_filename=body.source_filename,
        detected_encoding=None,
        created_by=principal.actor_id,
        commit=False,
    )
    await record_admin_audit(
        db,
        principal,
        action="preview_calendar_json",
        resource_type="calendar_import",
        resource_id=str(batch.id),
        details={
            "market": batch.market,
            "year": batch.year,
            "base_revision": batch.base_revision,
            "actual_days": batch.summary.get("total"),
        },
    )
    return _preview_response(batch)


@router.post("/calendars/imports/preview", response_model=CalendarPreviewResponse)
async def preview_twse_calendar_csv(
    body: CalendarCsvPreviewRequest,
    principal: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    market = await db.get(CalendarMarket, normalize_market(body.market))
    if market is None or not market.active:
        raise HTTPException(status_code=404, detail="Active calendar market not found")
    try:
        raw = b64decode(body.content_base64, validate=True)
        rows, encoding = parse_twse_csv(raw, year=body.year, market=market)
    except (ValueError, CalendarError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    batch = await create_preview(
        db,
        market=market,
        year=body.year,
        input_format="twse_csv",
        rows=rows,
        source_bytes=raw,
        source_filename=body.filename,
        detected_encoding=encoding,
        created_by=principal.actor_id,
        commit=False,
    )
    await record_admin_audit(
        db,
        principal,
        action="preview_calendar_csv",
        resource_type="calendar_import",
        resource_id=str(batch.id),
        details={
            "market": batch.market,
            "year": batch.year,
            "base_revision": batch.base_revision,
            "actual_days": batch.summary.get("total"),
            "source_sha256": batch.source_sha256,
        },
    )
    return _preview_response(batch)


@router.post("/calendars/imports/{batch_id}/apply", response_model=CalendarRevisionResponse)
async def apply_calendar_preview(
    batch_id: UUID,
    body: CalendarApplyRequest,
    principal: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    try:
        revision = await apply_preview(
            db,
            batch_id=batch_id,
            expected_revision=body.expected_revision,
            actor=principal.actor_id,
            commit=False,
        )
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await record_admin_audit(
        db,
        principal,
        action="apply_calendar_preview",
        resource_type="calendar_year",
        resource_id=f"{revision.market}:{revision.year}",
        details={
            "revision": revision.revision,
            "batch_id": str(batch_id),
            "actual_days": revision.actual_days,
        },
    )
    return _calendar_revision_response(revision)


@router.patch("/calendars/{market}/{trade_date}", response_model=CalendarRevisionResponse)
async def edit_calendar_day(
    market: str,
    trade_date: date,
    body: CalendarMutationRequest,
    principal: AdminPrincipal = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    normalized = normalize_market(market)
    if await db.get(CalendarMarket, normalized) is None:
        raise HTTPException(status_code=404, detail="Calendar market not found")
    try:
        revision = await clone_with_day_change(
            db,
            market=normalized,
            trade_date=trade_date,
            expected_revision=body.expected_revision,
            replacement=body.model_dump(exclude={"expected_revision", "reason"}, mode="json"),
            commit=False,
        )
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await record_admin_audit(
        db,
        principal,
        action="edit_calendar_day",
        resource_type="calendar_day",
        resource_id=f"{normalized}:{trade_date.isoformat()}",
        details={"revision": revision.revision, "reason": body.reason},
    )
    return _calendar_revision_response(revision)


@router.post("/calendars/{market}/{year}/publish", response_model=CalendarRevisionResponse)
async def publish_calendar_year(
    market: str,
    year: int,
    body: CalendarPublishRequest,
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    try:
        revision = await publish_revision(
            db,
            market=normalize_market(market),
            year=year,
            expected_revision=body.expected_revision,
            published_by=principal.actor_id,
            commit=False,
        )
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await record_admin_audit(
        db,
        principal,
        action="publish_calendar_year",
        resource_type="calendar_year",
        resource_id=f"{revision.market}:{year}",
        details={"revision": revision.revision},
    )
    return _calendar_revision_response(revision)


@router.post("/calendars/{market}/{year}/rollback", response_model=CalendarRevisionResponse)
async def rollback_calendar_year(
    market: str,
    year: int,
    body: CalendarRollbackRequest,
    principal: AdminPrincipal = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
):
    """Publish a new highest revision cloned from a prior complete revision."""
    try:
        revision = await rollback_revision(
            db,
            market=normalize_market(market),
            year=year,
            target_revision=body.target_revision,
            expected_revision=body.expected_revision,
            published_by=principal.actor_id,
            commit=False,
        )
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalendarError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await record_admin_audit(
        db,
        principal,
        action="rollback_calendar_year",
        resource_type="calendar_year",
        resource_id=f"{revision.market}:{year}",
        details={
            "revision": revision.revision,
            "target_revision": body.target_revision,
            "expected_revision": body.expected_revision,
        },
    )
    return _calendar_revision_response(revision)
