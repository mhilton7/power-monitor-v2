from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import text

from alembic import command
from alembic.config import Config

from .config import get_settings
from .db import make_engine

GRANT_STATEMENTS = (
    "REVOKE ALL ON SCHEMA public FROM PUBLIC",
    "GRANT USAGE ON SCHEMA public TO pm_api, pm_worker, pm_backup",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO pm_api, pm_worker",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pm_api, pm_worker",
    "GRANT SELECT ON ALL TABLES IN SCHEMA public TO pm_backup",
    "GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO pm_backup",
)


async def apply_runtime_grants() -> None:
    settings = get_settings()
    if not settings.resolved_database_url.startswith("postgresql+"):
        return
    engine = make_engine(settings)
    try:
        async with engine.begin() as connection:
            for statement in GRANT_STATEMENTS:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


def main() -> None:
    settings = get_settings()
    if settings.service_role != "migrate":
        raise RuntimeError("database migration requires PM_SERVICE_ROLE=migrate")
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    config = Config(str(config_path))
    command.upgrade(config, "head")
    asyncio.run(apply_runtime_grants())


if __name__ == "__main__":
    main()
