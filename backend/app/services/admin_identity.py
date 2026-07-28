"""Local Admin users, password hashing, and opaque session lifecycle."""

from datetime import timedelta
from hashlib import sha256
from secrets import token_urlsafe
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.registry import AdminSession, AdminUser
from app.utils import utc_now, uuid7

ROLES = {"owner", "operator", "viewer"}
ADMIN_IDENTITY_LOCK_ID = 4_604_470_401
_password_hasher = PasswordHasher()


def normalize_username(username: str) -> str:
    return username.strip().lower()


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def hash_session_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def temporary_password() -> str:
    return token_urlsafe(18)


async def active_owner_count(db: AsyncSession) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(AdminUser)
                .where(AdminUser.role == "owner", AdminUser.is_active.is_(True))
            )
        ).scalar_one()
    )


async def lock_admin_identity(db: AsyncSession) -> None:
    """Serialize bootstrap and last-owner decisions within the current transaction."""
    await db.execute(select(func.pg_advisory_xact_lock(ADMIN_IDENTITY_LOCK_ID)))


async def _finish(db: AsyncSession, row, *, commit: bool) -> None:
    if commit:
        await db.commit()
        await db.refresh(row)
    else:
        await db.flush()


async def create_user(
    db: AsyncSession,
    *,
    username: str,
    display_name: str,
    password: str,
    role: str,
    must_change_password: bool = False,
    commit: bool = True,
) -> AdminUser:
    if role not in ROLES:
        raise ValueError("Invalid admin role")
    row = AdminUser(
        user_id=uuid7(),
        username=normalize_username(username),
        display_name=display_name.strip(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        must_change_password=must_change_password,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(row)
    await _finish(db, row, commit=commit)
    return row


async def authenticate_user(db: AsyncSession, username: str, password: str) -> AdminUser | None:
    row = (
        await db.execute(
            select(AdminUser).where(AdminUser.username == normalize_username(username))
        )
    ).scalar_one_or_none()
    if row is None or not row.is_active or not verify_password(row.password_hash, password):
        return None
    return row


async def issue_session(
    db: AsyncSession, user: AdminUser, *, commit: bool = True
) -> tuple[AdminSession, str]:
    settings = get_settings()
    token = f"findb_sess_{token_urlsafe(32)}"
    now = utc_now()
    row = AdminSession(
        session_id=uuid7(),
        user_id=user.user_id,
        token_hash=hash_session_token(token),
        created_at=now,
        expires_at=now + timedelta(hours=settings.ADMIN_SESSION_HOURS),
    )
    db.add(row)
    user.last_login_at = now
    user.updated_at = now
    await _finish(db, row, commit=commit)
    return row, token


async def find_active_session(
    db: AsyncSession, token: str
) -> tuple[AdminSession, AdminUser] | None:
    result = await db.execute(
        select(AdminSession, AdminUser)
        .join(AdminUser, AdminUser.user_id == AdminSession.user_id)
        .where(
            AdminSession.token_hash == hash_session_token(token),
            AdminSession.revoked_at.is_(None),
            AdminSession.expires_at > utc_now(),
            AdminUser.is_active.is_(True),
        )
    )
    pair = result.one_or_none()
    if pair is None:
        return None
    return pair[0], pair[1]


async def revoke_session(db: AsyncSession, session_id: UUID) -> None:
    row = await db.get(AdminSession, session_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = utc_now()
        await db.commit()


async def revoke_user_sessions(db: AsyncSession, user_id: UUID, *, commit: bool = True) -> None:
    await db.execute(
        update(AdminSession)
        .where(AdminSession.user_id == user_id, AdminSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )
    if commit:
        await db.commit()
    else:
        await db.flush()


async def update_user(
    db: AsyncSession,
    row: AdminUser,
    *,
    display_name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    commit: bool = True,
) -> AdminUser:
    removing_owner = row.role == "owner" and (
        (role is not None and role != "owner") or is_active is False
    )
    if removing_owner:
        await lock_admin_identity(db)
        if await active_owner_count(db) <= 1:
            raise ValueError("Cannot disable or downgrade the last active owner")
    if role is not None:
        if role not in ROLES:
            raise ValueError("Invalid admin role")
        row.role = role
    if display_name is not None:
        row.display_name = display_name.strip()
    if is_active is not None:
        row.is_active = is_active
        row.disabled_at = None if is_active else utc_now()
    row.updated_at = utc_now()
    if is_active is False:
        await revoke_user_sessions(db, row.user_id, commit=False)
    await _finish(db, row, commit=commit)
    return row


async def set_password(
    db: AsyncSession,
    row: AdminUser,
    password: str,
    *,
    must_change: bool,
    commit: bool = True,
) -> AdminUser:
    row.password_hash = hash_password(password)
    row.password_changed_at = utc_now()
    row.must_change_password = must_change
    row.updated_at = utc_now()
    await revoke_user_sessions(db, row.user_id, commit=False)
    await _finish(db, row, commit=commit)
    return row
