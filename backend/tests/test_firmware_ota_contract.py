from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import timedelta

import pytest
from backend.app.main import session_factory
from backend.app.models import DeviceCommand, Home
from backend.app.routes.firmware import OTA_MANIFEST_FIELDS, ota_manifest_canonical
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)
from httpx import AsyncClient
from sqlalchemy import select


def _device_headers(
    *, device_id: str, secret: bytes, method: str, path: str, body: bytes = b""
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    digest = body_sha256(body)
    canonical = canonical_request(method, path, "", timestamp, nonce, digest)
    return {
        "X-PM-Protocol": "pm-protocol/1.0.0",
        "X-PM-Device-ID": device_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Nonce": nonce,
        "X-PM-Content-SHA256": digest,
        "X-PM-Signature": sign_request(
            derive_directional_key(secret, device_id, "device-to-server"), canonical
        ),
    }


@pytest.mark.asyncio
async def test_ota_command_and_download_use_one_locked_per_device_contract(
    owner_client: AsyncClient,
) -> None:
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
    assert home_id is not None
    token = await owner_client.post(
        "/api/v1/enrollment-tokens",
        json={
            "home_id": home_id,
            "friendly_name": "OTA target",
            "ct_rating_a": "100",
            "pzem_variant": "pzem004t-v4-classic-candidate",
            "expires_minutes": 15,
        },
    )
    assert token.status_code == 201, token.text
    enrolled = await owner_client.post(
        "/api/v1/devices/enroll",
        json={
            "enrollment_token": token.json()["token"],
            "protocol_id": "pm-protocol/1.0.0",
            "firmware_version": "0.1.0-rc.1",
            "hardware_fingerprint": "ota-contract-target",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    device_id = enrolled.json()["device_id"]
    device_secret = base64.b64decode(enrolled.json()["device_secret"])

    image = b"PowerMeter V2 OTA contract fixture\0" * 64
    image_sha256 = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.1-rc.1",
            "build_number": "101",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": image_sha256,
            "release_notes": "Exact OTA command contract fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release = uploaded.json()["release"]
    assert release["build_number"] == 101
    assert release["minimum_boot_version"] == 1

    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release['release_id']}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert deployed.status_code == 202, deployed.text
    async with session_factory() as session:
        command = await session.scalar(
            select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
        )
    assert command is not None
    assert command.required_firmware_capability == "ota_v1"
    assert command.expires_at - command.issued_at == timedelta(hours=24)
    manifest = command.payload
    assert set(manifest) == set(OTA_MANIFEST_FIELDS) | {"signature"}
    assert "manifest" not in manifest
    assert manifest["device_id"] == device_id
    assert manifest["deployment_id"] == deployed.json()["deployments"][0]["id"]
    assert manifest["release_id"] == release["release_id"]
    assert manifest["build_number"] == 101
    assert manifest["download_path"] == f"/api/v1/device/firmware/{release['release_id']}"
    assert len(manifest["manifest_nonce"]) == 32
    unsigned = {key: value for key, value in manifest.items() if key != "signature"}
    server_key = derive_directional_key(device_secret, device_id, "server-to-device")
    assert verify_signature(server_key, ota_manifest_canonical(unsigned), manifest["signature"])
    assert len(base64.b64decode(manifest["signature"], validate=True)) == 32

    path = manifest["download_path"]
    downloaded = await owner_client.get(
        path,
        headers=_device_headers(
            device_id=device_id,
            secret=device_secret,
            method="GET",
            path=path,
        ),
    )
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == image
    assert downloaded.headers["content-length"] == str(len(image))
    assert downloaded.headers["etag"] == f'"{image_sha256}"'
    assert "accept-ranges" not in downloaded.headers
    assert downloaded.headers["X-PM-Content-SHA256"] == image_sha256
    response_canonical = canonical_request(
        "RESPONSE",
        path,
        "",
        downloaded.headers["X-PM-Timestamp"],
        downloaded.headers["X-PM-Nonce"],
        downloaded.headers["X-PM-Content-SHA256"],
    )
    assert verify_signature(
        server_key,
        response_canonical,
        downloaded.headers["X-PM-Signature"],
    )

    range_headers = _device_headers(
        device_id=device_id,
        secret=device_secret,
        method="GET",
        path=path,
    )
    range_headers["Range"] = "bytes=1-"
    partial = await owner_client.get(path, headers=range_headers)
    assert partial.status_code == 422
    assert partial.json()["code"] == "INVALID_REQUEST"
