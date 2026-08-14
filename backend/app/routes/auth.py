from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_session
from ..errors import AuthenticationError, IntegrityConflict
from ..models import (
    AuditEvent,
    Home,
    Session,
    User,
    UtilityAccount,
    user_home_scopes,
    user_roles,
)
from ..schemas.api import BootstrapRequest, LoginRequest
from ..security.auth import (
    CSRF_COOKIE,
    GENERIC_LOGIN_FAILURE,
    SESSION_COOKIE,
    CurrentUser,
    create_session,
    current_user,
    login_source,
    revoke_session,
    seed_access_control,
    verify_login_attempt,
)
from ..security.passwords import hash_password

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
BOOTSTRAP_ADVISORY_LOCK_ID = 0x504D4232


def _set_session_cookies(response: Response, token: str, csrf: str, settings: Settings) -> None:
    secure = settings.public_origin.scheme == "https"
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
        max_age=settings.session_absolute_hours * 3600,
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
        max_age=settings.session_absolute_hours * 3600,
    )


@router.get("/bootstrap/status")
async def bootstrap_status(session: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    count = int(await session.scalar(select(func.count(User.id))) or 0)
    return {"required": count == 0}


@router.post("/bootstrap", status_code=201)
async def bootstrap_owner(
    payload: BootstrapRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    async with session.begin():
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": BOOTSTRAP_ADVISORY_LOCK_ID},
            )
        count = int(await session.scalar(select(func.count(User.id))) or 0)
        if count:
            raise IntegrityConflict("first-run owner bootstrap has already completed")
        roles = await seed_access_control(session)
        home = Home(name=payload.home_name, timezone=payload.timezone)
        user = User(
            email=payload.email.lower(),
            display_name=payload.display_name,
            password_hash=hash_password(payload.password),
        )
        session.add_all((home, user))
        await session.flush()
        session.add(
            UtilityAccount(
                home_id=home.id,
                utility_name="Southern California Edison",
                timezone=payload.timezone,
                billing_day=1,
                cost_scope="energy_only",
            )
        )
        await session.execute(
            user_roles.insert().values(user_id=user.id, role_id=roles["Owner"].id)
        )
        await session.execute(user_home_scopes.insert().values(user_id=user.id, home_id=home.id))
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                event_code="FIRST_RUN_OWNER_CREATED",
                target_type="home",
                target_id=home.id,
                correlation_id=request.state.correlation_id,
                details={},
            )
        )
        _row, token, csrf = await create_session(
            session,
            user,
            settings=settings,
            fingerprint=request.headers.get("user-agent"),
        )
    _set_session_cookies(response, token, csrf, settings)
    return {"user": {"id": user.id, "email": user.email, "display_name": user.display_name}}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    authenticated_user: User | None = None
    token: str | None = None
    csrf: str | None = None
    async with session.begin():
        attempt = await verify_login_attempt(
            session,
            email=payload.email,
            password=payload.password,
            totp_code=payload.totp_code,
            source=login_source(request),
            settings=settings,
        )
        if attempt.user is None:
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_code=(
                        "USER_LOGIN_RATE_LIMITED" if attempt.rate_limited else "USER_LOGIN_FAILED"
                    ),
                    target_type="login_principal_hash",
                    target_id=attempt.principal_hash,
                    correlation_id=request.state.correlation_id,
                    details={},
                )
            )
        else:
            authenticated_user = attempt.user
            row, token, csrf = await create_session(
                session,
                authenticated_user,
                settings=settings,
                fingerprint=request.headers.get("user-agent"),
            )
            session.add(
                AuditEvent(
                    actor_user_id=authenticated_user.id,
                    event_code="USER_LOGIN_SUCCEEDED",
                    target_type="session",
                    target_id=row.id,
                    correlation_id=request.state.correlation_id,
                    details={},
                )
            )
    if authenticated_user is None or token is None or csrf is None:
        raise AuthenticationError(GENERIC_LOGIN_FAILURE)
    _set_session_cookies(response, token, csrf, settings)
    return {
        "user": {
            "id": authenticated_user.id,
            "email": authenticated_user.email,
            "display_name": authenticated_user.display_name,
        }
    }


@router.get("/me")
async def me(user: CurrentUser = Depends(current_user)) -> dict[str, object]:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "permissions": sorted(user.permissions),
    }


@router.get("/sessions")
async def list_sessions(
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    rows = (
        await session.scalars(
            select(Session).where(Session.user_id == user.id).order_by(Session.created_at.desc())
        )
    ).all()
    return {
        "sessions": [
            {
                "id": row.id,
                "created_at": row.created_at,
                "last_seen_at": row.last_seen_at,
                "expires_at": row.expires_at,
                "revoked_at": row.revoked_at,
                "current": row.id == user.session_id,
            }
            for row in rows
        ]
    }


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    async with session.begin():
        await revoke_session(session, session_id, user.id)
    return Response(status_code=204)


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    async with session.begin():
        await revoke_session(session, user.session_id, user.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    response.status_code = 204
    return response
