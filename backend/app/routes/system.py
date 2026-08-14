from __future__ import annotations

import hashlib
import io
import json
import math
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..constants import PROTOCOL_ID, VERSION
from ..db import get_session
from ..models import (
    Alert,
    ApplicationLog,
    Device,
    DeviceCommand,
    DeviceHeartbeat,
    RateSyncRun,
    aware_utc,
    user_home_scopes,
)
from ..security.auth import CurrentUser, require_permission

router = APIRouter(prefix="/api/v1", tags=["system"])

SAFE_DIAGNOSTIC_DETAIL_KEYS = frozenset(
    {
        "accepted",
        "algorithm_version",
        "alert_type",
        "attempt",
        "byte_count",
        "command_type",
        "count",
        "duration_ms",
        "error_code",
        "event_code",
        "evidence_id",
        "firmware_version",
        "first_sequence",
        "gap_count",
        "highest_contiguous_sequence",
        "http_status",
        "identical_retries",
        "last_sequence",
        "operation",
        "page_count",
        "parser_version",
        "progress_percent",
        "protocol",
        "reason_code",
        "release_id",
        "result",
        "result_code",
        "revision_id",
        "run_id",
        "scope",
        "severity",
        "sha256",
        "source_id",
        "state",
        "status",
    }
)
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[a-z0-9._~-]+|pm_session=|pm_csrf=|"
    r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----|password\s*[=:]|"
    r"secret\s*[=:]|token\s*[=:])"
)


class DiagnosticEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    created_at: str
    event_code: str = Field(min_length=1, max_length=100)
    level: str = Field(min_length=1, max_length=16)
    correlation_id: str | None
    device_id: str | None
    command_id: str | None
    sync_id: str | None
    details: dict[str, str | int | float | bool | None]
    excluded_detail_fields: list[str]


class DiagnosticHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_id: str = "pm-diagnostics-health/1.0.0"
    generated_at: str
    version: str
    protocol: str
    correlation_id: str
    physical_hardware_certification: str


class DiagnosticMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiagnosticManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_id: str = "pm-diagnostics-bundle/1.0.0"
    generated_at: str
    members: list[DiagnosticMember]
    archive_sha256_delivery: str = "X-Content-SHA256 response header"


def _evidence_file(name: str, status_directory: Path) -> dict[str, Any]:
    path = status_directory / name
    if not path.is_file():
        return {"state": "unavailable", "evidence_file": name}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "invalid", "evidence_file": name}
    if not isinstance(value, dict):
        return {"state": "invalid", "evidence_file": name}
    allowlist = {
        "state",
        "run_id",
        "started_at",
        "completed_at",
        "sha256",
        "byte_count",
        "database_version",
        "migration_revision",
        "verification_checks",
        "error_code",
    }
    return {key: value[key] for key in allowlist if key in value}


