from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from backend.app.config import Settings, get_settings
from backend.app.main import app, session_factory
from backend.app.models import AuditEvent, LoginThrottle, Session, aware_utc
from backend.app.security import auth as auth_security
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError
from sqlalchemy import func, select

OWNER_EMAIL = "session-owner@example.com"
OWNER_PASSWORD = "correct horse battery staple 2026!"
INVALID_PASSWORD = "this password is intentionally invalid"


@pytest.fixture
def auth_settings() -> Iterator[Settings]:
    settings = Settings(
        env="test",
        session_absolute_hours=12,
        session_idle_minutes=30,
        login_failure_window_minutes=15,
        login_lockout_minutes=15,
        login_principal_max_failures=2,
        login_source_max_failures=20,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield settings
    finally:
        app.dependency_overrides.pop(get_settings, None)


async def _bootstrap(client: AsyncClient) -> Response:
    response = await client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": OWNER_EMAIL,
            "display_name": "Session Owner",
            "password": OWNER_PASSWORD,
            "home_name": "Authentication Test Home",
            "timezone": "America/Los_Angeles",
        },
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = client.cookies["pm_csrf"]
    return response


async def _only_session() -> Session:
    async with session_factory() as session:
        rows = (await session.scalars(select(Session))).all()
        assert len(rows) == 1
        return rows[0]


def _generic_failure(response) -> tuple[int, str, str]:  # type: ignore[no-untyped-def]
    body = response.json()
    return response.status_code, body["code"], body["detail"]


def test_auth_setting_relationships_are_fail_closed() -> None:
    with pytest.raises(ValidationError, match="PM_SESSION_IDLE_MINUTES"):
        Settings(env="test", session_absolute_hours=1, session_idle_minutes=61)
    with pytest.raises(ValidationError, match="PM_LOGIN_SOURCE_MAX_FAILURES"):
        Settings(
            env="test",
            login_principal_max_failures=10,
            login_source_max_failures=9,
        )


def test_auth_environment_variable_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PM_SESSION_ABSOLUTE_HOURS", "24")
    monkeypatch.setenv("PM_SESSION_IDLE_MINUTES", "45")
    monkeypatch.setenv("PM_LOGIN_FAILURE_WINDOW_MINUTES", "12")
    monkeypatch.setenv("PM_LOGIN_LOCKOUT_MINUTES", "20")
    monkeypatch.setenv("PM_LOGIN_PRINCIPAL_MAX_FAILURES", "6")
    monkeypatch.setenv("PM_LOGIN_SOURCE_MAX_FAILURES", "60")
    settings = Settings()
    assert (
        settings.session_absolute_hours,
        settings.session_idle_minutes,
        settings.login_failure_window_minutes,
        settings.login_lockout_minutes,
        settings.login_principal_max_failures,
        settings.login_source_max_failures,
    ) == (24, 45, 12, 20, 6, 60)


@pytest.mark.asyncio
async def test_session_has_fixed_absolute_expiry_and_sliding_idle_touch(
    client: AsyncClient, auth_settings: Settings
) -> None:
    bootstrap = await _bootstrap(client)
    created = await _only_session()
    original_expiry = aware_utc(created.expires_at)
    prior_seen = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as session:
        row = await session.get(Session, created.id)
        assert row is not None
        row.last_seen_at = prior_seen
        await session.commit()

    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 200, response.text
    touched = await _only_session()
    assert aware_utc(touched.last_seen_at) > prior_seen
    assert aware_utc(touched.expires_at) == original_expiry
    assert timedelta(hours=11, minutes=59) < original_expiry - aware_utc(created.created_at)
    cookies = "\n".join(bootstrap.headers.get_list("set-cookie"))
    assert cookies.count("Max-Age=43200") == 2
    assert "pm_session=" in cookies and "HttpOnly" in cookies
    assert "pm_csrf=" in cookies
    assert cookies.count("SameSite=strict") == 2
    assert cookies.count("Secure") == 2
    assert auth_settings.session_absolute_hours == 12


@pytest.mark.asyncio
async def test_idle_expired_session_is_rejected_and_cannot_be_revived(
    client: AsyncClient, auth_settings: Settings
) -> None:
    await _bootstrap(client)
    created = await _only_session()
    stale_seen = datetime.now(UTC) - timedelta(
        minutes=auth_settings.session_idle_minutes, seconds=1
    )
    async with session_factory() as session:
        row = await session.get(Session, created.id)
        assert row is not None
        row.last_seen_at = stale_seen
        row.expires_at = datetime.now(UTC) + timedelta(hours=1)
        await session.commit()

    first, second = await asyncio.gather(
        client.get("/api/v1/auth/me"),
        client.get("/api/v1/auth/me"),
    )
    assert first.status_code == second.status_code == 401
    rejected = await _only_session()
    assert aware_utc(rejected.last_seen_at) == stale_seen


