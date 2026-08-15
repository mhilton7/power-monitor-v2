from __future__ import annotations

import argparse
import json
import re
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SUCCESS_SUFFIXES = (
    ".json",
    "-authenticated.json",
    "-compose-ps.jsonl",
    "-permissions.txt",
)
FAILURE_SUFFIXES = (
    "-failure.json",
    "-failure-compose-ps.jsonl",
    "-failure-health.jsonl",
    "-failure-log-events.jsonl",
)
SERVICE_NAMES = {
    "initialize",
    "postgres",
    "migrate",
    "api",
    "worker",
    "frontend",
    "gateway",
    "backup",
}
RUNNING_SERVICE_NAMES = {"postgres", "api", "worker", "frontend", "gateway", "backup"}
ONE_SHOT_SERVICE_NAMES = {"initialize", "migrate"}
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
UTC_TIMESTAMP_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
LOG_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?Z\Z"
)
CONTAINER_STATUSES = {
    "created",
    "running",
    "paused",
    "restarting",
    "removing",
    "exited",
    "dead",
}
HEALTH_STATUSES = {"starting", "healthy", "unhealthy"}
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
SUCCESS_REPORT_KEYS = {
    "schema",
    "version",
    "revision",
    "completed_at",
    "status",
    "services",
    "checks",
    "rollback",
    "pdf_sandbox",
    "authenticated_sensor_evidence",
    "backup",
    "restore_test",
}
AUTHENTICATED_REPORT_KEYS = {
    "schema",
    "status",
    "protocol",
    "device_id",
    "enrollment",
    "heartbeat",
    "reading_sequence",
    "energy_kwh",
    "usage_source",
    "rate_source",
    "rate_source_sha256",
    "cost",
    "cost_rule",
    "command",
}
SUCCESS_SERVICES = [
    "api",
    "backup",
    "frontend",
    "gateway",
    "initialize",
    "migrate",
    "postgres",
    "worker",
]
SUCCESS_CHECKS = [
    "exact service set",
    "digest-pinned image startup",
    "one-shot host initializer first run",
    "one-shot host initializer idempotent rerun",
    "TLS chain and hostname",
    "liveness and readiness",
    "API image PDF sandbox self-test",
    "authenticated owner login",
    "authenticated sensor enrollment",
    "authenticated PZEM heartbeat and reading",
    "PZEM-only History",
    "reviewed rate-only PDF",
    "worker-produced sensor cost",
    "authenticated command round trip",
    "authenticated system health",
    "SSE proxy streaming",
    "oversize PDF rejection",
    "per-service restarts without initializer restart",
    "migration rerun",
    "full-stack runtime restart without initializer restart",
    "bind-mount access",
    "encrypted backup",
    "isolated restore",
]
FAILURE_DIAGNOSTICS = [
    "allowlisted service log event timeline",
    "Compose service state",
    "allowlisted container health state",
]


class EvidenceError(ValueError):
    """Deployment evidence is incomplete, ambiguous, or unsafe to publish."""


