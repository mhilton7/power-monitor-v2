from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

RUNTIME = Path(".test-runtime")
RUNTIME.mkdir(exist_ok=True)
DATABASE = RUNTIME / "powermeter-tests.sqlite3"
os.environ["PM_ENV"] = "test"
CONFIGURED_DATABASE_URL = os.environ.get("PM_DATABASE_URL")
CLEANUP_DATABASE_URL = os.environ.get("PM_TEST_MIGRATOR_DATABASE_URL")
REQUIRE_POSTGRES = os.environ.get("PM_REQUIRE_POSTGRES_TESTS") == "1"
if REQUIRE_POSTGRES and not (
    CONFIGURED_DATABASE_URL
    and CONFIGURED_DATABASE_URL.startswith(("postgresql+asyncpg://", "postgresql://"))
):
    raise RuntimeError(
        "PM_REQUIRE_POSTGRES_TESTS=1 requires an explicit PostgreSQL PM_DATABASE_URL"
    )
if REQUIRE_POSTGRES and not (
    CLEANUP_DATABASE_URL
    and CLEANUP_DATABASE_URL.startswith(("postgresql+asyncpg://", "postgresql://"))
):
    raise RuntimeError(
        "PM_REQUIRE_POSTGRES_TESTS=1 requires an explicit PostgreSQL "
        "PM_TEST_MIGRATOR_DATABASE_URL for data cleanup"
    )
if CONFIGURED_DATABASE_URL is None:
    os.environ["PM_DATABASE_URL"] = f"sqlite+aiosqlite:///{DATABASE.as_posix()}"
os.environ["PM_RATE_ARTIFACT_DIR"] = str(RUNTIME / "rate-artifacts")
os.environ["PM_FIRMWARE_DIR"] = str(RUNTIME / "firmware")

from backend.app.main import app, engine  # noqa: E402
from backend.app.models import Base  # noqa: E402

CLEANUP_ENGINE: AsyncEngine | None = (
    create_async_engine(CLEANUP_DATABASE_URL) if CLEANUP_DATABASE_URL is not None else None
)


@pytest_asyncio.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    if engine.dialect.name == "postgresql":
        if CLEANUP_ENGINE is None:
            raise RuntimeError("PostgreSQL tests require the explicit migrator cleanup engine")
        # Preserve the exact Alembic-created schema, constraints, triggers, and
        # version row. The application itself remains connected only as pm_api;
        # the explicit test cleanup channel uses the production migrator role.
        async with CLEANUP_ENGINE.begin() as connection:
            table_names = (
                await connection.scalars(
                    text(
                        "SELECT tablename FROM pg_catalog.pg_tables "
                        "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                    )
                )
            ).all()
            if table_names:
                quoted = ", ".join('"' + str(name).replace('"', '""') + '"' for name in table_names)
                await connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
    else:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
    try:
        yield
    finally:
        if engine.dialect.name == "postgresql":
            # pytest-asyncio intentionally uses a fresh event loop per test. Empty
            # both pools before that loop closes so asyncpg connections are never
            # reused by a later test loop.
            await engine.dispose()
            assert CLEANUP_ENGINE is not None
            await CLEANUP_ENGINE.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://powermeter.test"
    ) as http_client:
        yield http_client


@pytest_asyncio.fixture
async def owner_client(client: AsyncClient) -> AsyncClient:
    response = await client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "owner@example.com",
            "display_name": "Owner",
            "password": "correct horse battery staple 2026!",
            "home_name": "Test Home",
            "timezone": "America/Los_Angeles",
        },
    )
    assert response.status_code == 201, response.text
    client.headers["X-CSRF-Token"] = client.cookies["pm_csrf"]
    return client