@pytest.mark.asyncio
async def test_absolute_expiry_wins_even_when_session_was_just_seen(
    client: AsyncClient, auth_settings: Settings
) -> None:
    await _bootstrap(client)
    created = await _only_session()
    recent_seen = datetime.now(UTC)
    async with session_factory() as session:
        row = await session.get(Session, created.id)
        assert row is not None
        row.last_seen_at = recent_seen
        # SQLite's CURRENT_TIMESTAMP has whole-second precision; use an
        # unambiguous past boundary while production PostgreSQL keeps microseconds.
        row.expires_at = recent_seen - timedelta(seconds=2)
        await session.commit()

    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    rejected = await _only_session()
    assert aware_utc(rejected.last_seen_at) == recent_seen
    assert auth_settings.session_absolute_hours == 12


@pytest.mark.asyncio
async def test_failed_csrf_comparisons_do_not_touch_last_seen(
    client: AsyncClient, auth_settings: Settings
) -> None:
    await _bootstrap(client)
    created = await _only_session()
    prior_seen = datetime.now(UTC) - timedelta(minutes=1)
    async with session_factory() as session:
        row = await session.get(Session, created.id)
        assert row is not None
        row.last_seen_at = prior_seen
        await session.commit()

    client.headers["X-CSRF-Token"] = "incorrect-csrf-value"
    response = await client.post("/api/v1/auth/logout")
    assert response.status_code == 403
    rejected = await _only_session()
    assert aware_utc(rejected.last_seen_at) == prior_seen
    assert auth_settings.session_idle_minutes == 30


@pytest.mark.asyncio
async def test_principal_lockout_is_generic_persistent_and_opaque(
    client: AsyncClient, auth_settings: Settings
) -> None:
    await _bootstrap(client)
    client.cookies.clear()
    credentials = {"email": OWNER_EMAIL, "password": INVALID_PASSWORD}
    first = await client.post("/api/v1/auth/login", json=credentials)
    second = await client.post("/api/v1/auth/login", json=credentials)
    async with session_factory() as session:
        locked = await session.scalar(
            select(LoginThrottle).where(LoginThrottle.scope == "principal")
        )
        assert locked is not None and locked.locked_until is not None
        original_locked_until = aware_utc(locked.locked_until)
    locked_correct = await client.post(
        "/api/v1/auth/login",
        json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    assert _generic_failure(first) == _generic_failure(second) == _generic_failure(locked_correct)
    assert _generic_failure(first) == (
        401,
        "AUTHENTICATION_FAILED",
        "email, password, or MFA code is invalid",
    )
    assert not first.headers.get_list("set-cookie")
    assert not second.headers.get_list("set-cookie")
    assert not locked_correct.headers.get_list("set-cookie")

    async with session_factory() as session:
        principal = await session.scalar(
            select(LoginThrottle).where(LoginThrottle.scope == "principal")
        )
        assert principal is not None
        assert principal.failure_count == auth_settings.login_principal_max_failures
        assert principal.locked_until is not None
        assert aware_utc(principal.locked_until) == original_locked_until
        assert OWNER_EMAIL not in principal.key_hash
        assert len(principal.key_hash) == 64
        codes = (
            await session.scalars(
                select(AuditEvent.event_code).where(
                    AuditEvent.event_code.in_(("USER_LOGIN_FAILED", "USER_LOGIN_RATE_LIMITED"))
                )
            )
        ).all()
        assert codes.count("USER_LOGIN_FAILED") == 2
        assert codes.count("USER_LOGIN_RATE_LIMITED") == 1


@pytest.mark.asyncio
async def test_unknown_and_known_principals_have_identical_failure_contract(
    client: AsyncClient, auth_settings: Settings
) -> None:
    await _bootstrap(client)
    client.cookies.clear()
    known = await client.post(
        "/api/v1/auth/login",
        json={"email": OWNER_EMAIL, "password": INVALID_PASSWORD},
        headers={"X-Forwarded-For": "192.0.2.10"},
    )
    unknown_email = "not-a-user@example.com"
    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": unknown_email, "password": INVALID_PASSWORD},
        headers={"X-Forwarded-For": "198.51.100.20"},
    )
    assert _generic_failure(known) == _generic_failure(unknown)
    async with session_factory() as session:
        principals = (
            await session.scalars(select(LoginThrottle).where(LoginThrottle.scope == "principal"))
        ).all()
        assert len(principals) == 2
        serialized = " ".join(row.key_hash for row in principals)
        assert OWNER_EMAIL not in serialized
        assert unknown_email not in serialized
        assert all(row.failure_count == 1 for row in principals)
        sources = (
            await session.scalars(select(LoginThrottle).where(LoginThrottle.scope == "source"))
        ).all()
        assert len(sources) == 1
        assert sources[0].failure_count == 2
    assert auth_settings.login_source_max_failures > 2


