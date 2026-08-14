from __future__ import annotations

import json
import re
import sys
from collections import deque
from collections.abc import Iterable
from typing import TypedDict

SERVICE_NAMES = ("postgres", "migrate", "api", "worker", "frontend", "gateway", "backup")
LOG_EVENTS = {
    "application_starting",
    "application_started",
    "database_ready",
    "error",
    "fatal",
    "migration_activity",
    "readiness_not_ready",
    "readiness_probe",
    "readiness_ready",
    "redacted",
    "server_process_started",
    "server_running",
    "traceback",
    "unavailable",
    "warning",
}
MAX_LOG_LINES = 2_000
MAX_INPUT_LINE_CHARACTERS = 65_536
_SERVICE_PATTERN = re.compile(
    r"^(?:power-meter-v2-)?"
    r"(?P<service>postgres|migrate|api|worker|frontend|gateway|backup)"
    r"(?:-[0-9]+)?\s+\|\s+(?P<message>.*)$",
    re.IGNORECASE,
)
_TIMESTAMP_PATTERN = re.compile(
    r"(?<![0-9])(?P<timestamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z)(?![0-9])"
)


class SanitizedLogRecord(TypedDict):
    line_number: int
    service: str | None
    timestamp: str | None
    event: str


def _classify(message: str) -> str:
    lowered = message.lower()
    if "/health/ready" in lowered:
        if re.search(r"(?:^|\s)200(?:\s|$)", lowered):
            return "readiness_ready"
        if re.search(r"(?:^|\s)503(?:\s|$)", lowered):
            return "readiness_not_ready"
        return "readiness_probe"
    if "waiting for application startup" in lowered:
        return "application_starting"
    if "application startup complete" in lowered:
        return "application_started"
    if "started server process" in lowered:
        return "server_process_started"
    if "uvicorn running" in lowered:
        return "server_running"
    if "ready to accept connections" in lowered or "database system is ready" in lowered:
        return "database_ready"
    if "alembic" in lowered or "running upgrade" in lowered:
        return "migration_activity"
    if "traceback" in lowered:
        return "traceback"
    if "critical" in lowered or "fatal" in lowered:
        return "fatal"
    if "error" in lowered or "exception" in lowered or "failed" in lowered:
        return "error"
    if "warning" in lowered or "warn" in lowered:
        return "warning"
    return "redacted"


def sanitize_line(raw_line: str, *, line_number: int) -> SanitizedLogRecord:
    bounded = raw_line[:MAX_INPUT_LINE_CHARACTERS].rstrip("\r\n")
    match = _SERVICE_PATTERN.match(bounded)
    service = match.group("service").lower() if match else None
    message = match.group("message") if match else bounded
    timestamp_match = _TIMESTAMP_PATTERN.search(message)
    timestamp = timestamp_match.group("timestamp") if timestamp_match else None
    return {
        "line_number": line_number,
        "service": service,
        "timestamp": timestamp,
        "event": _classify(message),
    }


def sanitize_stream(lines: Iterable[str]) -> list[SanitizedLogRecord]:
    records: deque[SanitizedLogRecord] = deque(maxlen=MAX_LOG_LINES)
    for line_number, line in enumerate(lines, start=1):
        records.append(sanitize_line(line, line_number=line_number))
    return [
        {**record, "line_number": bounded_line_number}
        for bounded_line_number, record in enumerate(records, start=1)
    ]


def _bounded_stdin_lines() -> Iterable[str]:
    stream = sys.stdin.buffer
    while True:
        chunk = stream.readline(MAX_INPUT_LINE_CHARACTERS + 1)
        if not chunk:
            return
        if not chunk.endswith(b"\n"):
            while chunk and not chunk.endswith(b"\n"):
                chunk = stream.readline(MAX_INPUT_LINE_CHARACTERS + 1)
            yield ""
            continue
        yield chunk.decode("utf-8", errors="replace")


def main() -> int:
    records = sanitize_stream(_bounded_stdin_lines())
    for record in records:
        print(json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
