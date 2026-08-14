from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

SAFE_LOG_FIELDS = frozenset(
    {
        "event",
        "level",
        "logger",
        "timestamp",
        "service",
        "version",
        "protocol",
        "correlation_id",
        "home_id",
        "device_id",
        "command_id",
        "sync_id",
        "event_code",
        "error_code",
        "state",
        "result",
    }
)
SAFE_RESULT_FIELDS = frozenset(
    {
        "costs",
        "billing_estimates",
        "rollups",
        "alerts",
        "operational_alerts",
        "firmware_completed",
        "staged_rollouts_advanced",
        "prepare_tokens_expired",
        "nonces_removed",
        "rate_syncs",
        "lease_busy",
    }
)
SECRET_KEY = re.compile(
    r"authorization|cookie|password|passwd|secret|token|credential|private|session|csrf|key",
    re.IGNORECASE,
)
SECRET_VALUE = re.compile(r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def _sanitize_logging_event(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in event_dict.items():
        if key in {"_record", "_from_structlog"}:
            # ProcessorFormatter removes these internal objects immediately before rendering.
            # Keeping them here is required for foreign stdlib records such as httpx access logs.
            sanitized[key] = value
            continue
        if key not in SAFE_LOG_FIELDS or SECRET_KEY.search(key):
            continue
        if key == "result":
            if isinstance(value, dict):
                sanitized[key] = {
                    nested_key: nested_value
                    for nested_key, nested_value in value.items()
                    if nested_key in SAFE_RESULT_FIELDS
                    and isinstance(nested_value, int | float | bool | type(None))
                }
            continue
        if isinstance(value, str):
            sanitized[key] = SECRET_VALUE.sub("[REDACTED]", value[:500])
        elif isinstance(value, int | float | bool | type(None)):
            sanitized[key] = value
    return sanitized


def configure_logging(
    *,
    level_name: str,
    log_dir: Path | None,
    retention_days: int,
    service_name: str = "api",
) -> None:
    """Emit newline-delimited JSON to stdout and an optional daily rotating file."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _sanitize_logging_event,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(sort_keys=True),
        foreign_pre_chain=shared_processors,
    )

    handlers: list[logging.Handler] = []
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)

    if log_dir is not None:
        log_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            filename=log_dir / f"power-meter-v2-{service_name}.jsonl",
            when="midnight",
            interval=1,
            backupCount=retention_days,
            encoding="utf-8",
            delay=True,
            utc=True,
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    for existing_handler in root_logger.handlers[:]:
        root_logger.removeHandler(existing_handler)
        existing_handler.close()
    root_logger.setLevel(level)
    for handler in handlers:
        root_logger.addHandler(handler)

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
