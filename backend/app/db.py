from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings, get_settings


def make_engine(settings: Settings | None = None) -> AsyncEngine:
    active = settings or get_settings()
    return create_async_engine(
        active.resolved_database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    from .main import session_factory

    async with session_factory() as session:
        yield session