@router.get("/system/health")
async def system_health(
    user: CurrentUser = Depends(require_permission("system.view")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    now = datetime.now(UTC)
    homes = tuple(
        (
            await session.scalars(
                select(user_home_scopes.c.home_id).where(user_home_scopes.c.user_id == user.id)
            )
        ).all()
    )
    devices = (
        await session.scalars(
            select(Device).where(Device.home_id.in_(homes), Device.revoked_at.is_(None))
        )
    ).all()
    sensor_health = []
    for device in devices:
        heartbeat = await session.scalar(
            select(DeviceHeartbeat)
            .where(DeviceHeartbeat.device_id == device.id)
            .order_by(DeviceHeartbeat.received_at.desc())
            .limit(1)
        )
        age = (now - aware_utc(heartbeat.received_at)).total_seconds() if heartbeat else None
        sensor_health.append(
            {
                "device_id": device.id,
                "state": "online" if age is not None and age <= 30 else "offline",
                "heartbeat_age_seconds": age,
                "pzem_status": heartbeat.pzem_status if heartbeat else "unavailable",
                "storage_status": heartbeat.storage_status if heartbeat else "unavailable",
                "backlog": heartbeat.backlog if heartbeat else None,
            }
        )
    last_sync = await session.scalar(
        select(RateSyncRun)
        .where(RateSyncRun.home_id.in_(homes))
        .order_by(RateSyncRun.started_at.desc())
        .limit(1)
    )
    open_alert_count = int(
        await session.scalar(
            select(func.count(Alert.id)).where(Alert.home_id.in_(homes), Alert.state == "open")
        )
        or 0
    )
    return {
        "generated_at": now,
        "version": VERSION,
        "protocol": PROTOCOL_ID,
        "database": "reachable",
        "sensors": sensor_health,
        "open_alert_count": open_alert_count,
        "last_rate_sync": {
            "id": last_sync.id,
            "state": last_sync.state,
            "event_code": last_sync.event_code,
            "completed_at": last_sync.completed_at,
        }
        if last_sync
        else None,
        "backup": {
            "last_successful": _evidence_file(
                "last-successful-backup.json", settings.backup_status_dir
            ),
            "last_attempt": _evidence_file("last-backup-attempt.json", settings.backup_status_dir),
        },
        "restore_test": {
            "last_successful": _evidence_file(
                "last-successful-restore-test.json", settings.backup_status_dir
            ),
            "last_attempt": _evidence_file(
                "last-restore-test-attempt.json", settings.backup_status_dir
            ),
        },
        "physical_hardware_certification": "pending",
    }


@router.get("/backups/status")
async def backup_status(
    _user: CurrentUser = Depends(require_permission("backups.view")),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return {
        "last_successful_backup": _evidence_file(
            "last-successful-backup.json", settings.backup_status_dir
        ),
        "last_backup_attempt": _evidence_file(
            "last-backup-attempt.json", settings.backup_status_dir
        ),
        "last_successful_restore_test": _evidence_file(
            "last-successful-restore-test.json", settings.backup_status_dir
        ),
        "last_restore_test_attempt": _evidence_file(
            "last-restore-test-attempt.json", settings.backup_status_dir
        ),
        "verification_rule": (
            "success requires checksum, decrypt, pg_restore listing, and isolated restore evidence"
        ),
    }


def _safe_diagnostic_details(
    details: dict[str, Any],
) -> tuple[dict[str, str | int | float | bool | None], list[str]]:
    output: dict[str, str | int | float | bool | None] = {}
    excluded: list[str] = []
    for key, value in details.items():
        if key not in SAFE_DIAGNOSTIC_DETAIL_KEYS:
            excluded.append(key)
            continue
        if (
            value is None
            or isinstance(value, bool | int)
            or (isinstance(value, float) and math.isfinite(value))
        ):
            output[key] = value
        elif isinstance(value, str):
            if SECRET_VALUE_PATTERN.search(value):
                output[key] = "[REDACTED]"
            else:
                output[key] = value[:300]
        else:
            excluded.append(key)
    return output, sorted(set(excluded))


def _validated_json_bytes(value: BaseModel) -> bytes:
    body = value.model_dump_json().encode("utf-8")
    # Revalidate the exact serialized representation. Any unsupported value or
    # model drift fails the request before an archive is returned.
    type(value).model_validate_json(body)
    return body


@router.get("/diagnostics/bundle")
async def diagnostics_bundle(
    request: Request,
    user: CurrentUser = Depends(require_permission("logs.view")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    actor_homes = select(user_home_scopes.c.home_id).where(user_home_scopes.c.user_id == user.id)
    actor_devices = select(Device.id).where(Device.home_id.in_(actor_homes))
    actor_commands = select(DeviceCommand.id).where(DeviceCommand.device_id.in_(actor_devices))
    actor_syncs = select(RateSyncRun.id).where(RateSyncRun.home_id.in_(actor_homes))
    has_actor_scope = or_(
        ApplicationLog.home_id.in_(actor_homes),
        ApplicationLog.device_id.in_(actor_devices),
        ApplicationLog.command_id.in_(actor_commands),
        ApplicationLog.sync_id.in_(actor_syncs),
    )
    every_identifier_is_authorized = and_(
        or_(ApplicationLog.home_id.is_(None), ApplicationLog.home_id.in_(actor_homes)),
        or_(
            ApplicationLog.device_id.is_(None),
            ApplicationLog.device_id.in_(actor_devices),
        ),
        or_(
            ApplicationLog.command_id.is_(None),
            ApplicationLog.command_id.in_(actor_commands),
        ),
        or_(ApplicationLog.sync_id.is_(None), ApplicationLog.sync_id.in_(actor_syncs)),
    )
    logs = (
        await session.scalars(
            select(ApplicationLog)
            .where(has_actor_scope, every_identifier_is_authorized)
            .order_by(ApplicationLog.created_at.desc())
            .limit(2000)
        )
    ).all()
    generated_at = datetime.now(UTC).isoformat()
    health = DiagnosticHealth(
        generated_at=generated_at,
        version=VERSION,
        protocol=PROTOCOL_ID,
        correlation_id=request.state.correlation_id,
        physical_hardware_certification="pending",
    )
    event_bytes: list[bytes] = []
    for row in logs:
        safe_details, excluded = _safe_diagnostic_details(row.details)
        event_bytes.append(
            _validated_json_bytes(
                DiagnosticEvent(
                    created_at=row.created_at.isoformat(),
                    event_code=row.event_code,
                    level=row.level,
                    correlation_id=row.correlation_id,
                    device_id=row.device_id,
                    command_id=row.command_id,
                    sync_id=row.sync_id,
                    details=safe_details,
                    excluded_detail_fields=excluded,
                )
            )
        )
    log_content = b"\n".join(event_bytes)
    if event_bytes:
        log_content += b"\n"
    health_content = _validated_json_bytes(health)
    member_contents = {
        "health.json": ("application/json", health_content),
        "application-logs.jsonl": ("application/x-ndjson", log_content),
    }
    members = [
        DiagnosticMember(
            path=path,
            media_type=media_type,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        for path, (media_type, content) in member_contents.items()
    ]
    manifest_content = _validated_json_bytes(
        DiagnosticManifest(generated_at=generated_at, members=members)
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, (_media_type, content) in member_contents.items():
            archive.writestr(path, content)
        archive.writestr("manifest.json", manifest_content)
    body = buffer.getvalue()
    return Response(
        body,
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=powermeter-diagnostics.zip",
            "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
        },
    )
