from __future__ import annotations

import base64
import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from backend.app.constants import MAX_COMMAND_DELIVERY_ATTEMPT, MAX_DEVICE_RESPONSE_BYTES
from backend.app.main import session_factory
from backend.app.models import DeviceCommand, FirmwareDeployment, FirmwareRelease, Home
from backend.app.routes.firmware import OTA_MANIFEST_FIELDS, ota_manifest_canonical
from backend.app.schemas.device import CommandEnvelope, DeviceResponse
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)
from httpx import AsyncClient, Response
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


def _heartbeat_body(
    *, firmware_version: str = "0.1.0-rc.7", command_results: list[dict[str, object]] | None = None
) -> bytes:
    return orjson.dumps(
        {
            "protocol_id": "pm-protocol/1.0.0",
            "boot_id": "123e4567-e89b-12d3-a456-426614174000",
            "firmware_version": firmware_version,
            "measurement": {
                "measured_at": None,
                "monotonic_us": 1,
                "voltage_v": None,
                "current_a": None,
                "active_power_w": None,
                "frequency_hz": None,
                "power_factor": None,
                "pzem_energy_wh": None,
                "pzem_status": "absent",
                "pzem_error_code": "TEST_NO_METER",
            },
            "storage_status": "ok",
            "time_status": "trusted",
            "wifi_rssi": -50,
            "ip_address": "192.0.2.30",
            "backlog": 0,
            "oldest_sequence": None,
            "newest_sequence": None,
            "acknowledged_sequence": 0,
            "free_internal_heap": 200_000,
            "largest_internal_block": 120_000,
            "task_stack_watermarks": {},
            "reboot_reason": None,
            "health_flags": [],
            "command_results": command_results or [],
        }
    )


async def _heartbeat(
    client: AsyncClient,
    *,
    device_id: str,
    secret: bytes,
    firmware_version: str = "0.1.0-rc.7",
    command_results: list[dict[str, object]] | None = None,
) -> Response:
    path = "/api/v1/device/heartbeat"
    body = _heartbeat_body(firmware_version=firmware_version, command_results=command_results)
    return await client.post(
        path,
        content=body,
        headers={
            **_device_headers(
                device_id=device_id,
                secret=secret,
                method="POST",
                path=path,
                body=body,
            ),
            "Content-Type": "application/json",
        },
    )


async def _enroll_ota_target(
    owner_client: AsyncClient, *, name: str = "OTA target"
) -> tuple[str, bytes, str]:
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id).where(Home.name == "Test Home"))
    assert home_id is not None
    token = await owner_client.post(
        "/api/v1/enrollment-tokens",
        json={
            "home_id": home_id,
            "friendly_name": name,
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
            "firmware_version": "0.1.0-rc.7",
            "hardware_fingerprint": f"ota-fixture-{name}",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return (
        enrolled.json()["device_id"],
        base64.b64decode(enrolled.json()["device_secret"]),
        home_id,
    )


async def _upload_and_deploy(
    owner_client: AsyncClient,
    *,
    device_id: str,
    sequence: int,
    maximize_manifest: bool = False,
) -> tuple[str, str]:
    prerelease = "x" * 28 if maximize_manifest else "rc.7"
    semantic_version = f"1.{sequence}.0-{prerelease}"
    image = (f"PowerMeter OTA fixture {semantic_version}\0".encode()) * 64
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": semantic_version,
            "build_number": str(sequence + 1),
            "board_profile": "b" * 80 if maximize_manifest else "esp32-s3-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "OTA delivery hardening fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert deployed.status_code == 202, deployed.text
    return release_id, deployed.json()["deployments"][0]["id"]


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
    assert release["artifact_available"] is True
    assert release["release_notes"] == "Exact OTA command contract fixture."

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


@pytest.mark.asyncio
async def test_ota_rejects_same_version_before_creating_a_device_command(
    owner_client: AsyncClient,
) -> None:
    device_id, _device_secret, _home_id = await _enroll_ota_target(owner_client)
    image = b"PowerMeter same-version OTA fixture\0" * 64
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.7",
            "build_number": "7",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Same-version rejection fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text

    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{uploaded.json()['release']['release_id']}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )

    assert deployed.status_code == 422, deployed.text
    assert deployed.json()["code"] == "INVALID_REQUEST"
    assert "newer" in deployed.json()["detail"]
    async with session_factory() as session:
        command = await session.scalar(
            select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
        )
    assert command is None


@pytest.mark.asyncio
async def test_ota_delivery_attempt_never_exceeds_firmware_uint8(
    owner_client: AsyncClient,
) -> None:
    device_id, device_secret, _home_id = await _enroll_ota_target(owner_client)
    _release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=20
    )
    async with session_factory() as session:
        command = await session.scalar(
            select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
        )
        assert command is not None
        command.state = "delivered"
        command.attempt = MAX_COMMAND_DELIVERY_ATTEMPT
        await session.commit()
        command_id = command.id

    heartbeat = await _heartbeat(owner_client, device_id=device_id, secret=device_secret)
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == []

    async with session_factory() as session:
        command = await session.get(DeviceCommand, command_id)
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert command is not None
        assert deployment is not None
        assert command.attempt == MAX_COMMAND_DELIVERY_ATTEMPT
        assert command.state == "failed"
        assert command.last_result == {
            "result_code": "DELIVERY_ATTEMPTS_EXHAUSTED",
            "evidence": {"delivery_attempt": MAX_COMMAND_DELIVERY_ATTEMPT},
        }
        assert deployment.state == "failed"
        assert deployment.completed_at is not None
        assert deployment.evidence["server_result_code"] == "DELIVERY_ATTEMPTS_EXHAUSTED"
        assert deployment.evidence["command_id"] == command_id


