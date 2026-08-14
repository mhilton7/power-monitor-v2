from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from worker.app.jobs import acquire_worker_lease


@pytest.mark.asyncio
async def test_postgres_worker_lease_is_transaction_scoped() -> None:
    scalar = AsyncMock(return_value=True)
    session = cast(
        AsyncSession,
        SimpleNamespace(
            bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
            scalar=scalar,
        ),
    )

    assert await acquire_worker_lease(session) is True
    scalar_call = scalar.await_args
    assert scalar_call is not None
    statement = scalar_call.args[0]
    assert "pg_try_advisory_xact_lock" in str(statement)
    assert "pg_try_advisory_lock" not in str(statement)
