from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from backend.app.main import app, session_factory
from backend.app.models import AuditEvent, Session, User
from backend.app.security.passwords import verify_password
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

OWNER_PASSWORD = "correct horse battery staple 2026!"


@pytest.mark.asyncio
async def test_self_profile_preferences_and_email_change_are_audited_and_fail_closed(
    owner_client,  # type: ignore[no-untyped-def]
) -> None:
    profile = await owner_client.get("/api/v1/auth/profile")
    assert profile.status_code == 200
    assert profile.json()["display_name"] == "Owner"

    defaults = await owner_client.get("/api/v1/auth/preferences")
    assert defaults.status_code == 200
    assert defaults.json()["preferences"]["refresh_seconds"] == 60

    preferences = {
        "dashboard_range": "week",
        "history_range": "billing_cycle",
        "refresh_seconds": 120,
        "power_unit": "W",
        "energy_unit": "Wh",
        "date_format": "iso",
        "time_format": "24h",
        "decimal_precision": 3,
        "density": "compact",
        "dashboard_cards": ["live_power", "energy", "alerts"],
    }
    saved = await owner_client.put("/api/v1/auth/preferences", json=preferences)
    assert saved.status_code == 200, saved.text
    assert saved.json()["preferences"] == preferences
    assert (
        await owner_client.put(
            "/api/v1/auth/preferences", json={**preferences, "unknown": "rejected"}
        )
    ).status_code == 422

    renamed = await owner_client.patch(
        "/api/v1/auth/profile", json={"display_name": "  Primary   Owner  "}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "Primary Owner"
    assert renamed.json()["session_revoked"] is False

    denied = await owner_client.patch(
        "/api/v1/auth/profile",
        json={"email": "new-owner@example.com", "current_password": "wrong password"},
    )
    assert denied.status_code == 401
    changed = await owner_client.patch(
        "/api/v1/auth/profile",
        json={"email": "New-Owner@Example.com", "current_password": OWNER_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["email"] == "new-owner@example.com"
    assert changed.json()["session_revoked"] is True
    assert (await owner_client.get("/api/v1/auth/me")).status_code == 401

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "new-owner@example.com"))
        assert user is not None
        assert user.display_name == "Primary Owner"
        assert user.preferences == preferences
        events = set(
            (
                await session.scalars(
                    select(AuditEvent.event_code).where(AuditEvent.target_id == user.id)
                )
            ).all()
        )
        assert {
            "USER_DISPLAY_PREFERENCES_UPDATED",
            "USER_PROFILE_UPDATED",
        }.issubset(events)
        assert (
            await session.scalar(
                select(Session.id).where(Session.user_id == user.id, Session.revoked_at.is_(None))
            )
            is None
        )


@pytest.mark.asyncio
async def test_admin_user_lifecycle_normalizes_email_and_revokes_sessions(
    owner_client,  # type: ignore[no-untyped-def]
) -> None:
    roles = (await owner_client.get("/api/v1/roles")).json()["roles"]
    role_name = next(role["name"] for role in roles if role["name"] != "Owner")
    created = await owner_client.post(
        "/api/v1/users",
        json={
            "email": "operator@example.com",
            "display_name": "Operator",
            "password": "operator initial password 2026!",
            "role_names": [role_name],
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    updated = await owner_client.patch(
        f"/api/v1/users/{user_id}",
        json={
            "email": "Renamed-Operator@Example.com",
            "display_name": "  Kitchen   Operator  ",
            "role_names": [role_name],
            "enabled": True,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["email"] == "renamed-operator@example.com"
    assert updated.json()["display_name"] == "Kitchen Operator"

    duplicate = await owner_client.patch(
        f"/api/v1/users/{user_id}", json={"email": "OWNER@EXAMPLE.COM"}
    )
    assert duplicate.status_code == 409

    async with session_factory() as session:
        session.add(
            Session(
                user_id=user_id,
                token_hash="a" * 64,
                csrf_hash="b" * 64,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()

    replacement = "operator replacement password 2026!"
    reset = await owner_client.post(
        f"/api/v1/users/{user_id}/reset-password",
        json={"new_password": replacement},
    )
    assert reset.status_code == 204, reset.text

    disabled = await owner_client.patch(f"/api/v1/users/{user_id}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://powermeter.test"
    ) as login_client:
        assert (
            await login_client.post(
                "/api/v1/auth/login",
                json={"email": "renamed-operator@example.com", "password": replacement},
            )
        ).status_code == 401
    assert (
        await owner_client.patch(f"/api/v1/users/{user_id}", json={"enabled": True})
    ).status_code == 200

    deleted = await owner_client.delete(f"/api/v1/users/{user_id}")
    assert deleted.status_code == 204, deleted.text
    restored = await owner_client.post(f"/api/v1/users/{user_id}/restore")
    assert restored.status_code == 200, restored.text

    users = await owner_client.get("/api/v1/users")
    listed = next(user for user in users.json()["users"] if user["id"] == user_id)
    assert listed["manageable"] is True
    assert listed["created_at"]
    assert listed["last_login_at"] is not None
    assert listed["deleted_at"] is None
    assert listed["enabled"] is True

    async with session_factory() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert verify_password(user.password_hash, replacement)
        assert (
            await session.scalar(
                select(Session.id).where(Session.user_id == user_id, Session.revoked_at.is_(None))
            )
            is None
        )
        events = set(
            (
                await session.scalars(
                    select(AuditEvent.event_code).where(AuditEvent.target_id == user_id)
                )
            ).all()
        )
        assert {
            "USER_CREATED",
            "USER_UPDATED",
            "USER_PASSWORD_RESET",
            "USER_SOFT_DELETED",
            "USER_RESTORED",
        }.issubset(events)
        serialized_details = " ".join(
            str(details)
            for details in (
                await session.scalars(
                    select(AuditEvent.details).where(AuditEvent.target_id == user_id)
                )
            ).all()
        )
        assert replacement not in serialized_details


@pytest.mark.asyncio
async def test_regular_user_self_service_cannot_bypass_admin_authorization(
    owner_client,  # type: ignore[no-untyped-def]
) -> None:
    created = await owner_client.post(
        "/api/v1/users",
        json={
            "email": "viewer@example.com",
            "display_name": "Viewer",
            "password": "viewer initial password 2026!",
            "role_names": ["Viewer"],
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    regular = AsyncClient(transport=ASGITransport(app=app), base_url="https://powermeter.test")
    try:
        login = await regular.post(
            "/api/v1/auth/login",
            json={"email": "viewer@example.com", "password": "viewer initial password 2026!"},
        )
        assert login.status_code == 200, login.text
        regular.headers["X-CSRF-Token"] = regular.cookies["pm_csrf"]

        assert (await regular.get("/api/v1/auth/profile")).status_code == 200
        renamed = await regular.patch(
            "/api/v1/auth/profile", json={"display_name": "Household viewer"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["display_name"] == "Household viewer"

        assert (await regular.get("/api/v1/users")).status_code == 403
        assert (
            await regular.patch(f"/api/v1/users/{user_id}", json={"role_names": ["Owner"]})
        ).status_code == 403
        assert (
            await regular.patch("/api/v1/auth/profile", json={"role_names": ["Owner"]})
        ).status_code == 422
        assert (
            await regular.post(
                f"/api/v1/users/{user_id}/reset-password",
                json={"new_password": "unauthorized replacement 2026!"},
            )
        ).status_code == 403
        assert (await regular.delete(f"/api/v1/users/{user_id}")).status_code == 403

        changed = await regular.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "viewer initial password 2026!",
                "new_password": "viewer replacement password 2026!",
            },
        )
        assert changed.status_code == 204, changed.text
        assert (await regular.get("/api/v1/auth/me")).status_code == 401
    finally:
        await regular.aclose()


@pytest.mark.asyncio
async def test_last_active_owner_cannot_be_demoted_disabled_or_deleted(
    owner_client,  # type: ignore[no-untyped-def]
) -> None:
    owner_id = (await owner_client.get("/api/v1/auth/profile")).json()["id"]
    assert (
        await owner_client.patch(f"/api/v1/users/{owner_id}", json={"role_names": ["Viewer"]})
    ).status_code == 409
    assert (
        await owner_client.patch(f"/api/v1/users/{owner_id}", json={"enabled": False})
    ).status_code == 409
    assert (await owner_client.delete(f"/api/v1/users/{owner_id}")).status_code == 409
