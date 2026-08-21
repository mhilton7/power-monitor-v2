from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path as LocalPath
from typing import Any

import structlog
from anyio import Path, to_thread
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..constants import MAX_FIRMWARE_BYTES, PROTOCOL_ID
from ..db import get_session
from ..errors import IntegrityConflict, InvalidRequest, NotFound, OTAWorkflowError
from ..models import (
    AuditEvent,
    Device,
    DeviceCommand,
    DeviceCredential,
    FirmwareDeployment,
    FirmwareDeploymentBatch,
    FirmwareLifecycleSetting,
    FirmwareRelease,
    user_home_scopes,
)
from ..schemas.api import (
    FirmwareArchiveRequest,
    FirmwareCurrentRequest,
    FirmwareDeploymentArchiveRequest,
    FirmwareDeploymentDeleteRequest,
    FirmwareDeploymentHoldRequest,
    FirmwareDeploymentRequest,
    FirmwareDeploymentRestoreRequest,
    FirmwareDeploymentRetryRequest,
    FirmwareLifecycleSettingsUpdateRequest,
    FirmwareReleaseDeleteRequest,
    FirmwareRestoreRequest,
    FirmwareRollbackPinRequest,
)
from ..security.auth import CurrentUser, require_permission
from ..security.crypto import decrypt_secret
from ..security.device_auth import authenticate_device_request
from ..security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
)
from ..services.commands import create_command
from ..services.firmware_deployments import (
    ACTIVE_FIRMWARE_DEPLOYMENT_STATES,
    ACTIVE_OTA_COMMAND_STATES,
    TERMINAL_FIRMWARE_DEPLOYMENT_STATES,
    durable_replace,
    durable_unlink,
    durable_write_bytes,
    firmware_artifact_matches,
    lock_firmware_ota_graph,
    parse_esp32s3_app_identity,
    recalculate_firmware_batch,
    reconcile_firmware_artifact_quarantines,
    reconcile_stale_firmware_deployments,
)
from ..services.firmware_deployments import (
    lock_active_ota_commands_for_deployments as lock_active_ota_commands_for_deployments,
)

router = APIRouter(prefix="/api/v1", tags=["firmware"])
logger = structlog.get_logger()
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
OTA_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-rc\.([1-9]\d*))?$")


def _firmware_upgrade_available(installed: str | None, candidate: str) -> bool:
    """Mirror the device's strict stable/rc numeric upgrade ordering when parseable."""
    if not installed:
        return True
    current_match = OTA_VERSION.fullmatch(installed)
    candidate_match = OTA_VERSION.fullmatch(candidate)
    if current_match is None or candidate_match is None:
        # Unknown legacy identities still reach the device's fail-closed parser;
        # the server must not invent an ordering for them.
        return True

    def ordered(match: re.Match[str]) -> tuple[int, int, int, int, int]:
        major, minor, patch = (int(match.group(index)) for index in range(1, 4))
        release_candidate = match.group(4)
        return (
            major,
            minor,
            patch,
            1 if release_candidate is None else 0,
            int(release_candidate) if release_candidate is not None else 0,
        )

    return ordered(candidate_match) > ordered(current_match)


def _release_manifest(release: FirmwareRelease) -> dict[str, object]:
    return {
        "schema": "pm-ota-manifest/1.0.0",
        "release_id": release.id,
        "semantic_version": release.semantic_version,
        "build_number": int(release.build_number),
        "project_name": release.project_name,
        "target_chip": release.target_chip,
        "board_profile": release.board_profile,
        "minimum_boot_version": release.minimum_boot_version,
        "minimum_protocol": release.minimum_protocol,
        "minimum_config_version": release.minimum_config_version,
        "image_size": release.image_size,
        "sha256": release.sha256,
        "firmware_build_id": release.firmware_build_id,
        "candidate": release.candidate,
    }


def _manifest_signature(key: bytes, manifest: dict[str, object]) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return base64.b64encode(hmac.new(key, canonical, hashlib.sha256).digest()).decode()


OTA_MANIFEST_FIELDS = (
    "schema",
    "device_id",
    "deployment_id",
    "release_id",
    "semantic_version",
    "build_number",
    "project_name",
    "target_chip",
    "board_profile",
    "minimum_boot_version",
    "minimum_config_version",
    "minimum_protocol",
    "image_size",
    "sha256",
    "download_path",
    "manifest_nonce",
)


def ota_manifest_canonical(manifest: dict[str, Any]) -> bytes:
    """Return the byte-exact per-device OTA manifest contract."""
    if set(manifest) - {"signature"} != set(OTA_MANIFEST_FIELDS):
        raise ValueError("OTA manifest fields do not match the locked contract")
    integer_fields = {
        "build_number",
        "minimum_boot_version",
        "minimum_config_version",
        "image_size",
    }
    values: list[str] = []
    for name in OTA_MANIFEST_FIELDS:
        value = manifest[name]
        if name in integer_fields:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"OTA manifest {name} must be a positive integer")
        elif not isinstance(value, str) or not value:
            raise ValueError(f"OTA manifest {name} must be a non-empty string")
        values.append(str(value))
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest["sha256"])):
        raise ValueError("OTA manifest SHA-256 must be lowercase hex")
    if not re.fullmatch(r"[0-9a-f]{32}", str(manifest["manifest_nonce"])):
        raise ValueError("OTA manifest nonce must be lowercase 128-bit hex")
    download_path = str(manifest["download_path"])
    if not re.fullmatch(r"/api/v1/device/firmware/[0-9a-f-]{36}", download_path):
        raise ValueError("OTA manifest download path is invalid")
    return ("PM-OTA-MANIFEST-V1\n" + "\n".join(values)).encode("utf-8")


async def _device_manifest_key(session: AsyncSession, settings: Settings, device_id: str) -> bytes:
    credential = await session.scalar(
        select(DeviceCredential)
        .where(
            DeviceCredential.device_id == device_id,
            DeviceCredential.revoked_at.is_(None),
            DeviceCredential.state == "active",
        )
        .order_by(DeviceCredential.key_version.desc())
    )
    if credential is None:
        raise IntegrityConflict("target device has no active credential")
    secret = decrypt_secret(
        settings.master_key,
        credential.encrypted_secret,
        context=device_id.encode(),
    )
    return derive_directional_key(secret, device_id, "server-to-device")


async def _ota_command_manifest(
    *,
    session: AsyncSession,
    settings: Settings,
    release: FirmwareRelease,
    deployment: FirmwareDeployment,
    device: Device,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema": "pm-ota-manifest/1.0.0",
        "deployment_id": deployment.id,
        "release_id": release.id,
        "device_id": device.id,
        "semantic_version": release.semantic_version,
        "build_number": int(release.build_number),
        "project_name": release.project_name,
        "target_chip": release.target_chip,
        "board_profile": release.board_profile,
        "minimum_boot_version": release.minimum_boot_version,
        "minimum_config_version": release.minimum_config_version,
        "minimum_protocol": release.minimum_protocol,
        "image_size": release.image_size,
        "sha256": release.sha256,
        "download_path": f"/api/v1/device/firmware/{release.id}",
        "manifest_nonce": secrets.token_hex(16),
    }
    key = await _device_manifest_key(session, settings, device.id)
    manifest["signature"] = sign_request(key, ota_manifest_canonical(manifest))
    return manifest


async def _home_ids(session: AsyncSession, user_id: str) -> tuple[str, ...]:
    return tuple(
        (
            await session.scalars(
                select(user_home_scopes.c.home_id).where(user_home_scopes.c.user_id == user_id)
            )
        ).all()
    )


def _canonical_artifact_path(release: FirmwareRelease, settings: Settings) -> LocalPath | None:
    if not release.image_path:
        return None
    firmware_root = LocalPath(settings.firmware_dir).resolve()
    configured_path = LocalPath(release.image_path)
    if configured_path.is_symlink():
        return None
    stored_path = configured_path.resolve()
    expected_path = firmware_root / f"{release.id}.bin"
    if stored_path != expected_path or stored_path.parent != firmware_root:
        return None
    return stored_path


async def _artifact_available(release: FirmwareRelease, settings: Settings) -> bool:
    stored_path = _canonical_artifact_path(release, settings)
    if stored_path is None:
        return False
    return await to_thread.run_sync(
        firmware_artifact_matches,
        stored_path,
        release.image_size,
        release.sha256,
    )


def _build_identity_available(release: FirmwareRelease) -> bool:
    return (
        release.firmware_build_id is not None
        and re.fullmatch(r"[0-9a-f]{64}", release.firmware_build_id) is not None
    )


