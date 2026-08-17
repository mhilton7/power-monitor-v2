from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path as LocalPath
from typing import Any

import structlog
from anyio import Path
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy import func, select
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
    FirmwareRelease,
    user_home_scopes,
)
from ..schemas.api import FirmwareDeploymentRequest, FirmwareDeploymentRetryRequest
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
    recalculate_firmware_batch,
    reconcile_stale_firmware_deployments,
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


async def _artifact_available(release: FirmwareRelease) -> bool:
    return bool(release.image_path) and await Path(release.image_path).is_file()


def _deployment_view(
    deployment: FirmwareDeployment, release: FirmwareRelease, device: Device
) -> dict[str, object]:
    return {
        "id": deployment.id,
        "device_id": deployment.device_id,
        "device_name": device.friendly_name,
        "previous_version": deployment.evidence.get("previous_firmware_version"),
        "current_version": device.firmware_version,
        "target_version": release.semantic_version,
        "target_build": int(release.build_number),
        "state": deployment.state,
        "progress_percent": deployment.progress_percent,
        "attempt": deployment.attempt,
        "error_code": deployment.error_code,
        "error_message": deployment.error_message,
        "created_at": deployment.created_at,
        "updated_at": deployment.updated_at,
        "completed_at": deployment.completed_at,
        "confirmation_heartbeat_at": deployment.evidence.get("post_reboot_confirmed_at"),
        "reported_firmware_after_reboot": deployment.evidence.get("post_reboot_firmware_version"),
        "retry_eligible": deployment.state in {"failed", "rolled_back", "timed_out", "cancelled"}
        and _firmware_upgrade_available(device.firmware_version, release.semantic_version),
        "cancel_eligible": deployment.state in {"staged", "queued"},
    }


def _batch_view(
    batch: FirmwareDeploymentBatch,
    release: FirmwareRelease,
    rows: list[tuple[FirmwareDeployment, Device]],
) -> dict[str, object]:
    succeeded = sum(deployment.state == "succeeded" for deployment, _device in rows)
    failed = sum(
        deployment.state in {"failed", "rolled_back", "timed_out", "cancelled"}
        for deployment, _device in rows
    )
    pending = len(rows) - succeeded - failed
    return {
        "id": batch.id,
        "release_id": batch.firmware_release_id,
        "target_version": release.semantic_version,
        "rollout": batch.rollout,
        "state": batch.state,
        "targeted": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "completed_at": batch.completed_at,
        "jobs": [_deployment_view(deployment, release, device) for deployment, device in rows],
    }


