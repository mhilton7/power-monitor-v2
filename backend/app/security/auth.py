from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import pyotp
from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_session
from ..errors import AuthenticationError, PermissionDenied
from ..models import (
    LoginThrottle,
    MfaCredential,
    Permission,
    Role,
    Session,
    User,
    aware_utc,
    role_permissions,
    user_roles,
)
from .crypto import decrypt_secret
from .passwords import verify_password

SESSION_COOKIE = "pm_session"
CSRF_COOKIE = "pm_csrf"
GENERIC_LOGIN_FAILURE = "email, password, or MFA code is invalid"
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=2$ETtBEzhqh/Wt8RHVDurPvg$"  # noqa: S105
    "4lV2Dn36W1sCA7hAqVkk3CBtCsk3Fbat40ASlhPx9gA"
)

ALL_PERMISSIONS = (
    "dashboard.view",
    "history.view",
    "history.export",
    "billing.view",
    "billing.manage",
    "rates.bill_import",
    "rates.view",
    "rates.manage",
    "rates.sync",
    "sensors.view",
    "sensors.enroll",
    "sensors.configure",
    "sensors.command.reboot",
    "sensors.command.sleep",
    "sensors.command.storage_test",
    "sensors.command.storage_format",
    "sensors.command.ota",
    "sensors.command.data_reset",
    "firmware.view",
    "firmware.manage",
    "users.view",
    "users.manage",
    "backups.view",
    "backups.manage",
    "logs.view",
    "system.view",
    "system.manage",
)

ROLE_PERMISSION_MAP: dict[str, tuple[str, ...]] = {
    "Owner": ALL_PERMISSIONS,
    "Administrator": tuple(value for value in ALL_PERMISSIONS if value != "users.manage"),
    "Member": (
        "dashboard.view",
        "history.view",
        "history.export",
        "billing.view",
        "rates.bill_import",
        "rates.view",
        "sensors.view",
    ),
    "Viewer": ("dashboard.view", "history.view", "billing.view", "rates.view", "sensors.view"),
}


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    display_name: str
    permissions: frozenset[str]
    session_id: str


@dataclass(frozen=True)
class LoginAttemptResult:
    user: User | None
    principal_hash: str
    rate_limited: bool


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _database_utc_now(session: AsyncSession) -> datetime:
    value: datetime | None = await session.scalar(select(func.current_timestamp()))
    if value is None:
        raise RuntimeError("database clock is unavailable")
    return aware_utc(value)


async def seed_access_control(session: AsyncSession) -> dict[str, Role]:
    for name in ALL_PERMISSIONS:
        if await session.get(Permission, name) is None:
            session.add(Permission(name=name, description=name.replace(".", " ")))
    await session.flush()
    roles: dict[str, Role] = {}
    for role_name, permissions in ROLE_PERMISSION_MAP.items():
        role = await session.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            role = Role(name=role_name, built_in=True, description=f"Built-in {role_name} role")
            session.add(role)
            await session.flush()
        roles[role_name] = role
        existing = set(
            (
                await session.scalars(
                    select(role_permissions.c.permission_name).where(
                        role_permissions.c.role_id == role.id
                    )
                )
            ).all()
        )
        for permission_name in set(permissions) - existing:
            await session.execute(
                role_permissions.insert().values(role_id=role.id, permission_name=permission_name)
            )
    return roles


async def owner_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(User.id))
            .join(user_roles, user_roles.c.user_id == User.id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(Role.name == "Owner", User.enabled.is_(True), User.deleted_at.is_(None))
        )
        or 0
    )


async def create_session(
    session: AsyncSession,
    user: User,
    *,
    settings: Settings,
    fingerprint: str | None,
) -> tuple[Session, str, str]:
    session_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    now = await _database_utc_now(session)
    row = Session(
        user_id=user.id,
        token_hash=token_hash(session_token),
        csrf_hash=token_hash(csrf_token),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_absolute_hours),
        client_fingerprint=hashlib.sha256(fingerprint.encode()).hexdigest()
        if fingerprint
        else None,
    )
    session.add(row)
    await session.flush()
    return row, session_token, csrf_token