async def _shared_artifact_reference_ids(
    session: AsyncSession,
    release: FirmwareRelease,
    *,
    candidate_releases: Sequence[FirmwareRelease] | None = None,
) -> tuple[str, ...]:
    if not release.image_path:
        return ()
    target = LocalPath(release.image_path).resolve()
    rows = (
        list(candidate_releases)
        if candidate_releases is not None
        else list(
            (
                await session.scalars(
                    select(FirmwareRelease)
                    .where(
                        FirmwareRelease.id != release.id,
                        FirmwareRelease.lifecycle_state != "deleted",
                        FirmwareRelease.image_path != "",
                    )
                    .order_by(FirmwareRelease.id)
                )
            ).all()
        )
    )
    references: list[str] = []
    for row in rows:
        if row.id == release.id or row.lifecycle_state == "deleted" or not row.image_path:
            continue
        try:
            same_path = LocalPath(row.image_path).resolve() == target
        except OSError:
            same_path = row.image_path == release.image_path
        if same_path:
            references.append(row.id)
    return tuple(references)


def _quarantine_artifact_file(
    release: FirmwareRelease, settings: Settings
) -> tuple[LocalPath | None, LocalPath | None]:
    if not release.image_path:
        return None, None
    firmware_root = LocalPath(settings.firmware_dir).resolve()
    configured_path = LocalPath(release.image_path)
    if configured_path.is_symlink():
        raise IntegrityConflict("firmware artifact path is outside the configured store")
    stored_path = configured_path.resolve()
    expected_path = firmware_root / f"{release.id}.bin"
    if stored_path.parent != firmware_root or stored_path != expected_path:
        raise IntegrityConflict("firmware artifact path does not match the release identity")
    quarantine_path: LocalPath | None = None
    if stored_path.is_file():
        quarantine_path = firmware_root / (f".{release.id}.{secrets.token_hex(8)}.pending-delete")
        durable_replace(stored_path, quarantine_path)
    release.image_path = ""
    release.artifact_deleted_at = datetime.now(UTC)
    return stored_path, quarantine_path


def _restore_quarantined_artifact(
    original_path: LocalPath | None, quarantine_path: LocalPath | None
) -> None:
    if original_path is None or quarantine_path is None or not quarantine_path.is_file():
        return
    durable_replace(quarantine_path, original_path)


def _purge_quarantined_artifact(quarantine_path: LocalPath | None) -> None:
    if quarantine_path is not None:
        durable_unlink(quarantine_path, missing_ok=True)


def _attempt_state(state: str) -> str:
    return {
        "staged": "waiting",
        "queued": "waiting",
        "downloading": "downloading",
        "installing": "installing",
        "rebooting": "restarting",
        "validating": "confirming",
        "succeeded": "updated",
        "rolled_back": "failed",
        "failed": "failed",
        "timed_out": "timed_out",
        "cancelled": "canceled",
    }.get(state, "failed")


async def _release_lifecycle_view(
    session: AsyncSession,
    release: FirmwareRelease,
    *,
    artifact_available: bool,
    settings: Settings,
) -> dict[str, object]:
    build_identity_available = _build_identity_available(release)
    deployments = list(
        (
            await session.scalars(
                select(FirmwareDeployment).where(
                    FirmwareDeployment.firmware_release_id == release.id
                )
            )
        ).all()
    )
    deployment_count = len(deployments)
    active_count = sum(
        deployment.state in ACTIVE_FIRMWARE_DEPLOYMENT_STATES for deployment in deployments
    )
    deployment_ids = {deployment.id for deployment in deployments}
    active_commands = list(
        (
            await session.scalars(
                select(DeviceCommand).where(
                    DeviceCommand.device_id.in_(
                        {deployment.device_id for deployment in deployments}
                    ),
                    DeviceCommand.command_type == "ota_install",
                    DeviceCommand.state.in_(ACTIVE_OTA_COMMAND_STATES),
                )
            )
        ).all()
    )
    active_ota_reference_count = sum(
        command.payload.get("deployment_id") in deployment_ids for command in active_commands
    )
    versions = {
        release.semantic_version,
        release.semantic_version.removeprefix("v"),
        f"v{release.semantic_version.removeprefix('v')}",
    }
    sensor_count = int(
        await session.scalar(
            select(func.count(Device.id)).where(Device.firmware_version.in_(versions))
        )
        or 0
    )
    shared_artifact_reference_ids = await _shared_artifact_reference_ids(session, release)
    protections: list[str] = []
    if release.lifecycle_state == "current":
        protections.append("current_recommended_release")
    if active_count:
        protections.append("active_or_pending_deployment")
    if active_ota_reference_count:
        protections.append("active_ota_action_reference")
    if sensor_count:
        protections.append("reported_by_sensor")
    if release.rollback_pinned:
        protections.append("pinned_for_rollback")
    if shared_artifact_reference_ids:
        protections.append("shared_artifact_reference")
    if release.lifecycle_state == "deleted":
        protections.append("already_deleted")
    consistency_issues: list[str] = []
    if release.lifecycle_state in {"available", "current"} and not artifact_available:
        consistency_issues.append("deployable_state_without_artifact")
    if release.lifecycle_state in {"available", "current"} and not build_identity_available:
        consistency_issues.append("deployable_state_without_build_identity")
    if release.lifecycle_state == "deleted" and artifact_available:
        consistency_issues.append("deleted_state_with_artifact")
    if release.lifecycle_state == "archived" and not artifact_available:
        consistency_issues.append("archived_artifact_unavailable")
    if release.lifecycle_state == "deleted" and active_count:
        consistency_issues.append("deleted_release_has_active_deployment")
    if release.lifecycle_state == "deleted" and active_ota_reference_count:
        consistency_issues.append("deleted_release_has_active_ota_reference")
    if release.lifecycle_state == "deleted" and sensor_count:
        consistency_issues.append("deleted_release_reported_by_sensor")
    if shared_artifact_reference_ids:
        consistency_issues.append("shared_artifact_reference")
    if release.image_path and _canonical_artifact_path(release, settings) is None:
        consistency_issues.append("noncanonical_artifact_path")
    return {
        "release_state": release.lifecycle_state,
        "archived_at": release.archived_at,
        "deleted_at": release.deleted_at,
        "artifact_deleted_at": release.artifact_deleted_at,
        "rollback_pinned": release.rollback_pinned,
        "deployment_count": deployment_count,
        "active_deployment_count": active_count,
        "active_ota_reference_count": active_ota_reference_count,
        "sensor_reported_count": sensor_count,
        "shared_artifact_reference_count": len(shared_artifact_reference_ids),
        "deploy_eligible": release.lifecycle_state in {"available", "current"}
        and artifact_available
        and build_identity_available,
        "make_current_eligible": release.lifecycle_state == "available"
        and artifact_available
        and build_identity_available,
        "rollback_pin_eligible": release.lifecycle_state == "available"
        and artifact_available
        and build_identity_available
        and not release.rollback_pinned,
        "rollback_unpin_eligible": release.lifecycle_state != "deleted" and release.rollback_pinned,
        "archive_eligible": release.lifecycle_state in {"available", "rejected"}
        and active_count == 0,
        "restore_eligible": release.lifecycle_state == "archived",
        "delete_eligibility": {
            "eligible": not protections,
            "protection_reasons": protections,
        },
        "consistency": {
            "status": "consistent" if not consistency_issues else "attention_required",
            "issues": consistency_issues,
        },
    }


def _deployment_view(
    deployment: FirmwareDeployment,
    release: FirmwareRelease,
    device: Device,
    *,
    release_retry_eligible: bool,
) -> dict[str, object]:
    return {
        "id": deployment.id,
        "device_id": deployment.device_id,
        "device_name": device.friendly_name,
        "previous_version": deployment.evidence.get("previous_firmware_version"),
        "current_version": device.firmware_version,
        "target_version": release.semantic_version,
        "target_build": int(release.build_number),
        "target_firmware_build_id": release.firmware_build_id,
        "state": deployment.state,
        "attempt_state": _attempt_state(deployment.state),
        "progress_percent": deployment.progress_percent,
        "attempt": deployment.attempt,
        "error_code": deployment.error_code,
        "error_message": deployment.error_message,
        "created_at": deployment.created_at,
        "updated_at": deployment.updated_at,
        "completed_at": deployment.completed_at,
        "confirmation_heartbeat_at": deployment.evidence.get("post_reboot_confirmed_at"),
        "reported_firmware_after_reboot": deployment.evidence.get("post_reboot_firmware_version"),
        "reported_firmware_build_id_after_reboot": deployment.evidence.get(
            "post_reboot_firmware_build_id"
        ),
        "retry_eligible": deployment.state in {"failed", "rolled_back", "timed_out", "cancelled"}
        and release_retry_eligible
        and _firmware_upgrade_available(device.firmware_version, release.semantic_version),
        "cancel_eligible": deployment.state in {"staged", "queued"},
    }


