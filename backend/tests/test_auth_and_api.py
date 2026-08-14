from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from backend.app.config import get_settings
from backend.app.main import session_factory
from backend.app.models import MfaCredential, Session, User
from backend.app.security.crypto import encrypt_secret
from sqlalchemy import select


@pytest.mark.asyncio
async def test_bootstrap_is_one_time_and_session_is_csrf_protected(client) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "email": "owner@example.com",
        "display_name": "Owner",
        "password": "correct horse battery staple 2026!",
        "home_name": "Home",
        "timezone": "America/Los_Angeles",
    }
    first = await client.post("/api/v1/auth/bootstrap", json=payload)
    assert first.status_code == 201
    second = await client.post(
        "/api/v1/auth/bootstrap", json={**payload, "email": "other@example.com"}
    )
    assert second.status_code == 409
    no_csrf = await client.post(
        "/api/v1/enrollment-tokens",
        json={
            "home_id": "00000000-0000-0000-0000-000000000000",
            "friendly_name": "Sensor",
            "ct_rating_a": "100",
            "pzem_variant": "pzem004t-v4-classic-candidate",
        },
    )
    assert no_csrf.status_code == 403


@pytest.mark.asyncio
async def test_last_owner_cannot_be_disabled(owner_client) -> None:  # type: ignore[no-untyped-def]
    me = (await owner_client.get("/api/v1/auth/me")).json()
    response = await owner_client.patch(f"/api/v1/users/{me['id']}", json={"enabled": False})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_history_empty_has_an_explicit_missing_range(owner_client) -> None:  # type: ignore[no-untyped-def]
    response = await owner_client.get(
        "/api/v1/history",
        params={
            "from": datetime(2026, 8, 13, tzinfo=UTC).isoformat(),
            "to": (datetime(2026, 8, 13, tzinfo=UTC) + timedelta(hours=1)).isoformat(),
            "metric": "power",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["points"]
    assert all(point["value"] is None for point in response.json()["points"])
    assert response.json()["missing_ranges"]


def test_no_historical_bill_or_comparison_api() -> None:
    from backend.app.main import app

    paths = " ".join(app.openapi()["paths"]).lower()
    assert "historical-bill" not in paths
    assert "bill-comparison" not in paths
    assert "reconciliation" not in paths


@pytest.mark.asyncio
async def test_session_revocation_invalidates_cookie(owner_client) -> None:  # type: ignore[no-untyped-def]
    sessions = await owner_client.get("/api/v1/auth/sessions")
    assert sessions.status_code == 200
    current = next(item for item in sessions.json()["sessions"] if item["current"])
    revoked = await owner_client.delete(f"/api/v1/auth/sessions/{current['id']}")
    assert revoked.status_code == 204
    assert (await owner_client.get("/api/v1/auth/me")).status_code == 401


@pytest.mark.asyncio
async def test_mfa_code_cannot_be_replayed(client) -> None:  # type: ignore[no-untyped-def]
    bootstrap = await client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "mfa-owner@example.com",
            "display_name": "MFA Owner",
            "password": "correct horse battery staple 2026!",
            "home_name": "MFA Home",
            "timezone": "America/Los_Angeles",
        },
    )
    assert bootstrap.status_code == 201
    secret = pyotp.random_base32()
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "mfa-owner@example.com"))
        assert user is not None
        session.add(
            MfaCredential(
                user_id=user.id,
                encrypted_secret=encrypt_secret(
                    get_settings().master_key, secret.encode(), context=user.id.encode()
                ),
                enabled_at=datetime.now(UTC),
            )
        )
        await session.commit()
        user_id = user.id
    client.cookies.clear()
    code = pyotp.TOTP(secret).now()
    credentials = {
        "email": "mfa-owner@example.com",
        "password": "correct horse battery staple 2026!",
        "totp_code": code,
    }
    first = await client.post("/api/v1/auth/login", json=credentials)
    assert first.status_code == 200, first.text
    client.cookies.clear()
    replay = await client.post("/api/v1/auth/login", json=credentials)
    assert replay.status_code == 401
    async with session_factory() as session:
        assert await session.scalar(select(Session.id).where(Session.user_id == user_id))
