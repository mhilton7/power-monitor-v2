from __future__ import annotations

import json
import logging
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog
from backend.app.logging_config import configure_logging


def test_json_file_logging_and_retention() -> None:
    log_dir = Path(".test-runtime") / f"logging-{uuid.uuid4()}"
    configure_logging(level_name="INFO", log_dir=log_dir, retention_days=90)
    structlog.get_logger("test").info(
        "verified_event",
        secret="must-not-escape",
        authorization="Bearer must-not-escape",
        state="healthy",
        result={"costs": 2, "password": "must-not-escape"},
    )
    logging.getLogger("foreign-library").info("safe foreign event")

    records = (log_dir / "power-meter-v2-api.jsonl").read_text(encoding="utf-8").splitlines()
    event = json.loads(records[-2])
    assert event["event"] == "verified_event"
    assert event["level"] == "info"
    assert event["logger"] == "test"
    assert event["state"] == "healthy"
    assert event["result"] == {"costs": 2}
    assert "secret" not in event
    assert "authorization" not in event
    assert "must-not-escape" not in "\n".join(records)
    foreign_event = json.loads(records[-1])
    assert foreign_event["event"] == "safe foreign event"
    assert foreign_event["logger"] == "foreign-library"

    rotating_handlers = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, TimedRotatingFileHandler)
    ]
    assert len(rotating_handlers) == 1
    assert rotating_handlers[0].backupCount == 90
