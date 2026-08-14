from __future__ import annotations

from datetime import UTC, datetime

import pyotp
from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db import get_session
from ..errors import AuthenticationError, IntegrityConflict, NotFound, PermissionDenied
from ..models import (
    AuditEvent,
    Home,
    MfaCredential,
    Role,
    Session,
    User,
    role_permissions,
    user_home_scopes,
    user_roles,
)
from ..schemas.api import (
    AdminPasswordResetRequest,
    MfaConfirmRequest,
    PasswordChangeRequest,
    RoleCreateRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from ..security.auth import ALL_PERMISSIONS, CurrentUser, require_permission
from ..security.crypto import encrypt_secret
from ..security.passwords import hash_password, verify_password

router = APIRouter(prefix="/api/v1", tags=["users"])


async def _home_ids(session: AsyncSession, user_id: str) -> tuple[str, ...]:
    return tuple(
        (
            await session.scalars(
                select(user_home_scopes.c.home_id).where(user_home_scopes.c.user_id == user_id)
            )
        ).all()
    )


async def _role_ids(
    session: AsyncSession, names: list[str], actor_permissions: frozenset[str]
) -> list[str]:
    rows = (await session.scalars(select(Role).where(Role.name.in_(set(names))))).all()
    if {row.name for row in rows} != set(names):
        raise NotFound("one or more roles do not exist")
    permissions = set(
        (
            await session.scalars(
                select(role_permissions.c.permission_name).where(
                    role_permissions.c.role_id.in_([row.id for row in rows])
                )
            )
        ).all()
    )
    if not permissions.issubset(actor_permissions):
        raise PermissionDenied("cannot grant permissions the actor does not possess")
    return [row.id for row in rows]


async def _user_permissions(session: AsyncSession, user_id: str) -> frozenset[str]:
    return frozenset(
        (
            await session.scalars(
                select(role_permissions.c.permission_name)
                .join(Role, Role.id == role_permissions.c.role_id)
                .join(user_roles, user_roles.c.role_id == Role.id)
                .where(user_roles.c.user_id == user_id)
            )
        ).all()
    )


async def _scoped_mutation_target(
    session: AsyncSession, actor: CurrentUser, user_id: str
) -> tuple[User, tuple[str, ...]]:
    actor_homes = set(await _home_ids(session, actor.id))
    row = await session.scalar(
        select(User)
        .where(
            User.id == user_id,
            exists(
                select(1).where(
                    user_home_scopes.c.user_id == User.id,
                    user_home_scopes.c.home_id.in_(actor_homes),
                )
            ),
        )
        .with_for_update()
    )
    if row is None:
        raise NotFound("user does not exist")
    target_homes = await _home_ids(session, row.id)
    # User enablement, credentials, and roles are global to the identity. An
    # overlapping home is enough for visibility, but not for a mutation that
    # would also affect a home outside the actor's authority.
    if not target_homes or not set(target_homes).issubset(actor_homes):
        raise NotFound("user does not exist")
    if not (await _user_permissions(session, row.id)).issubset(actor.permissions):
        raise PermissionDenied("cannot manage a user with broader permissions")
    return row, target_homes


async def _protect_home_owners(session: AsyncSession, target_homes: tuple[str, ...]) -> None:
    # Lock every affected home in deterministic order so two concurrent owner
    # removals cannot both observe a stale owner count.
    await session.scalars(
        select(Home.id).where(Home.id.in_(target_homes)).order_by(Home.id).with_for_update()
    )
    count_rows = (
        await session.execute(
            select(
                user_home_scopes.c.home_id,
                func.count(func.distinct(User.id)),
            )
            .select_from(
                user_home_scopes.join(User, User.id == user_home_scopes.c.user_id)
                .join(user_roles, user_roles.c.user_id == User.id)
                .join(Role, Role.id == user_roles.c.role_id)
            )
            .where(
                user_home_scopes.c.home_id.in_(target_homes),
                Role.name == "Owner",
                User.enabled.is_(True),
                User.deleted_at.is_(None),
            )
            .group_by(user_home_scopes.c.home_id)
        )
    ).all()
    counts: dict[str, int] = {str(result[0]): int(result[1]) for result in count_rows}
    if any(int(counts.get(home_id, 0)) <= 1 for home_id in target_homes):
        raise IntegrityConflict("the last enabled owner for a home cannot be removed or disabled")


async def _is_owner(session: AsyncSession, user_id: str) -> bool:
    return (
        await session.scalar(
            select(Role.id)
            .join(user_roles, user_roles.c.role_id == Role.id)
            .where(user_roles.c.user_id == user_id, Role.name == "Owner")
        )
        is not None
    )


@router.get("/users")
async def list_users(
    actor: CurrentUser = Depends(require_permission("users.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    actor_homes = set(await _home_ids(session, actor.id))
    users = (
        await session.scalars(
            select(User)
            .where(
                exists(
                    select(1).where(
                        user_home_scopes.c.user_id == User.id,
                        user_home_scopes.c.home_id.in_(actor_homes),
                    )
                )
            )
            .order_by(User.email)
        )
    ).all()
    output: list[dict[str, object]] = []
    for row in users:
        roles = (
            await session.scalars(
                select(Role.name)
                .join(user_roles, user_roles.c.role_id == Role.id)
                .where(user_roles.c.user_id == row.id)
            )
        ).all()
        visible_homes = sorted(set(await _home_ids(session, row.id)) & actor_homes)
        manageable = set(await _home_ids(session, row.id)).issubset(actor_homes) and (
            await _user_permissions(session, row.id)
        ).issubset(actor.permissions)
        output.append(
            {
                "id": row.id,
                "email": row.email,
                "display_name": row.display_name,
                "enabled": row.enabled,
                "deleted_at": row.deleted_at,
                "roles": sorted(roles),
                "home_ids": visible_homes,
                "manageable": manageable,
            }
        )
    return {"users": output}


@router.post("/users", status_code=201)
async def create_user(
    payload: UserCreateRequest,
    request: Request,
    actor: CurrentUser = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    actor_homes = set(await _home_ids(session, actor.id))
    requested_homes = actor_homes if payload.home_ids is None else set(payload.home_ids)
    if not requested_homes or not requested_homes.issubset(actor_homes):
        raise NotFound("one or more homes do not exist")
    if await session.scalar(select(User.id).where(User.email == payload.email.lower())):
        raise IntegrityConflict("user could not be created")
    role_ids = await _role_ids(session, payload.role_names, actor.permissions)
    row = User(
        email=payload.email.lower(),
        display_name=payload.display_name,
        password_hash=hash_password(payload.password),
    )
    session.add(row)
    await session.flush()
    for role_id in role_ids:
        await session.execute(user_roles.insert().values(user_id=row.id, role_id=role_id))
    for home_id in sorted(requested_homes):
        await session.execute(user_home_scopes.insert().values(user_id=row.id, home_id=home_id))
    session.add(
        AuditEvent(
            actor_user_id=actor.id,
            event_code="USER_CREATED",
            target_type="user",
            target_id=row.id,
            correlation_id=request.state.correlation_id,
            details={
                "roles": sorted(payload.role_names),
                "home_scope_count": len(requested_homes),
            },
        )
    )
    await session.commit()
    return {
        "id": row.id,
        "email": row.email,
        "display_name": row.display_name,
        "home_ids": sorted(requested_homes),
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: UserUpdateRequest,
    request: Request,
    actor: CurrentUser = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    row, target_homes = await _scoped_mutation_target(session, actor, user_id)
    was_owner = await _is_owner(session, row.id)
    role_ids: list[str] | None = None
    role_names = payload.role_names
    if role_names is not None:
        role_ids = await _role_ids(session, role_names, actor.permissions)
    removes_owner = was_owner and role_names is not None and "Owner" not in role_names
    disables_owner = was_owner and payload.enabled is False
    if removes_owner or disables_owner:
        await _protect_home_owners(session, target_homes)
    if payload.display_name is not None:
        row.display_name = payload.display_name
    if payload.enabled is not None:
        row.enabled = payload.enabled
        if not payload.enabled:
            await session.execute(
                update(Session)
                .where(Session.user_id == row.id, Session.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
    if role_ids is not None:
        await session.execute(delete(user_roles).where(user_roles.c.user_id == row.id))
        for role_id in role_ids:
            await session.execute(user_roles.insert().values(user_id=row.id, role_id=role_id))
    session.add(
        AuditEvent(
            actor_user_id=actor.id,
            event_code="USER_UPDATED",
            target_type="user",
            target_id=row.id,
            correlation_id=request.state.correlation_id,
            details={"enabled": payload.enabled, "roles": role_names},
        )
    )
    await session.commit()
    return {"id": row.id, "enabled": row.enabled, "display_name": row.display_name}


@router.delete("/users/{user_id}", status_code=204)
async def soft_delete_user(
    user_id: str,
    actor: CurrentUser = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row, target_homes = await _scoped_mutation_target(session, actor, user_id)
    if await _is_owner(session, row.id):
        await _protect_home_owners(session, target_homes)
    row.deleted_at = datetime.now(UTC)
    row.enabled = False
    await session.execute(
        update(Session)
        .where(Session.user_id == row.id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    session.add(
        AuditEvent(
            actor_user_id=actor.id,
            event_code="USER_SOFT_DELETED",
            target_type="user",
            target_id=row.id,
            details={},
        )
    )
    await session.commit()


@router.post("/users/{user_id}/restore")
async def restore_user(
    user_id: str,
    actor: CurrentUser = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    row, _target_homes = await _scoped_mutation_target(session, actor, user_id)
    row.deleted_at = None
    row.enabled = True
    session.add(
        AuditEvent(
            actor_user_id=actor.id,
            event_code="USER_RESTORED",
            target_type="user",
            target_id=row.id,
            details={},
        )
    )
    await session.commit()
    return {"id": row.id, "enabled": row.enabled}


@router.post("/auth/change-password", status_code=204)
async def change_password(
    payload: PasswordChangeRequest,
    actor: CurrentUser = Depends(require_permission("dashboard.view")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row = await session.get(User, actor.id)
    if row is None or not verify_password(row.password_hash, payload.current_password):
        raise AuthenticationError("current password is invalid")
    row.password_hash = hash_password(payload.new_password)
    await session.execute(
        update(Session)
        .where(
            Session.user_id == actor.id,
            Session.id != actor.session_id,
            Session.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()


@router.post("/users/{user_id}/reset-password", status_code=204)
async def reset_password(
    user_id: str,
    payload: AdminPasswordResetRequest,
    actor: CurrentUser = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    row, _target_homes = await _scoped_mutation_target(session, actor, user_id)
    row.password_hash = hash_password(payload.new_password)
    await session.execute(
        update(Session)
        .where(Session.user_id == row.id, Session.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    session.add(
        AuditEvent(
            actor_user_id=actor.id,
            event_code="USER_PASSWORD_RESET",
            target_type="user",
            target_id=row.id,
            details={"sessions_revoked": True},
        )
    )
    await session.commit()


@router.post("/auth/mfa/setup")
async def setup_mfa(
    actor: CurrentUser = Depends(require_permission("dashboard.view")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    secret = pyotp.random_base32()
    credential = await session.scalar(
        select(MfaCredential).where(MfaCredential.user_id == actor.id)
    )
    encrypted = encrypt_secret(settings.master_key, secret.encode(), context=actor.id.encode())
    if credential is None:
        credential = MfaCredential(user_id=actor.id, encrypted_secret=encrypted)
        session.add(credential)
    else:
        credential.encrypted_secret = encrypted
        credential.enabled_at = None
    await session.commit()
    uri = pyotp.TOTP(secret).provisioning_uri(name=actor.email, issuer_name="PowerMeter V2")
    return {"secret": secret, "provisioning_uri": uri}


@router.post("/auth/mfa/confirm", status_code=204)
async def confirm_mfa(
    payload: MfaConfirmRequest,
    actor: CurrentUser = Depends(require_permission("dashboard.view")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    from ..security.crypto import decrypt_secret

    credential = await session.scalar(
        select(MfaCredential).where(MfaCredential.user_id == actor.id)
    )
    if credential is None:
        raise NotFound("MFA setup does not exist")
    secret = decrypt_secret(
        settings.master_key, credential.encrypted_secret, context=actor.id.encode()
    )
    totp = pyotp.TOTP(secret.decode())
    now = datetime.now(UTC)
    if not totp.verify(payload.code, for_time=now, valid_window=1):
        raise AuthenticationError("MFA code is invalid")
    credential.enabled_at = datetime.now(UTC)
    credential.last_counter = totp.timecode(now)
    await session.commit()


@router.get("/roles")
async def list_roles(
    actor: CurrentUser = Depends(require_permission("users.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    roles = (await session.scalars(select(Role).order_by(Role.name))).all()
    output = []
    for role in roles:
        permissions = (
            await session.scalars(
                select(role_permissions.c.permission_name).where(
                    role_permissions.c.role_id == role.id
                )
            )
        ).all()
        output.append(
            {
                "id": role.id,
                "name": role.name,
                "built_in": role.built_in,
                "description": role.description,
                "permissions": sorted(permissions),
                "assignable": set(permissions).issubset(actor.permissions),
            }
        )
    return {"roles": output, "available_permissions": list(ALL_PERMISSIONS)}


@router.post("/roles", status_code=201)
async def create_role(
    payload: RoleCreateRequest,
    actor: CurrentUser = Depends(require_permission("users.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    unknown = set(payload.permissions) - set(ALL_PERMISSIONS)
    if unknown:
        raise NotFound("one or more permissions do not exist")
    if not set(payload.permissions).issubset(actor.permissions):
        raise PermissionDenied("cannot create a role with permissions the actor does not possess")
    if await session.scalar(select(Role.id).where(Role.name == payload.name)):
        raise IntegrityConflict("role name already exists")
    role = Role(name=payload.name, description=payload.description, built_in=False)
    session.add(role)
    await session.flush()
    for permission in payload.permissions:
        await session.execute(
            role_permissions.insert().values(role_id=role.id, permission_name=permission)
        )
    session.add(
        AuditEvent(
            actor_user_id=actor.id,
            event_code="CUSTOM_ROLE_CREATED",
            target_type="role",
            target_id=role.id,
            details={"permissions": sorted(payload.permissions)},
        )
    )
    await session.commit()
    return {"id": role.id, "name": role.name}