def _reject_constant(value: str) -> None:
    raise EvidenceError(f"non-finite JSON value is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _loads_strict_json(value: str, *, source: Path) -> Any:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise EvidenceError(f"invalid JSON in {source}: {exc}") from exc
    return parsed


def _require_regular_nonempty(path: Path) -> None:
    if path.is_symlink():
        raise EvidenceError(f"evidence must not be a symlink: {path}")
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise EvidenceError(f"required evidence is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise EvidenceError(f"evidence must be a nonempty regular file: {path}")


def _load_json(path: Path) -> Any:
    _require_regular_nonempty(path)
    try:
        value = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise EvidenceError(f"evidence is not UTF-8: {path}") from exc
    return _loads_strict_json(value, source=path)


def _load_jsonl(path: Path) -> list[Any]:
    _require_regular_nonempty(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise EvidenceError(f"evidence is not UTF-8: {path}") from exc
    if not lines:
        raise EvidenceError(f"JSONL evidence has no records: {path}")
    records: list[Any] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise EvidenceError(f"blank JSONL record in {path}:{line_number}")
        records.append(_loads_strict_json(line, source=Path(f"{path}:{line_number}")))
    return records


def _require_absent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise EvidenceError(f"opposite-outcome evidence must be absent: {path}")


def _require_exact_files(prefix: Path, expected_suffixes: tuple[str, ...]) -> None:
    expected = {prefix.with_name(prefix.name + suffix) for suffix in expected_suffixes}
    actual = set(prefix.parent.glob(prefix.name + "*"))
    unexpected = sorted(actual - expected)
    if unexpected:
        rendered = ", ".join(str(path) for path in unexpected)
        raise EvidenceError(f"unexpected deployment evidence path(s): {rendered}")


def _require_schema(value: Any, *, schema: str, status_value: str, source: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"evidence root must be a JSON object: {source}")
    if value.get("schema") != schema or value.get("status") != status_value:
        raise EvidenceError(f"unexpected schema or status in {source}")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_exact_keys(value: dict[str, Any], expected: set[str], *, source: Path) -> None:
    if set(value) != expected:
        raise EvidenceError(f"evidence has missing or unexpected fields: {source}")


def _require_positive_decimal(value: Any, *, field: str, source: Path) -> None:
    if not isinstance(value, str):
        raise EvidenceError(f"{field} must be an exact decimal string: {source}")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise EvidenceError(f"{field} must be an exact decimal string: {source}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise EvidenceError(f"{field} must be a positive finite decimal: {source}")


def _validate_authenticated_report(value: Any, *, source: Path) -> dict[str, Any]:
    report = _require_schema(
        value,
        schema="pm-deployment-authenticated-evidence/1.0.0",
        status_value="passed",
        source=source,
    )
    _require_exact_keys(report, AUTHENTICATED_REPORT_KEYS, source=source)
    expected_values = {
        "protocol": "pm-protocol/1.0.0",
        "enrollment": "authenticated",
        "heartbeat": "authenticated_pzem",
        "reading_sequence": 1,
        "usage_source": "authenticated PZEM-004T sensor intervals only",
        "rate_source": "reviewed_rate_only_pdf",
    }
    if any(report.get(key) != expected for key, expected in expected_values.items()):
        raise EvidenceError(f"authenticated PZEM evidence invariant failed: {source}")
    if (
        not isinstance(report.get("device_id"), str)
        or UUID_PATTERN.fullmatch(report["device_id"]) is None
    ):
        raise EvidenceError(f"authenticated device_id must be a UUID: {source}")
    if (
        not isinstance(report.get("rate_source_sha256"), str)
        or SHA256_PATTERN.fullmatch(report["rate_source_sha256"]) is None
    ):
        raise EvidenceError(f"rate source digest must be lowercase SHA-256: {source}")
    _require_positive_decimal(report.get("energy_kwh"), field="energy_kwh", source=source)
    _require_positive_decimal(report.get("cost"), field="cost", source=source)
    if report.get("cost_rule") != "sum_only_when_every_visible_interval_is_priced":
        raise EvidenceError(f"cost_rule is not the authenticated interval rule: {source}")
    command = report.get("command")
    if not isinstance(command, dict) or set(command) != {
        "id",
        "type",
        "delivery",
        "state",
        "result_code",
    }:
        raise EvidenceError(f"authenticated command evidence has unexpected fields: {source}")
    if not isinstance(command.get("id"), str) or UUID_PATTERN.fullmatch(command["id"]) is None:
        raise EvidenceError(f"authenticated command id must be a UUID: {source}")
    if {
        "type": command.get("type"),
        "delivery": command.get("delivery"),
        "state": command.get("state"),
        "result_code": command.get("result_code"),
    } != {
        "type": "reboot",
        "delivery": "authenticated",
        "state": "succeeded",
        "result_code": "REBOOT_COMPLETED",
    }:
        raise EvidenceError(f"authenticated command invariant failed: {source}")
    return report


def _validate_backup_record(value: Any, *, minimum_checks: int, source: Path) -> None:
    if not isinstance(value, dict):
        raise EvidenceError(f"backup/restore evidence must be an object: {source}")
    if value.get("format") != "pm-backup/1.0.0" or value.get("state") != "verified":
        raise EvidenceError(f"backup/restore evidence is not verified: {source}")
    if not isinstance(value.get("run_id"), str) or not value["run_id"]:
        raise EvidenceError(f"backup/restore run_id is missing: {source}")
    if (
        not isinstance(value.get("sha256"), str)
        or SHA256_PATTERN.fullmatch(value["sha256"]) is None
    ):
        raise EvidenceError(f"backup/restore digest must be lowercase SHA-256: {source}")
    checks = value.get("verification_checks")
    if (
        not isinstance(checks, list)
        or len(checks) < minimum_checks
        or not all(isinstance(check, str) and check for check in checks)
    ):
        raise EvidenceError(f"backup/restore verification checks are incomplete: {source}")


def _validate_compose_record(value: Any, *, source: Path, line_number: int) -> str | None:
    location = f"{source}:{line_number}"
    if not isinstance(value, dict) or set(value) != {
        "service",
        "state",
        "health",
        "exit_code",
    }:
        raise EvidenceError(f"Compose record has non-allowlisted fields: {location}")
    service = value["service"]
    if service is None:
        if any(value[field] is not None for field in ("state", "health", "exit_code")):
            raise EvidenceError(f"Compose placeholder must be entirely null: {location}")
        return None
    if not isinstance(service, str) or service not in SERVICE_NAMES:
        raise EvidenceError(f"Compose service is outside the fixed allowlist: {location}")
    if not isinstance(value["state"], str) or value["state"] not in CONTAINER_STATUSES:
        raise EvidenceError(f"Compose state is outside the fixed allowlist: {location}")
    if value["health"] is not None and (
        not isinstance(value["health"], str) or value["health"] not in HEALTH_STATUSES
    ):
        raise EvidenceError(f"Compose health is outside the fixed allowlist: {location}")
    if not _is_int(value["exit_code"]):
        raise EvidenceError(f"Compose exit_code must be an integer: {location}")
    return service


def _validate_log_record(value: Any, *, source: Path, line_number: int) -> None:
    location = f"{source}:{line_number}"
    if not isinstance(value, dict) or set(value) != {
        "line_number",
        "service",
        "timestamp",
        "event",
    }:
        raise EvidenceError(f"log record has non-allowlisted fields: {location}")
    if not _is_int(value["line_number"]):
        raise EvidenceError(f"log line_number must be an integer: {location}")
    if value["service"] is not None and (
        not isinstance(value["service"], str) or value["service"] not in SERVICE_NAMES
    ):
        raise EvidenceError(f"log service is outside the fixed allowlist: {location}")
    timestamp = value["timestamp"]
    if timestamp is not None and (
        not isinstance(timestamp, str) or LOG_TIMESTAMP_PATTERN.fullmatch(timestamp) is None
    ):
        raise EvidenceError(f"log timestamp is outside the fixed format: {location}")
    if not isinstance(value["event"], str) or value["event"] not in LOG_EVENTS:
        raise EvidenceError(f"log event is outside the fixed allowlist: {location}")
    if value["event"] == "unavailable" and value != {
        "line_number": 0,
        "service": None,
        "timestamp": None,
        "event": "unavailable",
    }:
        raise EvidenceError(f"unavailable log placeholder has unexpected values: {location}")
    if value["event"] != "unavailable" and not 1 <= value["line_number"] <= 2_000:
        raise EvidenceError(f"log line_number must be between 1 and 2000: {location}")


def _validate_health_record(value: Any, *, source: Path, line_number: int) -> None:
    location = f"{source}:{line_number}"
    if not isinstance(value, dict) or set(value) != {"service", "container_id", "state"}:
        raise EvidenceError(f"health record has non-allowlisted top-level fields: {location}")
    service = value["service"]
    container_id = value["container_id"]
    state = value["state"]
    if service is None or container_id is None:
        if service is not None or container_id is not None or state is not None:
            raise EvidenceError(f"health placeholder must be entirely null: {location}")
        return
    if not isinstance(service, str) or service not in SERVICE_NAMES:
        raise EvidenceError(f"health service is outside the fixed allowlist: {location}")
    if not isinstance(container_id, str) or CONTAINER_ID_PATTERN.fullmatch(container_id) is None:
        raise EvidenceError(f"health container_id must be 64 lowercase hex characters: {location}")
    if state is None:
        return
    state_keys = {
        "status",
        "running",
        "restarting",
        "oom_killed",
        "dead",
        "exit_code",
        "health",
        "readiness",
    }
    if not isinstance(state, dict) or set(state) != state_keys:
        raise EvidenceError(f"health state has non-allowlisted fields: {location}")
    if not isinstance(state["status"], str) or state["status"] not in CONTAINER_STATUSES:
        raise EvidenceError(f"container status is outside the fixed allowlist: {location}")
    for key in ("running", "restarting", "oom_killed", "dead"):
        if not isinstance(state[key], bool):
            raise EvidenceError(f"container {key} must be boolean: {location}")
    if not _is_int(state["exit_code"]):
        raise EvidenceError(f"container exit_code must be an integer: {location}")
    readiness = state["readiness"]
    if readiness is not None:
        if not isinstance(readiness, dict) or set(readiness) != {
            "http_status",
            "status",
            "database",
            "pdf_sandbox",
        }:
            raise EvidenceError(f"readiness summary has non-allowlisted fields: {location}")
        if not _is_int(readiness["http_status"]) or readiness["http_status"] not in {200, 503}:
            raise EvidenceError(f"readiness HTTP status is outside the allowlist: {location}")
        if not isinstance(readiness["status"], str) or readiness["status"] not in {
            "ready",
            "not_ready",
        }:
            raise EvidenceError(f"readiness status is outside the allowlist: {location}")
        if not isinstance(readiness["database"], str) or readiness["database"] not in {
            "ready",
            "unavailable",
        }:
            raise EvidenceError(f"database readiness is outside the allowlist: {location}")
        if not isinstance(readiness["pdf_sandbox"], str) or readiness["pdf_sandbox"] not in {
            "enforced",
            "unavailable",
        }:
            raise EvidenceError(f"PDF readiness is outside the allowlist: {location}")
        ready = (
            readiness["http_status"] == 200
            and readiness["status"] == "ready"
            and readiness["database"] == "ready"
            and readiness["pdf_sandbox"] == "enforced"
        )
        not_ready = readiness["http_status"] == 503 and readiness["status"] == "not_ready"
        if not ready and not not_ready:
            raise EvidenceError(f"readiness summary fields are inconsistent: {location}")
    health = state["health"]
    if health is None:
        return
    if not isinstance(health, dict) or set(health) != {"status", "failing_streak"}:
        raise EvidenceError(f"health summary has non-allowlisted fields: {location}")
    if not isinstance(health["status"], str) or health["status"] not in HEALTH_STATUSES:
        raise EvidenceError(f"health status is outside the fixed allowlist: {location}")
    if not _is_int(health["failing_streak"]) or health["failing_streak"] < 0:
        raise EvidenceError(f"health failing_streak must be a nonnegative integer: {location}")


def _validate_permissions(path: Path) -> None:
    _require_regular_nonempty(path)
    try:
        value = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise EvidenceError(f"permissions evidence is not UTF-8: {path}") from exc
    base = "/mnt/Apps/PowerMeterV2"
    required_records = {
        f"directory|{base}/postgres|70:70|700",
        f"directory|{base}/config|0:0|755",
        f"directory|{base}/firmware|10001:10001|750",
        f"directory|{base}/backups|568:568|750",
        f"directory|{base}/backups/status|568:568|750",
        f"directory|{base}/logs|0:0|711",
        f"directory|{base}/logs/application|10001:10001|750",
        f"directory|{base}/logs/gateway|1000:1000|750",
        f"directory|{base}/rate-source-artifacts|10001:10001|750",
        f"directory|{base}/caddy-data|1000:1000|750",
        f"directory|{base}/caddy-config|1000:1000|750",
        f"directory|{base}/secrets|0:0|711",
        f"config|{base}/config/Caddyfile|0:1000|440",
        f"config|{base}/config/postgres-init-roles.sh|0:70|440",
        f"acl|{base}/backups|exact-api-traverse-only",
        f"acl|{base}/backups/status|exact-api-read-default",
    }
    readers = {
        "postgres_bootstrap_password": "70",
        "postgres_migrator_password": "70,10001",
        "postgres_api_password": "70,10001",
        "postgres_worker_password": "70,10001",
        "postgres_backup_password": "70,568",
        "postgres_restore_password": "70,568",
        "session_secret": "10001",
        "field_encryption_key": "10001",
        "ota_manifest_key": "10001",
        "backup_encryption_key": "568",
        "tls.crt": "1000",
        "tls.key": "1000",
        "tls-ca.crt": "1000",
    }
    for name, allowed in readers.items():
        required_records.add(f"secret|{base}/secrets/{name}|0:0|440")
        required_records.add(f"readers|{name}|{allowed}")
    lines = value.splitlines()
    if any(not line for line in lines):
        raise EvidenceError(f"permissions evidence contains a blank record: {path}")
    actual_records = set(lines)
    if len(actual_records) != len(lines):
        raise EvidenceError(f"permissions evidence contains a duplicate record: {path}")
    if actual_records != required_records:
        raise EvidenceError(
            f"permissions evidence does not contain the exact corrected state: {path}"
        )


def validate(
    prefix: Path,
    *,
    outcome: str,
    expected_version: str,
    expected_revision: str,
) -> None:
    if outcome not in {"success", "failure"}:
        raise EvidenceError(f"unsupported smoke outcome: {outcome}")
    if not expected_version or COMMIT_PATTERN.fullmatch(expected_revision) is None:
        raise EvidenceError("expected version and 40-character revision are required")
    selected = SUCCESS_SUFFIXES if outcome == "success" else FAILURE_SUFFIXES
    opposite = FAILURE_SUFFIXES if outcome == "success" else SUCCESS_SUFFIXES
    _require_exact_files(prefix, selected)
    for suffix in opposite:
        _require_absent(prefix.with_name(prefix.name + suffix))
    for suffix in selected:
        _require_regular_nonempty(prefix.with_name(prefix.name + suffix))

    if outcome == "success":
        report_path = prefix.with_name(prefix.name + ".json")
        authenticated_path = prefix.with_name(prefix.name + "-authenticated.json")
        compose_path = prefix.with_name(prefix.name + "-compose-ps.jsonl")
        report = _require_schema(
            _load_json(report_path),
            schema="pm-deployment-test/1.0.0",
            status_value="passed",
            source=report_path,
        )
        _require_exact_keys(report, SUCCESS_REPORT_KEYS, source=report_path)
        if report["version"] != expected_version or report["revision"] != expected_revision:
            raise EvidenceError(
                f"success evidence is not bound to the expected release: {report_path}"
            )
        if (
            not isinstance(report["completed_at"], str)
            or UTC_TIMESTAMP_PATTERN.fullmatch(report["completed_at"]) is None
        ):
            raise EvidenceError(
                f"success completed_at must be an exact UTC timestamp: {report_path}"
            )
        if report["services"] != SUCCESS_SERVICES or report["checks"] != SUCCESS_CHECKS:
            raise EvidenceError(f"success service/check evidence is incomplete: {report_path}")
        if report["rollback"] != "not_exercised_github_hosted_smoke":
            raise EvidenceError(f"success rollback status is unexpected: {report_path}")
        if report["pdf_sandbox"] != {
            "schema_id": "pm-pdf-sandbox-health/1.0.0",
            "pdf_sandbox": "enforced",
        }:
            raise EvidenceError(f"success PDF sandbox evidence is invalid: {report_path}")
        authenticated = _validate_authenticated_report(
            _load_json(authenticated_path), source=authenticated_path
        )
        if report["authenticated_sensor_evidence"] != authenticated:
            raise EvidenceError(f"embedded authenticated evidence does not match: {report_path}")
        _validate_backup_record(report["backup"], minimum_checks=4, source=report_path)
        _validate_backup_record(report["restore_test"], minimum_checks=5, source=report_path)
        compose_services: set[str] = set()
        for line_number, record in enumerate(_load_jsonl(compose_path), start=1):
            service = _validate_compose_record(record, source=compose_path, line_number=line_number)
            if service is None:
                raise EvidenceError(
                    f"success Compose evidence contains a placeholder: {compose_path}"
                )
            if service in compose_services:
                raise EvidenceError(f"success Compose evidence repeats a service: {compose_path}")
            if service in RUNNING_SERVICE_NAMES:
                if record["state"] != "running" or record["health"] != "healthy":
                    raise EvidenceError(f"success Compose service is not healthy: {compose_path}")
            elif service in ONE_SHOT_SERVICE_NAMES and (
                record["state"] != "exited"
                or record["exit_code"] != 0
                or record["health"] is not None
            ):
                raise EvidenceError(
                    f"success one-shot service did not exit cleanly: {compose_path}"
                )
            compose_services.add(service)
        if compose_services != SERVICE_NAMES:
            raise EvidenceError(
                f"Compose evidence is missing an exact service record: {compose_path}"
            )
        _validate_permissions(prefix.with_name(prefix.name + "-permissions.txt"))
        return

    failure_path = prefix.with_name(prefix.name + "-failure.json")
    failure = _require_schema(
        _load_json(failure_path),
        schema="pm-deployment-failure/1.0.0",
        status_value="failed",
        source=failure_path,
    )
    _require_exact_keys(
        failure,
        {
            "schema",
            "version",
            "revision",
            "completed_at",
            "status",
            "exit_code",
            "diagnostics",
        },
        source=failure_path,
    )
    if failure["version"] != expected_version or failure["revision"] != expected_revision:
        raise EvidenceError(
            f"failure evidence is not bound to the expected release: {failure_path}"
        )
    if (
        not isinstance(failure["completed_at"], str)
        or UTC_TIMESTAMP_PATTERN.fullmatch(failure["completed_at"]) is None
    ):
        raise EvidenceError(f"failure completed_at must be an exact UTC timestamp: {failure_path}")
    if not _is_int(failure.get("exit_code")) or failure["exit_code"] <= 0:
        raise EvidenceError(f"failure exit_code must be a positive integer: {failure_path}")
    if failure["diagnostics"] != FAILURE_DIAGNOSTICS:
        raise EvidenceError(f"failure diagnostics list is incomplete: {failure_path}")
    failure_compose_path = prefix.with_name(prefix.name + "-failure-compose-ps.jsonl")
    for line_number, record in enumerate(_load_jsonl(failure_compose_path), start=1):
        _validate_compose_record(record, source=failure_compose_path, line_number=line_number)
    health_path = prefix.with_name(prefix.name + "-failure-health.jsonl")
    for line_number, record in enumerate(_load_jsonl(health_path), start=1):
        _validate_health_record(record, source=health_path, line_number=line_number)
    log_path = prefix.with_name(prefix.name + "-failure-log-events.jsonl")
    for line_number, record in enumerate(_load_jsonl(log_path), start=1):
        _validate_log_record(record, source=log_path, line_number=line_number)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate exact deployment evidence sets.")
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--outcome", choices=("success", "failure"), required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    try:
        validate(
            args.prefix,
            outcome=args.outcome,
            expected_version=args.expected_version,
            expected_revision=args.expected_revision,
        )
    except EvidenceError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
