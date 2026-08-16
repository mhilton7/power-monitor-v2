from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import UTC, datetime
from typing import Any

from anyio import Path
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..constants import MAX_FIRMWARE_BYTES, PROTOCOL_ID
from ..db import get_session
from ..errors import IntegrityConflict, InvalidRequest, NotFound
from ..models import (
    AuditEvent,
    Device,
    DeviceCredential,
    FirmwareDeployment,
    FirmwareRelease,
    user_home_scopes,
)
from ..schemas.api import FirmwareDeploymentRequest
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

router = APIRouter(prefix="/api/v1", tags=["firmware"])
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


@router.get("/firmware/releases")
async def list_firmware_releases(
    _user: CurrentUser = Depends(require_permission("firmware.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    rows = (
        await session.scalars(select(FirmwareRelease).order_by(FirmwareRelease.created_at.desc()))
    ).all()
    return {
        "releases": [
            {
                **_release_manifest(row),
                "release_notes": row.release_notes,
                "physical_certification": "pending" if row.candidate else "required",
            }
            for row in rows
        ]
    }


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
    data = b""
    return {
        "release": _release_manifest(release),
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
    release = await session.get(FirmwareRelease, release_id)
    if release is None:
        raise NotFound("firmware release does not exist")
    homes = await _home_ids(session, user.id)
    devices = (
        await session.scalars(
            select(Device).where(
                Device.id.in_(payload.device_ids),
                Device.home_id.in_(homes),
                Device.revoked_at.is_(None),
            )
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
    deployments: list[FirmwareDeployment] = []
    for index, device in enumerate(devices):
        existing = await session.scalar(
            select(FirmwareDeployment).where(
                FirmwareDeployment.firmware_release_id == release.id,
                FirmwareDeployment.device_id == device.id,
                FirmwareDeployment.state.not_in(("failed", "rolled_back", "cancelled")),
            )
        )
        if existing is not None:
            deployments.append(existing)
            continue
        deployment = FirmwareDeployment(
            firmware_release_id=release.id,
            device_id=device.id,
            state="queued" if payload.rollout == "immediate" or index == 0 else "staged",
            progress_percent=0,
            evidence={"issued_by_user_id": user.id},
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
    return {
        "deployments": [
            {"id": row.id, "device_id": row.device_id, "state": row.state} for row in deployments
        ]
    }


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