def _batch_view(
    batch: FirmwareDeploymentBatch,
    release: FirmwareRelease,
    rows: list[tuple[FirmwareDeployment, Device]],
    *,
    release_retry_eligible: bool,
) -> dict[str, object]:
    succeeded = sum(deployment.state == "succeeded" for deployment, _device in rows)
    failed = sum(
        deployment.state in {"failed", "rolled_back", "timed_out", "cancelled"}
        for deployment, _device in rows
    )
    pending = len(rows) - succeeded - failed
    archived = batch.archived_at is not None
    deleted = batch.deleted_at is not None
    terminal = not any(
        deployment.state in ACTIVE_FIRMWARE_DEPLOYMENT_STATES for deployment, _device in rows
    )
    protections: list[str] = []
    if not terminal:
        protections.append("deployment_not_terminal")
    if batch.troubleshooting_hold:
        protections.append("troubleshooting_hold")
    if not archived:
        protections.append("archive_required_before_deletion")
    if deleted:
        protections.append("already_deleted")
    display_state = (
        "deleted"
        if deleted
        else "archived"
        if archived
        else "canceled"
        if batch.state == "cancelled"
        else batch.state
    )
    jobs = [
        _deployment_view(
            deployment,
            release,
            device,
            release_retry_eligible=release_retry_eligible,
        )
        for deployment, device in rows
    ]
    if archived or deleted:
        for job in jobs:
            job["retry_eligible"] = False
    return {
        "id": batch.id,
        "release_id": batch.firmware_release_id,
        "target_version": release.semantic_version,
        "rollout": batch.rollout,
        "state": display_state,
        "deployment_state": display_state,
        "result_state": "canceled" if batch.state == "cancelled" else batch.state,
        "targeted": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "completed_at": batch.completed_at,
        "archived_at": batch.archived_at,
        "deleted_at": batch.deleted_at,
        "troubleshooting_hold": batch.troubleshooting_hold,
        "archive_eligible": terminal and not archived and not deleted,
        "restore_eligible": archived and not deleted,
        "delete_eligibility": {
            "eligible": not protections,
            "protection_reasons": protections,
        },
        "jobs": jobs,
    }


async def _batch_for_user(
    session: AsyncSession,
    *,
    batch_id: str,
    user_id: str,
    lock: bool,
    commands_locked: bool = False,
) -> tuple[FirmwareDeploymentBatch, FirmwareRelease, list[tuple[FirmwareDeployment, Device]]]:
    batch_statement = select(FirmwareDeploymentBatch).where(FirmwareDeploymentBatch.id == batch_id)
    batch = await session.scalar(batch_statement)
    if batch is None:
        raise NotFound("firmware deployment batch does not exist")
    release = await session.get(FirmwareRelease, batch.firmware_release_id)
    if release is None:
        raise IntegrityConflict("firmware deployment release evidence is missing")
    row_statement = (
        select(FirmwareDeployment, Device)
        .join(Device, Device.id == FirmwareDeployment.device_id)
        .where(FirmwareDeployment.batch_id == batch.id)
        .order_by(FirmwareDeployment.id)
    )
    rows = list((await session.execute(row_statement)).tuples().all())
    homes = set(await _home_ids(session, user_id))
    if not rows or any(device.home_id not in homes for _deployment, device in rows):
        raise NotFound("firmware deployment batch does not exist")
    if not lock:
        return batch, release, rows

    graph = await lock_firmware_ota_graph(
        session,
        [deployment for deployment, _device in rows],
        lock_commands=not commands_locked,
    )
    locked_batch = next((row for row in graph.batches if row.id == batch.id), None)
    locked_release = next((row for row in graph.releases if row.id == release.id), None)
    locked_deployments = [
        deployment for deployment in graph.deployments if deployment.batch_id == batch.id
    ]
    if locked_batch is None or locked_release is None:
        raise IntegrityConflict("firmware deployment lifecycle evidence changed")
    if {deployment.id for deployment in locked_deployments} != {
        deployment.id for deployment, _device in rows
    }:
        raise IntegrityConflict("firmware deployment references changed; retry the request")
    devices_by_id = {device.id: device for _deployment, device in rows}
    locked_rows = [
        (deployment, devices_by_id[deployment.device_id])
        for deployment in locked_deployments
        if deployment.device_id in devices_by_id
    ]
    homes = set(await _home_ids(session, user_id))
    if len(locked_rows) != len(locked_deployments) or any(
        device.home_id not in homes for _deployment, device in locked_rows
    ):
        raise NotFound("firmware deployment batch does not exist")
    return locked_batch, locked_release, locked_rows


async def _locked_batch_for_user(
    session: AsyncSession,
    *,
    batch_id: str,
    user_id: str,
    commands_locked: bool = False,
) -> tuple[FirmwareDeploymentBatch, FirmwareRelease, list[tuple[FirmwareDeployment, Device]]]:
    return await _batch_for_user(
        session,
        batch_id=batch_id,
        user_id=user_id,
        lock=True,
        commands_locked=commands_locked,
    )


async def _firmware_lifecycle_settings(session: AsyncSession) -> FirmwareLifecycleSetting:
    settings = await session.get(FirmwareLifecycleSetting, "global")
    if settings is not None:
        return settings
    candidate = FirmwareLifecycleSetting(id="global", deployment_retention_days=365)
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
        return candidate
    except IntegrityError:
        settings = await session.get(FirmwareLifecycleSetting, "global")
        if settings is None:
            raise
        return settings


async def _lock_firmware_lifecycle_coordinator(
    session: AsyncSession,
) -> FirmwareLifecycleSetting:
    settings = await _firmware_lifecycle_settings(session)
    locked = await session.scalar(
        select(FirmwareLifecycleSetting)
        .where(FirmwareLifecycleSetting.id == settings.id)
        .with_for_update()
    )
    if locked is None:
        raise IntegrityConflict("firmware lifecycle coordinator is unavailable")
    return locked


async def _make_release_current(
    session: AsyncSession,
    *,
    release: FirmwareRelease,
    actor_user_id: str,
    correlation_id: str,
    reason: str,
) -> tuple[str, ...]:
    await _lock_firmware_lifecycle_coordinator(session)
    prior_current = list(
        (
            await session.scalars(
                select(FirmwareRelease)
                .where(
                    FirmwareRelease.lifecycle_state == "current",
                    FirmwareRelease.id != release.id,
                )
                .with_for_update()
            )
        ).all()
    )
    changed_at = datetime.now(UTC)
    for prior in prior_current:
        prior.lifecycle_state = "available"
        prior.rollback_pinned = True
        prior.updated_at = changed_at
    # Flush demotions before inserting/promoting the unique partial-index row.
    await session.flush()
    release.lifecycle_state = "current"
    release.rollback_pinned = False
    release.archived_at = None
    release.archived_by_user_id = None
    release.updated_at = changed_at
    session.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            event_code="FIRMWARE_CURRENT_RELEASE_CHANGED",
            target_type="firmware_release",
            target_id=release.id,
            correlation_id=correlation_id,
            details={
                "semantic_version": release.semantic_version,
                "sha256": release.sha256,
                "prior_current_release_ids": [prior.id for prior in prior_current],
                "prior_releases_rollback_pinned": True,
                "reason": reason,
            },
        )
    )
    return tuple(prior.id for prior in prior_current)


def _lifecycle_settings_view(settings: FirmwareLifecycleSetting) -> dict[str, object]:
    days = settings.deployment_retention_days
    return {
        "deployment_retention_days": days,
        "retention_policy": "indefinitely" if days is None else f"{days}_days",
        "automatic_cleanup_scope": "archived_terminal_deployment_tombstones_only",
        "firmware_artifacts_affected": False,
        "active_deployments_affected": False,
        "updated_at": settings.updated_at,
    }