async def verify_login(
    session: AsyncSession,
    email: str,
    password: str,
    totp_code: str | None,
    settings: Settings,
) -> User:
    user = await session.scalar(
        select(User).where(User.email == email.lower().strip()).with_for_update()
    )
    if user is None or not user.enabled or user.deleted_at is not None:
        verify_password(_DUMMY_PASSWORD_HASH, password)
        raise AuthenticationError(GENERIC_LOGIN_FAILURE)
    if not verify_password(user.password_hash, password):
        raise AuthenticationError(GENERIC_LOGIN_FAILURE)
    mfa = await session.scalar(
        select(MfaCredential)
        .where(MfaCredential.user_id == user.id, MfaCredential.enabled_at.is_not(None))
        .with_for_update()
    )
    if mfa is not None:
        if not totp_code:
            raise AuthenticationError(GENERIC_LOGIN_FAILURE)
        secret = decrypt_secret(settings.master_key, mfa.encrypted_secret, context=user.id.encode())
        totp = pyotp.TOTP(secret.decode())
        matched_counter: int | None = None
        now = await _database_utc_now(session)
        for offset in (-1, 0, 1):
            candidate_time = now + timedelta(seconds=offset * totp.interval)
            if hmac.compare_digest(totp.at(candidate_time), totp_code):
                matched_counter = totp.timecode(candidate_time)
                break
        if matched_counter is None or (
            mfa.last_counter is not None and matched_counter <= mfa.last_counter
        ):
            raise AuthenticationError(GENERIC_LOGIN_FAILURE)
        mfa.last_counter = matched_counter
    return user


def _login_key_hash(settings: Settings, scope: str, value: str) -> str:
    message = f"pm-login-throttle/1/{scope}\0{value}".encode()
    return hmac.new(settings.session_key, message, hashlib.sha256).hexdigest()


def login_source(request: Request) -> str:
    """Return only the directly connected peer; untrusted forwarding headers are ignored."""

    host = request.client.host if request.client is not None else "unavailable"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "unavailable"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.compressed
    return address.compressed


async def _lock_login_throttle(
    session: AsyncSession,
    *,
    scope: str,
    key_hash: str,
    now: datetime,
) -> LoginThrottle:
    values = {
        "scope": scope,
        "key_hash": key_hash,
        "failure_count": 0,
        "window_started_at": now,
        "updated_at": now,
    }
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        await session.execute(
            postgresql_insert(LoginThrottle).values(values).on_conflict_do_nothing()
        )
    elif dialect_name == "sqlite":
        await session.execute(sqlite_insert(LoginThrottle).values(values).on_conflict_do_nothing())
    else:
        raise RuntimeError("login throttling requires PostgreSQL or SQLite")
    row = await session.scalar(
        select(LoginThrottle)
        .where(LoginThrottle.scope == scope, LoginThrottle.key_hash == key_hash)
        .with_for_update()
    )
    if row is None:
        raise RuntimeError("login throttle state could not be locked")
    return row


def _prepare_login_throttle(row: LoginThrottle, *, now: datetime, settings: Settings) -> None:
    locked_until = aware_utc(row.locked_until) if row.locked_until is not None else None
    window_started_at = aware_utc(row.window_started_at)
    window_cutoff = now - timedelta(minutes=settings.login_failure_window_minutes)
    if (locked_until is not None and locked_until <= now) or (
        locked_until is None and window_started_at <= window_cutoff
    ):
        row.failure_count = 0
        row.window_started_at = now
        row.last_failed_at = None
        row.locked_until = None
        row.updated_at = now


def _login_throttle_is_locked(row: LoginThrottle, *, now: datetime) -> bool:
    return row.locked_until is not None and aware_utc(row.locked_until) > now


def _record_login_failure(
    rows: tuple[LoginThrottle, LoginThrottle], *, now: datetime, settings: Settings
) -> None:
    limits = {
        "principal": settings.login_principal_max_failures,
        "source": settings.login_source_max_failures,
    }
    for row in rows:
        if row.failure_count == 0:
            row.window_started_at = now
        row.failure_count += 1
        row.last_failed_at = now
        row.updated_at = now
        if row.failure_count >= limits[row.scope]:
            row.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)