@pytest.mark.asyncio
async def test_single_ota_envelope_larger_than_absolute_response_budget_is_terminal(
    owner_client: AsyncClient,
) -> None:
    device_id, device_secret, _home_id = await _enroll_ota_target(owner_client)
    _release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=22
    )
    async with session_factory() as session:
        command = await session.scalar(
            select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
        )
        assert command is not None
        command.payload = {
            **command.payload,
            "oversized_contract_fixture": "x" * MAX_DEVICE_RESPONSE_BYTES,
        }
        envelope = CommandEnvelope(
            command_id=command.id,
            command_type=command.command_type,
            not_before=command.not_before,
            expires_at=command.expires_at,
            attempt=1,
            idempotency_key=command.idempotency_key,
            required_firmware_capability=command.required_firmware_capability,
            payload=command.payload,
        )
        expected_serialized_bytes = len(
            orjson.dumps(envelope.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
        )
        await session.commit()
        command_id = command.id

    heartbeat = await _heartbeat(owner_client, device_id=device_id, secret=device_secret)
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == []
    expected_maximum_bytes = MAX_DEVICE_RESPONSE_BYTES - len(heartbeat.content)
    assert expected_serialized_bytes > expected_maximum_bytes
    size_evidence = {
        "serialized_envelope_bytes": expected_serialized_bytes,
        "maximum_envelope_bytes": expected_maximum_bytes,
    }

    async with session_factory() as session:
        command = await session.get(DeviceCommand, command_id)
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert command is not None
        assert deployment is not None
        assert command.state == "failed"
        assert command.attempt == 0
        assert command.last_result == {
            "result_code": "DELIVERY_RESPONSE_TOO_LARGE",
            "evidence": size_evidence,
        }
        assert deployment.state == "failed"
        assert deployment.completed_at is not None
        assert deployment.evidence["server_result_code"] == "DELIVERY_RESPONSE_TOO_LARGE"
        assert deployment.evidence["command_id"] == command_id
        assert deployment.evidence["command_state"] == "failed"
        assert deployment.evidence["delivery_attempt"] == 0
        assert deployment.evidence["serialized_envelope_bytes"] == expected_serialized_bytes
        assert deployment.evidence["maximum_envelope_bytes"] == expected_maximum_bytes


@pytest.mark.asyncio
async def test_expired_ota_command_terminalizes_linked_deployment(
    owner_client: AsyncClient,
) -> None:
    device_id, device_secret, _home_id = await _enroll_ota_target(owner_client)
    _release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=21
    )
    async with session_factory() as session:
        command = await session.scalar(
            select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
        )
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert command is not None
        assert deployment is not None
        command.state = "delivered"
        command.attempt = 1
        command.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        deployment.state = "downloading"
        await session.commit()
        command_id = command.id

    heartbeat = await _heartbeat(owner_client, device_id=device_id, secret=device_secret)
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == []

    async with session_factory() as session:
        command = await session.get(DeviceCommand, command_id)
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert command is not None
        assert deployment is not None
        assert command.state == "expired"
        assert command.last_result == {
            "result_code": "COMMAND_EXPIRED",
            "evidence": {"delivery_attempt": 1},
        }
        assert deployment.state == "failed"
        assert deployment.completed_at is not None
        assert deployment.evidence["server_result_code"] == "COMMAND_EXPIRED"
        assert deployment.evidence["command_id"] == command_id