@router.get("/firmware/deployment-batches")
async def list_firmware_deployment_batches(
    show_archived: bool = False,
    show_deleted: bool = False,
    user: CurrentUser = Depends(require_permission("firmware.view")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    expired = await reconcile_stale_firmware_deployments(session)
    if expired:
        await session.commit()
    home_ids = await _home_ids(session, user.id)
    batches = list(
        (
            await session.scalars(
                select(FirmwareDeploymentBatch).order_by(
                    FirmwareDeploymentBatch.created_at.desc(),
                    FirmwareDeploymentBatch.id.desc(),
                )
            )
        ).all()
    )
    results: list[dict[str, object]] = []
    for batch in batches:
        if batch.deleted_at is not None and not show_deleted:
            continue
        if batch.archived_at is not None and batch.deleted_at is None and not show_archived:
            continue
        release = await session.get(FirmwareRelease, batch.firmware_release_id)
        if release is None:
            continue
        release_retry_eligible = (
            release.lifecycle_state in {"available", "current"}
            and _build_identity_available(release)
            and await _artifact_available(release, settings)
        )
        rows = list(
            (
                await session.execute(
                    select(FirmwareDeployment, Device)
                    .join(Device, Device.id == FirmwareDeployment.device_id)
                    .where(
                        FirmwareDeployment.batch_id == batch.id,
                        Device.home_id.in_(home_ids),
                    )
                    .order_by(FirmwareDeployment.created_at, FirmwareDeployment.id)
                )
            )
            .tuples()
            .all()
        )
        if rows:
            results.append(
                _batch_view(
                    batch,
                    release,
                    rows,
                    release_retry_eligible=release_retry_eligible,
                )
            )
    return {
        "deployment_batches": results,
        "filters": {"show_archived": show_archived, "show_deleted": show_deleted},
    }


@router.get("/firmware/lifecycle-settings")
async def get_firmware_lifecycle_settings(
    user: CurrentUser = Depends(require_permission("firmware.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    del user
    settings = await _firmware_lifecycle_settings(session)
    return _lifecycle_settings_view(settings)


@router.patch("/firmware/lifecycle-settings")
async def update_firmware_lifecycle_settings(
    payload: FirmwareLifecycleSettingsUpdateRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    current_settings = await _firmware_lifecycle_settings(session)
    settings = await session.scalar(
        select(FirmwareLifecycleSetting)
        .where(FirmwareLifecycleSetting.id == current_settings.id)
        .with_for_update()
    )
    if settings is None:
        raise IntegrityConflict("firmware lifecycle settings are unavailable")
    prior_days = settings.deployment_retention_days
    settings.deployment_retention_days = payload.deployment_retention_days
    settings.updated_by_user_id = user.id
    settings.updated_at = datetime.now(UTC)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_RETENTION_UPDATED",
            target_type="firmware_lifecycle_settings",
            target_id=settings.id,
            correlation_id=request.state.correlation_id,
            details={
                "prior_retention_days": prior_days,
                "retention_days": payload.deployment_retention_days,
                "artifact_cleanup_allowed": False,
            },
        )
    )
    await session.commit()
    return _lifecycle_settings_view(settings)


@router.post("/firmware/deployment-batches/{batch_id}/archive")
async def archive_firmware_deployment_batch(
    batch_id: str,
    payload: FirmwareDeploymentArchiveRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    del payload
    batch, release, rows = await _locked_batch_for_user(session, batch_id=batch_id, user_id=user.id)
    if batch.deleted_at is not None:
        raise IntegrityConflict("a deleted firmware deployment cannot be archived")
    if batch.archived_at is not None:
        raise IntegrityConflict("firmware deployment is already archived")
    if any(
        deployment.state not in TERMINAL_FIRMWARE_DEPLOYMENT_STATES for deployment, _device in rows
    ):
        raise IntegrityConflict("only a terminal firmware deployment can be archived")
    archived_at = datetime.now(UTC)
    batch.archived_at = archived_at
    batch.archived_by_user_id = user.id
    batch.updated_at = archived_at
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_ARCHIVED",
            target_type="firmware_deployment_batch",
            target_id=batch.id,
            correlation_id=request.state.correlation_id,
            details={
                "release_id": release.id,
                "deployment_count": len(rows),
                "final_state": batch.state,
                "artifact_preserved": True,
            },
        )
    )
    await session.commit()
    return _batch_view(batch, release, rows, release_retry_eligible=False)


@router.post("/firmware/deployment-batches/{batch_id}/restore")
async def restore_firmware_deployment_batch(
    batch_id: str,
    payload: FirmwareDeploymentRestoreRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    del payload
    batch, release, rows = await _locked_batch_for_user(session, batch_id=batch_id, user_id=user.id)
    if batch.deleted_at is not None:
        raise IntegrityConflict("a deleted firmware deployment cannot be restored")
    if batch.archived_at is None:
        raise IntegrityConflict("only an archived firmware deployment can be restored")
    restored_at = datetime.now(UTC)
    prior_archived_at = batch.archived_at
    batch.archived_at = None
    batch.archived_by_user_id = None
    batch.updated_at = restored_at
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_RESTORED",
            target_type="firmware_deployment_batch",
            target_id=batch.id,
            correlation_id=request.state.correlation_id,
            details={
                "release_id": release.id,
                "prior_archived_at": prior_archived_at.isoformat(),
                "deployment_count": len(rows),
            },
        )
    )
    await session.commit()
    release_retry_eligible = (
        release.lifecycle_state in {"available", "current"}
        and _build_identity_available(release)
        and await _artifact_available(release, settings)
    )
    return _batch_view(
        batch,
        release,
        rows,
        release_retry_eligible=release_retry_eligible,
    )


@router.patch("/firmware/deployment-batches/{batch_id}/troubleshooting-hold")
async def set_firmware_deployment_troubleshooting_hold(
    batch_id: str,
    payload: FirmwareDeploymentHoldRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    batch, release, rows = await _locked_batch_for_user(session, batch_id=batch_id, user_id=user.id)
    if batch.deleted_at is not None:
        raise IntegrityConflict("a deleted deployment cannot carry a troubleshooting hold")
    prior = batch.troubleshooting_hold
    batch.troubleshooting_hold = payload.troubleshooting_hold
    batch.updated_at = datetime.now(UTC)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_HOLD_UPDATED",
            target_type="firmware_deployment_batch",
            target_id=batch.id,
            correlation_id=request.state.correlation_id,
            details={
                "prior_hold": prior,
                "troubleshooting_hold": payload.troubleshooting_hold,
                "reason": payload.reason,
            },
        )
    )
    await session.commit()
    release_retry_eligible = (
        release.lifecycle_state in {"available", "current"}
        and _build_identity_available(release)
        and await _artifact_available(release, settings)
    )
    return _batch_view(
        batch,
        release,
        rows,
        release_retry_eligible=release_retry_eligible,
    )


@router.post("/firmware/deployment-batches/{batch_id}/delete-permanently", status_code=204)
async def permanently_delete_firmware_deployment_batch(
    batch_id: str,
    payload: FirmwareDeploymentDeleteRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
) -> None:
    if payload.deployment_batch_id != batch_id:
        raise IntegrityConflict("firmware deployment deletion confirmation does not match")
    _preflight_batch, _preflight_release, preflight_rows = await _batch_for_user(
        session, batch_id=batch_id, user_id=user.id, lock=False
    )
    if any(
        deployment.state not in TERMINAL_FIRMWARE_DEPLOYMENT_STATES
        for deployment, _device in preflight_rows
    ):
        raise IntegrityConflict("only terminal firmware deployments can be deleted")
    locked_commands = await lock_active_ota_commands_for_deployments(
        session, [deployment for deployment, _device in preflight_rows]
    )
    batch, release, rows = await _locked_batch_for_user(
        session,
        batch_id=batch_id,
        user_id=user.id,
        commands_locked=True,
    )
    if {deployment.id for deployment, _device in rows} != {
        deployment.id for deployment, _device in preflight_rows
    }:
        raise IntegrityConflict("firmware deployment references changed; retry deletion")
    if batch.deleted_at is not None:
        raise IntegrityConflict("firmware deployment is already deleted")
    if batch.archived_at is None:
        raise IntegrityConflict("firmware deployment must be archived before deletion")
    if batch.troubleshooting_hold:
        raise IntegrityConflict("an active troubleshooting hold protects this deployment")
    if any(
        deployment.state not in TERMINAL_FIRMWARE_DEPLOYMENT_STATES for deployment, _device in rows
    ):
        raise IntegrityConflict("only terminal firmware deployments can be deleted")
    deployment_ids = {deployment.id for deployment, _device in rows}
    if any(command.payload.get("deployment_id") in deployment_ids for command in locked_commands):
        raise IntegrityConflict("an active OTA action still references this deployment")
    deleted_at = datetime.now(UTC)
    final_states = {deployment.id: deployment.state for deployment, _device in rows}
    for deployment, _device in rows:
        retained_evidence = {
            key: deployment.evidence[key]
            for key in (
                "previous_firmware_version",
                "post_reboot_firmware_version",
                "expected_firmware_build_id",
                "post_reboot_firmware_build_id",
                "post_reboot_confirmed_at",
                "server_result_code",
            )
            if key in deployment.evidence
        }
        deployment.evidence = {
            **retained_evidence,
            "audit_tombstone": True,
            "details_deleted_at": deleted_at.isoformat(),
        }
        deployment.error_message = None
    batch.deleted_at = deleted_at
    batch.deleted_by_user_id = user.id
    batch.updated_at = deleted_at
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_DELETED",
            target_type="firmware_deployment_batch_tombstone",
            target_id=batch.id,
            correlation_id=request.state.correlation_id,
            details={
                "release_id": release.id,
                "final_state": batch.state,
                "per_sensor_final_states": final_states,
                "artifact_preserved": True,
                "audit_tombstone_retained": True,
            },
        )
    )
    await session.commit()


