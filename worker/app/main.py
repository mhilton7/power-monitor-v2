from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import structlog
from backend.app.config import get_settings
from backend.app.db import make_engine, make_session_factory
from backend.app.logging_config import configure_logging

from worker.app.jobs import run_jobs

HEALTH_FILE = Path(os.environ.get("PM_WORKER_HEALTH_FILE", "/data/worker-health.json"))


async def run() -> None:
    settings = get_settings()
    configure_logging(
        level_name=settings.log_level,
        log_dir=settings.log_dir,
        retention_days=settings.log_retention_days,
        service_name="worker",
    )
    logger = structlog.get_logger("worker")
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings)
    sessions = make_session_factory(engine)
    try:
        while True:
            try:
                async with sessions() as session:
                    result = await run_jobs(
                        session,
                        backup_status_dir=settings.backup_status_dir,
                        settings=settings,
                    )
                HEALTH_FILE.write_text(
                    json.dumps(
                        {
                            "state": "healthy",
                            "completed_at": datetime.now(UTC).isoformat(),
                            "result": result,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                logger.info("worker_cycle_completed", result=result)
            except Exception as exc:
                HEALTH_FILE.write_text(
                    json.dumps(
                        {
                            "state": "degraded",
                            "completed_at": datetime.now(UTC).isoformat(),
                            "error_code": type(exc).__name__,
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                logger.exception("worker_cycle_failed", error_code=type(exc).__name__)
            await asyncio.sleep(15)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