@pytest.mark.asyncio
async def test_heartbeat_defers_ota_commands_to_fit_firmware_response_buffer(
    owner_client: AsyncClient,
) -> None:
    device_id, device_secret, _home_id = await _enroll_ota_target(owner_client)
    deployment_ids: list[str] = []
    for sequence in range(30, 34):
        _release_id, deployment_id = await _upload_and_deploy(
            owner_client,
            device_id=device_id,
            sequence=sequence,
            maximize_manifest=True,
        )
        deployment_ids.append(deployment_id)

    heartbeat = await _heartbeat(owner_client, device_id=device_id, secret=device_secret)
    assert heartbeat.status_code == 200, heartbeat.text
    assert len(heartbeat.content) <= MAX_DEVICE_RESPONSE_BYTES
    assert int(heartbeat.headers["content-length"]) == len(heartbeat.content)
    delivered = heartbeat.json()["commands"]
    assert 0 < len(delivered) < len(deployment_ids)
    assert all(command["attempt"] <= MAX_COMMAND_DELIVERY_ATTEMPT for command in delivered)
    response_model = DeviceResponse.model_validate(heartbeat.json())
    serialized_response = orjson.dumps(
        response_model.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS
    )
    assert serialized_response == heartbeat.content
    empty_response = response_model.model_copy(update={"commands": []})
    serialized_empty_response = orjson.dumps(
        empty_response.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS
    )
    exact_envelope_bytes = sum(
        len(orjson.dumps(command.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS))
        for command in response_model.commands
    )
    exact_comma_bytes = len(response_model.commands) - 1
    exact_replacement_bytes = exact_envelope_bytes + exact_comma_bytes
    assert len(heartbeat.content) - len(serialized_empty_response) == exact_replacement_bytes
    assert exact_replacement_bytes <= (MAX_DEVICE_RESPONSE_BYTES - len(serialized_empty_response))

    async with session_factory() as session:
        commands = (
            await session.scalars(
                select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
            )
        ).all()
        assert len(commands) == len(deployment_ids)
        assert sum(command.state == "delivered" for command in commands) == len(delivered)
        assert sum(command.state == "queued" for command in commands) == (
            len(deployment_ids) - len(delivered)
        )
        assert all(
            command.attempt == (1 if command.state == "delivered" else 0) for command in commands
        )


@pytest.mark.asyncio
async def test_firmware_artifact_can_be_removed_only_when_no_deployment_is_active(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Artifact retention target"
    )
    image = b"PowerMeter disposable OTA fixture\0" * 64
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.8",
            "build_number": "8",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Disposable artifact fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        stored_path = Path(release.image_path)
    assert stored_path.is_file()

    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert deployed.status_code == 202, deployed.text
    blocked = await owner_client.delete(f"/api/v1/firmware/releases/{release_id}")
    assert blocked.status_code == 409, blocked.text

    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployed.json()["deployments"][0]["id"])
        assert deployment is not None
        deployment.state = "failed"
        deployment.completed_at = datetime.now(UTC)
        await session.commit()

    removed = await owner_client.delete(f"/api/v1/firmware/releases/{release_id}")
    assert removed.status_code == 204, removed.text
    assert not stored_path.exists()
    listed = await owner_client.get("/api/v1/firmware/releases")
    row = next(item for item in listed.json()["releases"] if item["release_id"] == release_id)
    assert row["artifact_available"] is False
    redeploy = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert redeploy.status_code == 409, redeploy.text


@pytest.mark.asyncio
async def test_post_reboot_version_evidence_completes_and_advances_staged_ota(
    owner_client: AsyncClient,
) -> None:
    first_id, first_secret, _home_id = await _enroll_ota_target(
        owner_client, name="Indoor staged target"
    )
    second_id, _second_secret, _home_id = await _enroll_ota_target(
        owner_client, name="Outdoor staged target"
    )
    image = b"PowerMeter staged OTA fixture\0" * 64
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.8",
            "build_number": "8",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Staged OTA advancement fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [first_id, second_id], "rollout": "staged"},
    )
    assert deployed.status_code == 202, deployed.text
    deployments = deployed.json()["deployments"]
    assert [row["device_id"] for row in deployments] == [first_id, second_id]
    assert [row["state"] for row in deployments] == ["queued", "staged"]

    async with session_factory() as session:
        first = await session.get(FirmwareDeployment, deployments[0]["id"])
        first_command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == first_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert first is not None and first_command is not None
        first.state = "validating"
        first.progress_percent = 90
        first_command.state = "succeeded"
        first_command.progress_percent = 100
        await session.commit()

    heartbeat = await _heartbeat(
        owner_client,
        device_id=first_id,
        secret=first_secret,
        firmware_version="0.1.0-rc.8",
    )
    assert heartbeat.status_code == 200, heartbeat.text

    async with session_factory() as session:
        first = await session.get(FirmwareDeployment, deployments[0]["id"])
        second = await session.get(FirmwareDeployment, deployments[1]["id"])
        second_command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == second_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert first is not None and second is not None and second_command is not None
        assert first.state == "succeeded"
        assert first.progress_percent == 100
        assert first.completed_at is not None
        assert second.state == "queued"
        assert second_command.state in {"queued", "delivered"}