@router.get("/firmware/releases")
async def list_firmware_releases(
    show_archived: bool = False,
    show_deleted: bool = False,
    show_deployment_history: bool = False,
    user: CurrentUser = Depends(require_permission("firmware.view")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    expired = await reconcile_stale_firmware_deployments(session)
    if expired:
        await session.commit()
    homes = await _home_ids(session, user.id)
    rows = (
        await session.scalars(select(FirmwareRelease).order_by(FirmwareRelease.created_at.desc()))
    ).all()
    releases: list[dict[str, object]] = []
    for row in rows:
        if row.lifecycle_state == "archived" and not show_archived:
            continue
        if row.lifecycle_state == "deleted" and not show_deleted:
            continue
        artifact_available = await _artifact_available(row, settings)
        release_retry_eligible = (
            row.lifecycle_state in {"available", "current"}
            and _build_identity_available(row)
            and artifact_available
        )
        batches = list(
            (
                await session.scalars(
                    select(FirmwareDeploymentBatch)
                    .where(FirmwareDeploymentBatch.firmware_release_id == row.id)
                    .order_by(FirmwareDeploymentBatch.created_at.desc())
                )
            ).all()
        )
        batch_views: list[dict[str, object]] = []
        for batch in batches:
            if batch.deleted_at is not None:
                continue
            if batch.archived_at is not None and not show_deployment_history:
                continue
            deployment_rows = list(
                (
                    await session.execute(
                        select(FirmwareDeployment, Device)
                        .join(Device, Device.id == FirmwareDeployment.device_id)
                        .where(
                            FirmwareDeployment.batch_id == batch.id,
                            Device.home_id.in_(homes),
                        )
                        .order_by(FirmwareDeployment.created_at, FirmwareDeployment.id)
                    )
                )
                .tuples()
                .all()
            )
            if deployment_rows:
                batch_views.append(
                    _batch_view(
                        batch,
                        row,
                        deployment_rows,
                        release_retry_eligible=release_retry_eligible,
                    )
                )
        releases.append(
            {
                **_release_manifest(row),
                "release_notes": row.release_notes,
                "physical_certification": "pending" if row.candidate else "required",
                "artifact_available": artifact_available,
                "upload_status": "uploaded" if row.image_path else "artifact_removed",
                "validation_status": "ready" if row.image_path else "not_deployable",
                **await _release_lifecycle_view(
                    session,
                    row,
                    artifact_available=artifact_available,
                    settings=settings,
                ),
                "deployment_batches": batch_views,
            }
        )
    quarantine_diagnostics = await reconcile_firmware_artifact_quarantines(
        session,
        firmware_dir=LocalPath(settings.firmware_dir),
        apply=False,
    )
    return {
        "releases": releases,
        "filters": {
            "show_archived": show_archived,
            "show_deleted": show_deleted,
            "show_deployment_history": show_deployment_history,
        },
        "reconciliation": {
            "strategy": "explicit_state_and_artifact_evidence",
            "inconsistent_release_ids": [
                str(release["release_id"])
                for release in releases
                if release["consistency"]["status"] == "attention_required"  # type: ignore[index]
            ],
            "silent_deletion_performed": False,
            "artifact_quarantines": quarantine_diagnostics,
        },
    }


@router.post("/firmware/releases/{release_id}/archive")
async def archive_firmware_release(
    release_id: str,
    payload: FirmwareArchiveRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    release = await session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == release_id).with_for_update()
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    if release.lifecycle_state == "current":
        raise IntegrityConflict("the current recommended firmware release cannot be archived")
    if release.lifecycle_state == "deleted":
        raise IntegrityConflict("a deleted firmware release cannot be archived")
    if release.lifecycle_state == "archived":
        raise IntegrityConflict("firmware release is already archived")
    active = await session.scalar(
        select(FirmwareDeployment.id)
        .where(
            FirmwareDeployment.firmware_release_id == release.id,
            FirmwareDeployment.state.in_(ACTIVE_FIRMWARE_DEPLOYMENT_STATES),
        )
        .limit(1)
    )
    if active is not None:
        raise IntegrityConflict("firmware release cannot be archived during an active deployment")
    archived_at = datetime.now(UTC)
    release.lifecycle_state = "archived"
    release.archived_at = archived_at
    release.archived_by_user_id = user.id
    release.updated_at = archived_at
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_RELEASE_ARCHIVED",
            target_type="firmware_release",
            target_id=release.id,
            correlation_id=request.state.correlation_id,
            details={
                "semantic_version": release.semantic_version,
                "build_number": release.build_number,
                "sha256": release.sha256,
                "artifact_preserved": bool(release.image_path),
            },
        )
    )
    await session.commit()
    artifact_available = await _artifact_available(release, settings)
    return {
        **_release_manifest(release),
        "artifact_available": artifact_available,
        **await _release_lifecycle_view(
            session,
            release,
            artifact_available=artifact_available,
            settings=settings,
        ),
    }


@router.post("/firmware/releases/{release_id}/restore")
async def restore_firmware_release(
    release_id: str,
    payload: FirmwareRestoreRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    release = await session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == release_id).with_for_update()
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    if release.lifecycle_state != "archived":
        raise IntegrityConflict("only an archived firmware release can be restored")
    if not await _artifact_available(release, settings):
        raise IntegrityConflict(
            "the archived firmware artifact is unavailable and cannot be restored"
        )
    restored_at = datetime.now(UTC)
    prior_archived_at = release.archived_at
    release.lifecycle_state = "available"
    release.archived_at = None
    release.archived_by_user_id = None
    release.updated_at = restored_at
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_RELEASE_RESTORED",
            target_type="firmware_release",
            target_id=release.id,
            correlation_id=request.state.correlation_id,
            details={
                "semantic_version": release.semantic_version,
                "sha256": release.sha256,
                "prior_archived_at": prior_archived_at.isoformat()
                if prior_archived_at is not None
                else None,
            },
        )
    )
    await session.commit()
    return {
        **_release_manifest(release),
        "artifact_available": True,
        **await _release_lifecycle_view(
            session, release, artifact_available=True, settings=settings
        ),
    }


@router.post("/firmware/releases/{release_id}/make-current")
async def make_firmware_release_current(
    release_id: str,
    payload: FirmwareCurrentRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await _lock_firmware_lifecycle_coordinator(session)
    release = await session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == release_id).with_for_update()
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    if payload.semantic_version != release.semantic_version or not hmac.compare_digest(
        payload.sha256, release.sha256
    ):
        raise IntegrityConflict("current firmware confirmation does not match")
    if release.lifecycle_state != "available":
        raise IntegrityConflict("only an available firmware release can become current")
    if not await _artifact_available(release, settings):
        raise IntegrityConflict("firmware artifact is unavailable")
    if not _build_identity_available(release):
        raise IntegrityConflict("firmware build identity is unavailable")
    await _make_release_current(
        session,
        release=release,
        actor_user_id=user.id,
        correlation_id=request.state.correlation_id,
        reason="administrator_selected",
    )
    await session.commit()
    return {
        **_release_manifest(release),
        "artifact_available": True,
        **await _release_lifecycle_view(
            session, release, artifact_available=True, settings=settings
        ),
    }