@pytest.mark.asyncio
async def test_expired_lock_allows_login_and_clears_principal_state(
    client: AsyncClient, auth_settings: Settings
) -> None:
    await _bootstrap(client)
    client.cookies.clear()
    credentials = {"email": OWNER_EMAIL, "password": INVALID_PASSWORD}
    for _ in range(auth_settings.login_principal_max_failures):
        assert (await client.post("/api/v1/auth/login", json=credentials)).status_code == 401
    async with session_factory() as session:
        principal = await session.scalar(
            select(LoginThrottle).where(LoginThrottle.scope == "principal")
        )
        assert principal is not None
        principal.locked_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    accepted = await client.post(
        "/api/v1/auth/login",
        json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
    )
    assert accepted.status_code == 200, accepted.text
    async with session_factory() as session:
        principal = await session.scalar(
            select(LoginThrottle).where(LoginThrottle.scope == "principal")
        )
        source = await session.scalar(select(LoginThrottle).where(LoginThrottle.scope == "source"))
        assert principal is not None and source is not None
        assert principal.failure_count == 0
        assert principal.locked_until is None
        assert principal.last_failed_at is None
        assert source.failure_count == auth_settings.login_principal_max_failures


@pytest.mark.asyncio
async def test_source_limit_blocks_correct_login_from_independent_client(
    client: AsyncClient, auth_settings: Settings
) -> None:
    auth_settings.login_source_max_failures = 2
    await _bootstrap(client)
    client.cookies.clear()
    for email in ("spray-one@example.com", "spray-two@example.com"):
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": INVALID_PASSWORD}
        )
        assert response.status_code == 401

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://powermeter.test"
    ) as independent_client:
        blocked = await independent_client.post(
            "/api/v1/auth/login",
            json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
        )
    assert _generic_failure(blocked) == (
        401,
        "AUTHENTICATION_FAILED",
        "email, password, or MFA code is invalid",
    )
    async with session_factory() as session:
        source = await session.scalar(select(LoginThrottle).where(LoginThrottle.scope == "source"))
        assert source is not None and source.locked_until is not None
        principals = (
            await session.scalars(select(LoginThrottle).where(LoginThrottle.scope == "principal"))
        ).all()
        assert len(principals) == 2
        assert await session.scalar(
            select(AuditEvent.id).where(AuditEvent.event_code == "USER_LOGIN_RATE_LIMITED")
        )


@pytest.mark.asyncio
async def test_concurrent_failed_logins_do_not_lose_database_updates(
    client: AsyncClient, auth_settings: Settings
) -> None:
    auth_settings.login_principal_max_failures = 10
    auth_settings.login_source_max_failures = 20
    await _bootstrap(client)
    client.cookies.clear()
    clients = [
        AsyncClient(transport=ASGITransport(app=app), base_url="https://powermeter.test")
        for _ in range(4)
    ]
    try:
        responses = await asyncio.gather(
            *(
                candidate.post(
                    "/api/v1/auth/login",
                    json={"email": OWNER_EMAIL, "password": INVALID_PASSWORD},
                )
                for candidate in clients
            )
        )
    finally:
        await asyncio.gather(*(candidate.aclose() for candidate in clients))
    assert all(response.status_code == 401 for response in responses)
    async with session_factory() as session:
        principal = await session.scalar(
            select(LoginThrottle).where(LoginThrottle.scope == "principal")
        )
        source = await session.scalar(select(LoginThrottle).where(LoginThrottle.scope == "source"))
        assert principal is not None and source is not None
        assert principal.failure_count == 4
        assert source.failure_count == 4


@pytest.mark.asyncio
async def test_login_fails_closed_when_throttle_storage_is_unavailable(
    client: AsyncClient,
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _bootstrap(client)

    async def unavailable(*_args: object, **_kwargs: object) -> LoginThrottle:
        raise RuntimeError("simulated throttle storage failure")

    monkeypatch.setattr(auth_security, "_lock_login_throttle", unavailable)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://powermeter.test",
    ) as independent_client:
        response = await independent_client.post(
            "/api/v1/auth/login",
            json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
        )
    assert response.status_code == 500
    assert not response.headers.get_list("set-cookie")
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Session.id))) == 1
    assert auth_settings.login_principal_max_failures == 2