@router.get("/firmware/releases")
async def list_firmware_releases(
    user: CurrentUser = Depends(require_permission("firmware.view")),
    session: AsyncSession = Depends(get_session),
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
                batch_views.append(_batch_view(batch, row, deployment_rows))
        releases.append(
            {
                **_release_manifest(row),
                "release_notes": row.release_notes,
                "physical_certification": "pending" if row.candidate else "required",
                "artifact_available": await _artifact_available(row),
                "upload_status": "uploaded" if row.image_path else "archived",
                "validation_status": "ready" if row.image_path else "archived",
                "deployment_batches": batch_views,
            }
        )
    return {"releases": releases}


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
        image_path="pending",
        release_notes=release_notes,
        manifest_signature="pending",
        candidate=True,
    )
    session.add(release)
    await session.flush()
    settings.firmware_dir.mkdir(parents=True, exist_ok=True)
    target = settings.firmware_dir / f"{release.id}.bin"
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
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
            details={"sha256": digest, "candidate": True},
        )
    )
    await session.commit()
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
        },
        "manifest_signature": release.manifest_signature,
        "physical_certification": "pending",
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
    release = await session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == release_id).with_for_update()
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    if not await _artifact_available(release):
        raise IntegrityConflict(
            "firmware artifact has been removed; upload a newer release before deploying"
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
    await session.commit()
    for deployment in deployments:
        logger.info(
            "ota_sensor_job_created",
            release_id=release.id,
            batch_id=batch.id,
            deployment_id=deployment.id,
            device_id=deployment.device_id,
            expected_version=release.semantic_version,
            state=deployment.state,
        )
    return {
        "batch_id": batch.id,
        "batch_state": batch.state,
        "deployments": [
            {"id": row.id, "device_id": row.device_id, "state": row.state} for row in deployments
        ],
    }


@router.post("/firmware/deployment-batches/{batch_id}/retry", status_code=202)
async def retry_firmware_deployment_batch(
    batch_id: str,
    payload: FirmwareDeploymentRetryRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("firmware.manage")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    batch = await session.get(FirmwareDeploymentBatch, batch_id)
    if batch is None:
        raise NotFound("firmware deployment batch does not exist")
    homes = await _home_ids(session, user.id)
    rows = list(
        (
            await session.execute(
                select(FirmwareDeployment, Device)
                .join(Device, Device.id == FirmwareDeployment.device_id)
                .where(
                    FirmwareDeployment.batch_id == batch.id,
                    FirmwareDeployment.device_id.in_(payload.device_ids),
                    Device.home_id.in_(homes),
                )
            )
        ).all()
    )
    if {deployment.device_id for deployment, _device in rows} != set(payload.device_ids):
        raise NotFound("one or more retry targets do not belong to this deployment")
    release = await session.get(FirmwareRelease, batch.firmware_release_id)
    if release is None:
        raise NotFound("firmware release does not exist")
    for deployment, device in rows:
        if deployment.state not in {"failed", "rolled_back", "timed_out", "cancelled"}:
            raise OTAWorkflowError("only terminal failed or outdated sensor jobs can be retried")
        if not _firmware_upgrade_available(device.firmware_version, release.semantic_version):
            raise OTAWorkflowError("a selected sensor already reports the target version or newer")
    response = await deploy_firmware_release(
        release.id,
        FirmwareDeploymentRequest(device_ids=payload.device_ids, rollout="immediate"),
        request,
        user,
        session,
        settings,
    )
    retry_batch = await session.get(FirmwareDeploymentBatch, str(response["batch_id"]))
    if retry_batch is None:
        raise RuntimeError("retry batch was not persisted")
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
    batch = await session.scalar(
        select(FirmwareDeploymentBatch)
        .where(FirmwareDeploymentBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        raise NotFound("firmware deployment batch does not exist")
    homes = await _home_ids(session, user.id)
    rows = list(
        (
            await session.execute(
                select(FirmwareDeployment, Device)
                .join(Device, Device.id == FirmwareDeployment.device_id)
                .where(FirmwareDeployment.batch_id == batch.id, Device.home_id.in_(homes))
                .with_for_update()
            )
        ).all()
    )
    if not rows:
        raise NotFound("firmware deployment batch does not exist")
    if not any(deployment.state in {"staged", "queued"} for deployment, _device in rows):
        raise OTAWorkflowError("this deployment has no waiting jobs that can be cancelled")
    unsafe = [
        deployment
        for deployment, _device in rows
        if deployment.state in {"downloading", "rebooting", "validating"}
    ]
    queued_commands: dict[str, DeviceCommand] = {}
    for deployment, _device in rows:
        if deployment.state != "queued":
            continue
        commands = list(
            (
                await session.scalars(
                    select(DeviceCommand).where(
                        DeviceCommand.device_id == deployment.device_id,
                        DeviceCommand.command_type == "ota_install",
                        DeviceCommand.state.in_(("queued", "delivered")),
                    )
                )
            ).all()
        )
        command = next(
            (
                candidate
                for candidate in commands
                if candidate.payload.get("deployment_id") == deployment.id
            ),
            None,
        )
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
    release = await session.scalar(
        select(FirmwareRelease).where(FirmwareRelease.id == release_id).with_for_update()
    )
    if release is None:
        raise NotFound("firmware release does not exist")
    active_count = int(
        await session.scalar(
            select(func.count(FirmwareDeployment.id)).where(
                FirmwareDeployment.firmware_release_id == release.id,
                FirmwareDeployment.state.in_(ACTIVE_FIRMWARE_DEPLOYMENT_STATES),
            )
        )
        or 0
    )
    if active_count:
        raise IntegrityConflict(
            "firmware artifact cannot be removed while deployments are active or staged"
        )
    if release.image_path:
        firmware_root = LocalPath(settings.firmware_dir).resolve()
        configured_path = LocalPath(release.image_path)
        if configured_path.is_symlink():
            raise IntegrityConflict("firmware artifact path is outside the configured store")
        stored_path = configured_path.resolve()
        if stored_path.parent != firmware_root:
            raise IntegrityConflict("firmware artifact path is outside the configured store")
        if stored_path.is_file():
            stored_path.unlink()
        release.image_path = ""
        session.add(
            AuditEvent(
                actor_user_id=user.id,
                event_code="FIRMWARE_ARTIFACT_DELETED",
                target_type="firmware_release",
                target_id=release.id,
                correlation_id=request.state.correlation_id,
                details={
                    "semantic_version": release.semantic_version,
                    "sha256": release.sha256,
                    "metadata_retained": True,
                },
            )
        )
        await session.commit()


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
    deployment = await session.scalar(
        select(FirmwareDeployment).where(
            FirmwareDeployment.firmware_release_id == release_id,
            FirmwareDeployment.device_id == device.id,
            FirmwareDeployment.state.in_(("queued", "downloading", "validating")),
        )
    )
    if deployment is None:
        raise NotFound("firmware deployment does not exist")
    release = await session.get(FirmwareRelease, release_id)
    if release is None:
        raise NotFound("firmware release does not exist")
    if request.headers.get("range") is not None:
        raise InvalidRequest("partial OTA downloads are not supported; retry from byte zero")
    path = Path(release.image_path)
    if not await path.is_file():
        raise IntegrityConflict("firmware artifact integrity verification failed")
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