@router.patch("/firmware/releases/{release_id}/rollback-pin")
async def update_firmware_release_rollback_pin(
    release_id: str,
    payload: FirmwareRollbackPinRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await _lock_firmware_lifecycle_coordinator(session)
    release = await session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == release_id).with_for_update()
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    if release.lifecycle_state == "deleted":
        raise IntegrityConflict("a deleted firmware release cannot be rollback-pinned")
    if payload.rollback_pinned and release.lifecycle_state != "available":
        raise IntegrityConflict("only an available firmware release can be rollback-pinned")
    if payload.rollback_pinned and not await _artifact_available(release, settings):
        raise IntegrityConflict("an unavailable firmware artifact cannot be rollback-pinned")
    if payload.rollback_pinned and not _build_identity_available(release):
        raise IntegrityConflict("firmware without a build identity cannot be rollback-pinned")
    prior = release.rollback_pinned
    release.rollback_pinned = payload.rollback_pinned
    release.updated_at = datetime.now(UTC)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_ROLLBACK_PROTECTION_UPDATED",
            target_type="firmware_release",
            target_id=release.id,
            correlation_id=request.state.correlation_id,
            details={
                "semantic_version": release.semantic_version,
                "prior_rollback_pinned": prior,
                "rollback_pinned": payload.rollback_pinned,
            },
        )
    )
    await session.commit()
    artifact_available = await _artifact_available(release, settings)
    return {
        **_release_manifest(release),
        "artifact_available": artifact_available,
        **await _release_lifecycle_view(
            session,
            release,
            artifact_available=artifact_available,
            settings=settings,
        ),
    }


