from __future__ import annotations

import os
from pathlib import Path

import pytest
from backend.app.main import engine
from backend.app.models import Base
from sqlalchemy import text

from alembic.config import Config
from alembic.script import ScriptDirectory


@pytest.mark.integration
@pytest.mark.asyncio
async def test_required_ci_database_is_postgres_and_at_alembic_head() -> None:
    if os.environ.get("PM_REQUIRE_POSTGRES_TESTS") != "1":
        pytest.skip("the local portable suite does not require PostgreSQL")

    assert engine.dialect.name == "postgresql"
    configuration = Config(str(Path("backend/alembic.ini")))
    expected_head = ScriptDirectory.from_config(configuration).get_current_head()
    assert expected_head is not None
    async with engine.connect() as connection:
        database_name = await connection.scalar(text("SELECT current_database()"))
        database_role = await connection.scalar(text("SELECT current_user"))
        migrated_head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        public_tables = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
    assert isinstance(database_name, str) and database_name
    assert database_role == "pm_api"
    assert migrated_head == expected_head
    assert int(public_tables or 0) >= len(Base.metadata.tables)