async def verify_login_attempt(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    totp_code: str | None,
    source: str,
    settings: Settings,
) -> LoginAttemptResult:
    """Authenticate while serializing failure state across all API processes."""

    normalized_email = email.lower().strip()
    now = await _database_utc_now(session)
    principal_hash = _login_key_hash(settings, "principal", normalized_email)
    source_row = await _lock_login_throttle(
        session,
        scope="source",
        key_hash=_login_key_hash(settings, "source", source),
        now=now,
    )
    _prepare_login_throttle(source_row, now=now, settings=settings)
    if _login_throttle_is_locked(source_row, now=now):
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return LoginAttemptResult(
            user=None,
            principal_hash=principal_hash,
            rate_limited=True,
        )
    principal = await _lock_login_throttle(
        session,
        scope="principal",
        key_hash=principal_hash,
        now=now,
    )
    _prepare_login_throttle(principal, now=now, settings=settings)
    if _login_throttle_is_locked(principal, now=now):
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return LoginAttemptResult(user=None, principal_hash=principal_hash, rate_limited=True)
    rows = (principal, source_row)
    try:
        user = await verify_login(session, normalized_email, password, totp_code, settings)
    except AuthenticationError:
        _record_login_failure(rows, now=now, settings=settings)
        return LoginAttemptResult(
            user=None,
            principal_hash=principal_hash,
            rate_limited=False,
        )
    principal.failure_count = 0
    principal.window_started_at = now
    principal.last_failed_at = None
    principal.locked_until = None
    principal.updated_at = now
    return LoginAttemptResult(user=user, principal_hash=principal_hash, rate_limited=False)


async def _permissions(session: AsyncSession, user_id: str) -> frozenset[str]:
    values = (
        await session.scalars(
            select(role_permissions.c.permission_name)
            .join(Role, Role.id == role_permissions.c.role_id)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .where(user_roles.c.user_id == user_id)
        )
    ).all()
    return frozenset(values)


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    pm_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> CurrentUser:
    if not pm_session:
        raise AuthenticationError("authentication required")
    now = await _database_utc_now(session)
    idle_cutoff = now - timedelta(minutes=settings.session_idle_minutes)
    row = (
        await session.execute(
            update(Session)
            .where(
                Session.token_hash == token_hash(pm_session),
                Session.revoked_at.is_(None),
                Session.expires_at > now,
                Session.last_seen_at > idle_cutoff,
            )
            .values(last_seen_at=now)
            .returning(Session.id, Session.user_id, Session.csrf_hash)
        )
    ).one_or_none()
    if row is None:
        raise AuthenticationError("session is invalid or expired")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        csrf_cookie = request.cookies.get(CSRF_COOKIE) or ""
        presented_csrf = csrf_header or ""
        cookie_matches = secrets.compare_digest(csrf_cookie, presented_csrf)
        stored_matches = secrets.compare_digest(row.csrf_hash, token_hash(presented_csrf))
        if not csrf_cookie or not presented_csrf or not cookie_matches or not stored_matches:
            raise PermissionDenied("CSRF validation failed")
    user = await session.get(User, row.user_id)
    if user is None or not user.enabled or user.deleted_at is not None:
        raise AuthenticationError("user is disabled")
    permissions = await _permissions(session, user.id)
    await session.commit()
    return CurrentUser(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        permissions=permissions,
        session_id=row.id,
    )


def require_permission(permission: str):  # type: ignore[no-untyped-def]
    async def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if permission not in user.permissions:
            raise PermissionDenied(f"permission required: {permission}")
        return user

    return dependency


async def revoke_session(session: AsyncSession, session_id: str, user_id: str) -> None:
    row = await session.scalar(
        select(Session)
        .where(Session.id == session_id, Session.user_id == user_id)
        .with_for_update()
    )
    if row is None:
        raise AuthenticationError("session does not exist")
    row.revoked_at = await _database_utc_now(session)