@router.post("/firmware/releases/{release_id}/delete-permanently", status_code=204)
async def permanently_delete_firmware_release(
    release_id: str,
    payload: FirmwareReleaseDeleteRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    preflight_release = await session.get(FirmwareRelease, release_id)
    if preflight_release is None:
        raise NotFound("firmware release does not exist")
    if (
        payload.semantic_version != preflight_release.semantic_version
        or payload.build_number != preflight_release.build_number
        or not hmac.compare_digest(payload.sha256, preflight_release.sha256)
    ):
        raise IntegrityConflict("firmware release deletion confirmation does not match")
    preflight_deployments = list(
        (
            await session.scalars(
                select(FirmwareDeployment)
                .where(FirmwareDeployment.firmware_release_id == release_id)
                .order_by(FirmwareDeployment.id)
            )
        ).all()
    )
    locked_commands = await lock_active_ota_commands_for_deployments(session, preflight_deployments)
    await _lock_firmware_lifecycle_coordinator(session)
    # Permanent deletion compares physical artifact references in Python because
    # historical rows may contain differently spelled paths. Lock every release
    # in one deterministic order before selecting the target so two distinct
    # deletes cannot each hold its target and then wait for the other's row.
    locked_releases = list(
        (
            await session.scalars(
                select(FirmwareRelease)
                .order_by(FirmwareRelease.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    release = next((row for row in locked_releases if row.id == release_id), None)
    if release is None:
        raise NotFound("firmware release does not exist")
    if (
        payload.semantic_version != release.semantic_version
        or payload.build_number != release.build_number
        or not hmac.compare_digest(payload.sha256, release.sha256)
    ):
        raise IntegrityConflict("firmware release deletion confirmation does not match")
    batch_ids = sorted(
        {
            deployment.batch_id
            for deployment in preflight_deployments
            if deployment.batch_id is not None
        }
    )
    if batch_ids:
        locked_batch_ids = tuple(
            (
                await session.scalars(
                    select(FirmwareDeploymentBatch.id)
                    .where(FirmwareDeploymentBatch.id.in_(batch_ids))
                    .order_by(FirmwareDeploymentBatch.id)
                    .with_for_update()
                )
            ).all()
        )
        if set(locked_batch_ids) != set(batch_ids):
            raise IntegrityConflict("firmware deployment batch evidence is missing")
    deployments = list(
        (
            await session.scalars(
                select(FirmwareDeployment)
                .where(FirmwareDeployment.firmware_release_id == release.id)
                .order_by(FirmwareDeployment.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if {row.id for row in deployments} != {row.id for row in preflight_deployments}:
        raise IntegrityConflict("firmware deployment references changed; retry deletion")
    if any(row.state in ACTIVE_FIRMWARE_DEPLOYMENT_STATES for row in deployments):
        raise IntegrityConflict("active, queued, or confirming deployments protect this release")
    deployment_ids = {deployment.id for deployment in deployments}
    if any(command.payload.get("deployment_id") in deployment_ids for command in locked_commands):
        raise IntegrityConflict("an active OTA action still references this release")
    versions = {
        release.semantic_version,
        release.semantic_version.removeprefix("v"),
        f"v{release.semantic_version.removeprefix('v')}",
    }
    reported = await session.scalar(
        select(Device.id).where(Device.firmware_version.in_(versions)).with_for_update().limit(1)
    )
    if reported is not None:
        raise IntegrityConflict("a sensor currently reports this firmware release")
    if release.lifecycle_state == "current":
        raise IntegrityConflict("the current recommended firmware release cannot be deleted")
    if release.rollback_pinned:
        raise IntegrityConflict("a rollback-pinned firmware release cannot be deleted")
    if release.lifecycle_state == "deleted":
        raise IntegrityConflict("firmware release is already deleted")
    if await _shared_artifact_reference_ids(
        session,
        release,
        candidate_releases=locked_releases,
    ):
        raise IntegrityConflict("another firmware release still references this artifact")

    original_path, quarantine_path = _quarantine_artifact_file(release, settings)
    artifact_removed = quarantine_path is not None
    deleted_at = datetime.now(UTC)
    prior_state = release.lifecycle_state
    deployment_count = len(deployments)
    release.lifecycle_state = "deleted"
    release.deleted_at = deleted_at
    release.deleted_by_user_id = user.id
    release.updated_at = deleted_at
    release.release_notes = ""
    release.manifest_signature = ""
    release.candidate = False
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_RELEASE_DELETED",
            target_type="firmware_release_tombstone",
            target_id=release.id,
            correlation_id=request.state.correlation_id,
            details={
                "semantic_version": release.semantic_version,
                "build_number": release.build_number,
                "sha256": release.sha256,
                "prior_state": prior_state,
                "deployment_count": deployment_count,
                "artifact_removed": artifact_removed,
                "audit_tombstone_retained": True,
            },
        )
    )
    try:
        await session.commit()
    except BaseException:
        await session.rollback()
        try:
            _restore_quarantined_artifact(original_path, quarantine_path)
        except OSError:
            logger.exception(
                "firmware_quarantine_rollback_restore_failed",
                release_id=release_id,
                quarantine_filename=quarantine_path.name if quarantine_path is not None else None,
            )
        raise
    _purge_quarantined_artifact(quarantine_path)


@router.post("/firmware/releases", status_code=201)
async def upload_firmware_release(
    request: Request,
    image: UploadFile = File(...),
    semantic_version: str = Form(...),
    build_number: int = Form(..., ge=1, le=4_294_967_295),
    board_profile: str = Form(..., min_length=1, max_length=80),
    minimum_boot_version: int = Form(..., ge=1, le=4_294_967_295),
    minimum_config_version: int = Form(..., ge=1),
    expected_sha256: str = Form(..., pattern=r"^[0-9a-f]{64}$"),
    firmware_build_id: str | None = Form(default=None, pattern=r"^[0-9a-f]{64}$"),
    release_notes: str = Form(..., max_length=20_000),
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not SEMVER.fullmatch(semantic_version):
        raise InvalidRequest("firmware version is not valid semantic versioning")
    if image.content_type not in ("application/octet-stream", "application/x-binary"):
        raise InvalidRequest("firmware image must be an octet-stream")
    data = await image.read(MAX_FIRMWARE_BYTES + 1)
    if not data or len(data) > MAX_FIRMWARE_BYTES:
        raise InvalidRequest("firmware image is empty or exceeds the size limit")
    digest = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256):
        raise IntegrityConflict("firmware image SHA-256 does not match")
    try:
        app_identity = parse_esp32s3_app_identity(data)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    if app_identity.semantic_version != semantic_version:
        raise IntegrityConflict(
            "firmware image version does not match the submitted semantic version"
        )
    if app_identity.project_name != "power-monitor-sensor-headless":
        raise IntegrityConflict("firmware image project identity is not PowerMeter Sensor")
    if firmware_build_id is not None and not hmac.compare_digest(
        firmware_build_id, app_identity.firmware_build_id
    ):
        raise IntegrityConflict("firmware build ID does not match the uploaded image")
    if await session.scalar(
        select(FirmwareRelease.id).where(
            (FirmwareRelease.semantic_version == semantic_version)
            | (FirmwareRelease.sha256 == digest)
        )
    ):
        raise IntegrityConflict("firmware version or image already exists")
    release = FirmwareRelease(
        semantic_version=semantic_version,
        build_number=str(build_number),
        project_name="power-monitor-sensor-headless",
        target_chip="esp32s3",
        board_profile=board_profile,
        minimum_boot_version=minimum_boot_version,
        minimum_protocol=PROTOCOL_ID,
        minimum_config_version=minimum_config_version,
        image_size=len(data),
        sha256=digest,
        firmware_build_id=app_identity.firmware_build_id,
        image_path="pending",
        release_notes=release_notes,
        manifest_signature="pending",
        candidate=True,
        lifecycle_state="available",
    )
    session.add(release)
    await session.flush()
    # Hold the shared lifecycle coordinator from pending-file creation through
    # the metadata commit.  The reconciler takes the same lock before deciding
    # that a pending upload has no owner, so it cannot purge an in-flight upload.
    await _lock_firmware_lifecycle_coordinator(session)
    settings.firmware_dir.mkdir(parents=True, exist_ok=True)
    target = settings.firmware_dir / f"{release.id}.bin"
    pending_upload = settings.firmware_dir / (
        f".{release.id}.{secrets.token_hex(8)}.pending-upload"
    )
    try:
        durable_write_bytes(pending_upload, data)
        release.image_path = str(target)
        release.manifest_signature = _manifest_signature(
            settings.ota_manifest_key, _release_manifest(release)
        )
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                event_code="FIRMWARE_RELEASE_UPLOADED",
                target_type="firmware_release",
                target_id=release.id,
                correlation_id=request.state.correlation_id,
                details={
                    "sha256": digest,
                    "firmware_build_id": app_identity.firmware_build_id,
                    "candidate": True,
                    "release_state": "available",
                    "became_current": False,
                    "artifact_pending_promotion": True,
                },
            )
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        try:
            durable_unlink(pending_upload, missing_ok=True)
        except OSError:
            logger.exception(
                "firmware_pending_upload_cleanup_failed",
                release_id=release.id,
                filename=pending_upload.name,
            )
        raise
    # The metadata commit establishes ownership before the same-volume rename.
    # A crash at this boundary is repaired by the bounded worker reconciler.
    durable_replace(pending_upload, target)
    logger.info(
        "firmware_upload_completed",
        release_id=release.id,
        semantic_version=release.semantic_version,
        build_number=release.build_number,
        sha256=release.sha256,
        image_size=release.image_size,
    )
    data = b""
    return {
        "release": {
            **_release_manifest(release),
            "release_notes": release.release_notes,
            "physical_certification": "pending",
            "artifact_available": True,
            **await _release_lifecycle_view(
                session,
                release,
                artifact_available=True,
                settings=settings,
            ),
        },
        "manifest_signature": release.manifest_signature,
        "physical_certification": "pending",
    }


async def _prepare_firmware_deployment(
    release_id: str,
    payload: FirmwareDeploymentRequest,
    request: Request,
    user: CurrentUser,
    session: AsyncSession,
    settings: Settings,
) -> dict[str, object]:
    await _lock_firmware_lifecycle_coordinator(session)
    release = await session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == release_id).with_for_update()
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    if release.lifecycle_state not in {"available", "current"}:
        raise IntegrityConflict(
            "only available or current firmware releases can start new deployments"
        )
    if not await _artifact_available(release, settings):
        raise IntegrityConflict(
            "firmware artifact has been removed; upload a newer release before deploying"
        )
    if not _build_identity_available(release):
        raise IntegrityConflict(
            "firmware build identity is unavailable; upload a verified ESP32-S3 image"
        )
    homes = await _home_ids(session, user.id)
    devices = (
        await session.scalars(
            select(Device)
            .where(
                Device.id.in_(payload.device_ids),
                Device.home_id.in_(homes),
                Device.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if {row.id for row in devices} != set(payload.device_ids):
        raise NotFound("one or more target devices do not exist")
    if any(
        not _firmware_upgrade_available(device.firmware_version, release.semantic_version)
        for device in devices
    ):
        raise InvalidRequest(
            "OTA requires a firmware version newer than every target sensor's installed version"
        )
    devices_by_id = {device.id: device for device in devices}
    ordered_devices = [devices_by_id[device_id] for device_id in payload.device_ids]
    conflicting_device = await session.scalar(
        select(FirmwareDeployment.device_id)
        .where(
            FirmwareDeployment.device_id.in_(payload.device_ids),
            FirmwareDeployment.state.in_(ACTIVE_FIRMWARE_DEPLOYMENT_STATES),
        )
        .limit(1)
    )
    if conflicting_device is not None:
        raise OTAWorkflowError(
            "a target sensor already has an active OTA job; wait, cancel it if safe, "
            "or retry after it becomes terminal"
        )
    release_has_active_deployment = (
        await session.scalar(
            select(FirmwareDeployment.id)
            .where(
                FirmwareDeployment.firmware_release_id == release.id,
                FirmwareDeployment.state.in_(tuple(ACTIVE_FIRMWARE_DEPLOYMENT_STATES - {"staged"})),
            )
            .limit(1)
        )
        is not None
    )
    batch = FirmwareDeploymentBatch(
        firmware_release_id=release.id,
        rollout=payload.rollout,
        state="in_progress",
        created_by_user_id=user.id,
    )
    session.add(batch)
    await session.flush()
    deployments: list[FirmwareDeployment] = []
    for index, device in enumerate(ordered_devices):
        should_queue = payload.rollout == "immediate" or (
            index == 0 and not release_has_active_deployment
        )
        prior_attempt = int(
            await session.scalar(
                select(func.max(FirmwareDeployment.attempt)).where(
                    FirmwareDeployment.firmware_release_id == release.id,
                    FirmwareDeployment.device_id == device.id,
                )
            )
            or 0
        )
        deployment = FirmwareDeployment(
            batch_id=batch.id,
            firmware_release_id=release.id,
            device_id=device.id,
            state="queued" if should_queue else "staged",
            progress_percent=0,
            attempt=prior_attempt + 1,
            evidence={
                "issued_by_user_id": user.id,
                "previous_firmware_version": device.firmware_version,
                "expected_firmware_build_id": release.firmware_build_id,
            },
        )
        session.add(deployment)
        await session.flush()
        manifest = await _ota_command_manifest(
            session=session,
            settings=settings,
            release=release,
            deployment=deployment,
            device=device,
        )
        deployment.evidence = {
            "manifest": manifest,
            "issued_by_user_id": user.id,
            "previous_firmware_version": device.firmware_version,
            "expected_firmware_build_id": release.firmware_build_id,
        }
        if deployment.state == "queued":
            await create_command(
                session,
                device_id=device.id,
                command_type="ota_install",
                issued_by_user_id=user.id,
                idempotency_key=f"ota:{deployment.id}",
                payload=manifest,
            )
        deployments.append(deployment)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_CREATED",
            target_type="firmware_release",
            target_id=release.id,
            correlation_id=request.state.correlation_id,
            details={"device_count": len(devices), "rollout": payload.rollout},
        )
    )
    return {
        "batch_id": batch.id,
        "batch_state": batch.state,
        "deployments": [
            {"id": row.id, "device_id": row.device_id, "state": row.state} for row in deployments
        ],
    }


@router.post("/firmware/releases/{release_id}/deploy", status_code=202)
async def deploy_firmware_release(
    release_id: str,
    payload: FirmwareDeploymentRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    response = await _prepare_firmware_deployment(
        release_id, payload, request, user, session, settings
    )
    await session.commit()
    logger.info(
        "ota_deployment_batch_created",
        release_id=release_id,
        batch_id=response["batch_id"],
        deployments=response["deployments"],
    )
    return response


@router.post("/firmware/deployment-batches/{batch_id}/retry", status_code=202)
async def retry_firmware_deployment_batch(
    batch_id: str,
    payload: FirmwareDeploymentRetryRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    preflight_batch, preflight_release, preflight_rows = await _batch_for_user(
        session, batch_id=batch_id, user_id=user.id, lock=False
    )
    if preflight_batch.deleted_at is not None:
        raise IntegrityConflict("a deleted firmware deployment tombstone cannot be retried")
    if preflight_batch.archived_at is not None:
        raise IntegrityConflict("restore the archived firmware deployment before retrying it")
    selected_preflight_rows = [
        row for row in preflight_rows if row[0].device_id in set(payload.device_ids)
    ]
    if {deployment.device_id for deployment, _device in selected_preflight_rows} != set(
        payload.device_ids
    ):
        raise NotFound("one or more retry targets do not belong to this deployment")

    # Command rows are always locked before their linked deployment rows.  This
    # matches command delivery/result processing and prevents retry/delete races.
    locked_commands = await lock_active_ota_commands_for_deployments(
        session, [deployment for deployment, _device in preflight_rows]
    )
    await _lock_firmware_lifecycle_coordinator(session)
    release = await session.scalar(
        select(FirmwareRelease)
        .where(FirmwareRelease.id == preflight_release.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    batch, _batch_release, rows = await _locked_batch_for_user(
        session,
        batch_id=batch_id,
        user_id=user.id,
        commands_locked=True,
    )
    if {deployment.id for deployment, _device in rows} != {
        deployment.id for deployment, _device in preflight_rows
    }:
        raise IntegrityConflict("firmware deployment references changed; retry the request")
    if batch.deleted_at is not None:
        raise IntegrityConflict("a deleted firmware deployment tombstone cannot be retried")
    if batch.archived_at is not None:
        raise IntegrityConflict("restore the archived firmware deployment before retrying it")
    deployment_ids = {deployment.id for deployment, _device in rows}
    if any(command.payload.get("deployment_id") in deployment_ids for command in locked_commands):
        raise OTAWorkflowError("the prior OTA action is still active and cannot be retried")
    selected_rows = [row for row in rows if row[0].device_id in set(payload.device_ids)]
    for deployment, device in selected_rows:
        if deployment.state not in {"failed", "rolled_back", "timed_out", "cancelled"}:
            raise OTAWorkflowError("only terminal failed or outdated sensor jobs can be retried")
        if not _firmware_upgrade_available(device.firmware_version, release.semantic_version):
            raise OTAWorkflowError("a selected sensor already reports the target version or newer")
    response = await _prepare_firmware_deployment(
        release.id,
        FirmwareDeploymentRequest(device_ids=payload.device_ids, rollout="immediate"),
        request,
        user,
        session,
        settings,
    )
    retry_batch = await session.get(FirmwareDeploymentBatch, str(response["batch_id"]))
    if retry_batch is None:
        raise RuntimeError("retry batch was not prepared")
    retry_batch.rollout = "retry"
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_RETRIED",
            target_type="firmware_deployment_batch",
            target_id=retry_batch.id,
            correlation_id=request.state.correlation_id,
            details={"prior_batch_id": batch.id, "device_count": len(payload.device_ids)},
        )
    )
    await session.commit()
    logger.info(
        "ota_deployment_retried",
        prior_batch_id=batch.id,
        batch_id=retry_batch.id,
        device_ids=payload.device_ids,
        expected_version=release.semantic_version,
    )
    response["batch_state"] = retry_batch.state
    return response


@router.post("/firmware/deployment-batches/{batch_id}/cancel")
async def cancel_firmware_deployment_batch(
    batch_id: str,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    preflight_batch, preflight_release, preflight_rows = await _batch_for_user(
        session, batch_id=batch_id, user_id=user.id, lock=False
    )
    if preflight_batch.deleted_at is not None or preflight_batch.archived_at is not None:
        raise IntegrityConflict("an archived or deleted deployment cannot be cancelled")
    locked_commands = await lock_active_ota_commands_for_deployments(
        session, [deployment for deployment, _device in preflight_rows]
    )
    # Staged promotion has no command row yet and serializes on the release.
    # Take that same lock before the batch/deployment locks so cancellation
    # cannot deadlock or cancel across a concurrent stage promotion.
    locked_release = await session.scalar(
        select(FirmwareRelease)
        .where(FirmwareRelease.id == preflight_release.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_release is None:
        raise IntegrityConflict("firmware deployment release evidence is missing")
    batch, _release, rows = await _locked_batch_for_user(
        session,
        batch_id=batch_id,
        user_id=user.id,
        commands_locked=True,
    )
    if {deployment.id for deployment, _device in rows} != {
        deployment.id for deployment, _device in preflight_rows
    }:
        raise IntegrityConflict("firmware deployment references changed; retry cancellation")
    if batch.deleted_at is not None or batch.archived_at is not None:
        raise IntegrityConflict("an archived or deleted deployment cannot be cancelled")
    if not any(deployment.state in {"staged", "queued"} for deployment, _device in rows):
        raise OTAWorkflowError("this deployment has no waiting jobs that can be cancelled")
    unsafe = [
        deployment
        for deployment, _device in rows
        if deployment.state in {"downloading", "rebooting", "validating"}
    ]
    commands_by_deployment_id = {
        command.payload.get("deployment_id"): command for command in locked_commands
    }
    queued_commands: dict[str, DeviceCommand] = {}
    for deployment, _device in rows:
        if deployment.state != "queued":
            continue
        command = commands_by_deployment_id.get(deployment.id)
        if command is None or command.state != "queued":
            unsafe.append(deployment)
        else:
            queued_commands[deployment.id] = command
    if unsafe:
        raise OTAWorkflowError(
            "cancellation cannot reverse an OTA already delivered, downloading, or "
            "confirming; wait for its terminal result"
        )
    cancelled_at = datetime.now(UTC)
    for deployment, _device in rows:
        if deployment.state not in {"staged", "queued"}:
            continue
        deployment.state = "cancelled"
        deployment.completed_at = cancelled_at
        deployment.updated_at = cancelled_at
        deployment.error_code = "OTA_CANCELLED_BY_ADMINISTRATOR"
        deployment.error_message = "The update was cancelled before delivery"
        command = queued_commands.get(deployment.id)
        if command is not None:
            command.state = "cancelled"
            command.last_result = {
                "result_code": "OTA_CANCELLED_BY_ADMINISTRATOR",
                "evidence": {},
            }
    await recalculate_firmware_batch(session, batch.id, now=cancelled_at)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="FIRMWARE_DEPLOYMENT_CANCELLED",
            target_type="firmware_deployment_batch",
            target_id=batch.id,
            correlation_id=request.state.correlation_id,
            details={"cancelled_before_delivery": True},
        )
    )
    await session.commit()
    logger.info(
        "ota_deployment_cancelled",
        batch_id=batch.id,
        device_ids=[deployment.device_id for deployment, _device in rows],
        error_code="OTA_CANCELLED_BY_ADMINISTRATOR",
    )
    return {"batch_id": batch.id, "state": batch.state}


@router.delete("/firmware/releases/{release_id}", status_code=204)
async def delete_firmware_artifact(
    release_id: str,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> None:
    del release_id, request, user, session, settings
    raise InvalidRequest(
        "legacy unconfirmed artifact deletion is retired; use the confirmed "
        "delete-permanently release workflow"
    )


@router.get("/device/firmware/{release_id}")
async def download_firmware(
    release_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    authenticated = await authenticate_device_request(request, session, settings, b"")
    device = authenticated.device
    secret = authenticated.secret
    preflight_deployment = await session.scalar(
        select(FirmwareDeployment).where(
            FirmwareDeployment.firmware_release_id == release_id,
            FirmwareDeployment.device_id == device.id,
            FirmwareDeployment.state.in_(("queued", "downloading")),
        )
    )
    if preflight_deployment is None:
        raise NotFound("firmware deployment does not exist")
    graph = await lock_firmware_ota_graph(
        session,
        (preflight_deployment,),
        lock_commands=True,
    )
    deployment = next(
        (
            row
            for row in graph.deployments
            if row.id == preflight_deployment.id and row.state in {"queued", "downloading"}
        ),
        None,
    )
    if deployment is None:
        raise NotFound("firmware deployment does not exist")
    if not any(command.payload.get("deployment_id") == deployment.id for command in graph.commands):
        raise NotFound("active firmware download authorization does not exist")
    release = next((row for row in graph.releases if row.id == release_id), None)
    if release is None:
        raise NotFound("firmware release does not exist")
    if request.headers.get("range") is not None:
        raise InvalidRequest("partial OTA downloads are not supported; retry from byte zero")
    stored_path = _canonical_artifact_path(release, settings)
    if stored_path is None:
        raise IntegrityConflict("firmware artifact integrity verification failed")
    path = Path(stored_path)
    content = await path.read_bytes()
    if len(content) != release.image_size or hashlib.sha256(content).hexdigest() != release.sha256:
        raise IntegrityConflict("firmware artifact integrity verification failed")
    timestamp = str(int(datetime.now(UTC).timestamp()))
    nonce = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    digest = body_sha256(content)
    canonical = canonical_request(
        "RESPONSE", request.url.path, request.url.query, timestamp, nonce, digest
    )
    signature = sign_request(
        derive_directional_key(secret, device.id, "server-to-device"), canonical
    )
    deployment.state = "downloading"
    deployment.progress_percent = max(deployment.progress_percent, 1)
    deployment.updated_at = datetime.now(UTC)
    await session.commit()
    return Response(
        content=content,
        status_code=200,
        media_type="application/octet-stream",
        headers={
            "X-PM-Protocol": PROTOCOL_ID,
            "X-PM-Device-ID": device.id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Nonce": nonce,
            "X-PM-Content-SHA256": digest,
            "X-PM-Signature": signature,
            "ETag": f'"{release.sha256}"',
            "Cache-Control": "private, no-store",
        },
    )
