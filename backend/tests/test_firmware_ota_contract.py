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
from backend.app.models import (
    DeviceCommand,
    FirmwareDeployment,
    FirmwareDeploymentBatch,
    FirmwareRelease,
    Home,
    User,
)
from backend.app.routes.firmware import OTA_MANIFEST_FIELDS, ota_manifest_canonical
from backend.app.schemas.device import CommandEnvelope, DeviceResponse
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)
from backend.app.services.commands import create_command
from backend.app.services.firmware_deployments import reconcile_stale_firmware_deployments
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
async def test_heartbeat_defers_commands_to_fit_firmware_response_buffer(
    owner_client: AsyncClient,
) -> None:
    device_id, device_secret, _home_id = await _enroll_ota_target(owner_client)
    async with session_factory() as session:
        actor_id = await session.scalar(select(User.id))
        assert actor_id is not None
        for sequence in range(30, 34):
            await create_command(
                session,
                device_id=device_id,
                command_type="diagnostics_snapshot",
                issued_by_user_id=actor_id,
                idempotency_key=f"response-budget-{sequence}",
                payload={"sequence": sequence, "padding": "x" * 1_300},
            )
        await session.commit()

    heartbeat = await _heartbeat(owner_client, device_id=device_id, secret=device_secret)
    assert heartbeat.status_code == 200, heartbeat.text
    assert len(heartbeat.content) <= MAX_DEVICE_RESPONSE_BYTES
    assert int(heartbeat.headers["content-length"]) == len(heartbeat.content)
    delivered = heartbeat.json()["commands"]
    assert 0 < len(delivered) < 4
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
                select(DeviceCommand).where(DeviceCommand.command_type == "diagnostics_snapshot")
            )
        ).all()
        assert len(commands) == 4
        assert sum(command.state == "delivered" for command in commands) == len(delivered)
        assert sum(command.state == "queued" for command in commands) == (4 - len(delivered))
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


@pytest.mark.asyncio
async def test_two_sensor_partial_ota_retries_only_outdated_sensor(
    owner_client: AsyncClient,
) -> None:
    indoor_id, indoor_secret, _home_id = await _enroll_ota_target(owner_client, name="Indoor-AC")
    outdoor_id, outdoor_secret, _home_id = await _enroll_ota_target(owner_client, name="Outdoor-AC")
    image = b"PowerMeter two-sensor OTA fixture\0" * 64
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
            "release_notes": "Two-sensor partial OTA fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [indoor_id, outdoor_id], "rollout": "immediate"},
    )
    assert deployed.status_code == 202, deployed.text
    original_batch_id = deployed.json()["batch_id"]
    assert {row["device_id"] for row in deployed.json()["deployments"]} == {
        indoor_id,
        outdoor_id,
    }

    indoor_offer = await _heartbeat(owner_client, device_id=indoor_id, secret=indoor_secret)
    outdoor_offer = await _heartbeat(owner_client, device_id=outdoor_id, secret=outdoor_secret)
    indoor_command = indoor_offer.json()["commands"][0]
    outdoor_command = outdoor_offer.json()["commands"][0]
    succeeded_result = {
        "state": "succeeded",
        "progress_percent": 100,
        "result_code": "ok",
        "evidence": {"post_boot_valid": True},
    }
    indoor_result = await _heartbeat(
        owner_client,
        device_id=indoor_id,
        secret=indoor_secret,
        firmware_version="v0.1.0-rc.8",
        command_results=[{"command_id": indoor_command["command_id"], **succeeded_result}],
    )
    assert indoor_result.status_code == 200, indoor_result.text
    outdoor_result = await _heartbeat(
        owner_client,
        device_id=outdoor_id,
        secret=outdoor_secret,
        firmware_version="0.1.0-rc.7",
        command_results=[{"command_id": outdoor_command["command_id"], **succeeded_result}],
    )
    assert outdoor_result.status_code == 200, outdoor_result.text

    listed = await owner_client.get("/api/v1/firmware/releases")
    assert listed.status_code == 200, listed.text
    release = next(row for row in listed.json()["releases"] if row["release_id"] == release_id)
    batch = next(row for row in release["deployment_batches"] if row["id"] == original_batch_id)
    assert batch["state"] == "partial"
    assert (batch["succeeded"], batch["failed"], batch["pending"]) == (1, 1, 0)
    jobs = {row["device_id"]: row for row in batch["jobs"]}
    assert jobs[indoor_id]["state"] == "succeeded"
    assert jobs[indoor_id]["reported_firmware_after_reboot"] == "v0.1.0-rc.8"
    assert jobs[outdoor_id]["state"] == "failed"
    assert jobs[outdoor_id]["error_code"] == "OTA_VERSION_NOT_CONFIRMED"
    assert jobs[outdoor_id]["reported_firmware_after_reboot"] == "0.1.0-rc.7"
    assert jobs[outdoor_id]["retry_eligible"] is True

    retried = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{original_batch_id}/retry",
        json={"device_ids": [outdoor_id]},
    )
    assert retried.status_code == 202, retried.text
    assert [row["device_id"] for row in retried.json()["deployments"]] == [outdoor_id]
    async with session_factory() as session:
        deployments = list(
            (
                await session.scalars(
                    select(FirmwareDeployment).where(
                        FirmwareDeployment.firmware_release_id == release_id
                    )
                )
            ).all()
        )
        assert sum(row.device_id == indoor_id for row in deployments) == 1
        assert sorted(row.attempt for row in deployments if row.device_id == outdoor_id) == [1, 2]

    retry_offer = await _heartbeat(owner_client, device_id=outdoor_id, secret=outdoor_secret)
    retry_command = retry_offer.json()["commands"][0]
    retry_result = await _heartbeat(
        owner_client,
        device_id=outdoor_id,
        secret=outdoor_secret,
        firmware_version="0.1.0-rc.8",
        command_results=[{"command_id": retry_command["command_id"], **succeeded_result}],
    )
    assert retry_result.status_code == 200, retry_result.text

    next_image = b"PowerMeter upload after partial fixture\0" * 64
    next_digest = hashlib.sha256(next_image).hexdigest()
    next_upload = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", next_image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.9",
            "build_number": "9",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": next_digest,
            "release_notes": "Upload remains available after a partial batch.",
        },
    )
    assert next_upload.status_code == 201, next_upload.text


@pytest.mark.asyncio
async def test_ota_rollback_is_terminal_and_does_not_complete_from_another_sensor(
    owner_client: AsyncClient,
) -> None:
    indoor_id, indoor_secret, _home_id = await _enroll_ota_target(
        owner_client, name="Rollback target"
    )
    outdoor_id, outdoor_secret, _home_id = await _enroll_ota_target(
        owner_client, name="Unrelated version witness"
    )
    release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=indoor_id, sequence=71
    )
    offer = await _heartbeat(owner_client, device_id=indoor_id, secret=indoor_secret)
    command = offer.json()["commands"][0]

    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        release = await session.get(FirmwareRelease, release_id)
        assert deployment is not None and release is not None
        deployment.state = "validating"
        deployment.progress_percent = 90
        await session.commit()
        target_version = release.semantic_version

    unrelated = await _heartbeat(
        owner_client,
        device_id=outdoor_id,
        secret=outdoor_secret,
        firmware_version=target_version,
    )
    assert unrelated.status_code == 200, unrelated.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None
        assert deployment.state == "validating"
        assert "post_reboot_firmware_version" not in deployment.evidence

    rolled_back = await _heartbeat(
        owner_client,
        device_id=indoor_id,
        secret=indoor_secret,
        firmware_version="0.1.0-rc.7",
        command_results=[
            {
                "command_id": command["command_id"],
                "state": "rolled_back",
                "progress_percent": 100,
                "result_code": "rollback_confirmed",
                "evidence": {"rollback_confirmed": True},
            }
        ],
    )
    assert rolled_back.status_code == 200, rolled_back.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None
        batch = await session.get(FirmwareDeploymentBatch, deployment.batch_id)
        assert batch is not None
        assert deployment.state == "rolled_back"
        assert deployment.error_code == "OTA_FIRMWARE_ROLLED_BACK"
        assert batch.state == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_state", ["queued", "downloading", "rebooting", "validating"])
async def test_stale_ota_job_reconciliation_is_terminal_and_retryable(
    owner_client: AsyncClient, stale_state: str
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name=f"Stale {stale_state} target"
    )
    _release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=70
    )
    stale_at = datetime.now(UTC) - timedelta(days=2)
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None
        deployment.state = stale_state
        deployment.updated_at = stale_at
        batch_id = deployment.batch_id
        await session.commit()

    async with session_factory() as session:
        reconciled = await reconcile_stale_firmware_deployments(session, now=datetime.now(UTC))
        assert reconciled == (deployment_id,)
        await session.commit()

    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        batch = await session.get(FirmwareDeploymentBatch, batch_id)
        assert deployment is not None and batch is not None
        assert deployment.state == "timed_out"
        assert deployment.error_code == "OTA_JOB_TIMED_OUT"
        assert deployment.evidence["last_confirmed_stage"] == stale_state
        assert batch.state == "failed"
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        if command is not None and command.payload.get("deployment_id") == deployment_id:
            assert command.state in {"expired", "succeeded"}


@pytest.mark.asyncio
async def test_duplicate_ota_job_is_rejected_and_queued_batch_can_be_cancelled(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Cancelable OTA target"
    )
    release_id, _deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=80
    )
    listed = await owner_client.get("/api/v1/firmware/releases")
    release = next(row for row in listed.json()["releases"] if row["release_id"] == release_id)
    batch_id = release["deployment_batches"][0]["id"]
    duplicate = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert duplicate.status_code == 409, duplicate.text

    cancelled = await owner_client.post(f"/api/v1/firmware/deployment-batches/{batch_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, _deployment_id)
        assert deployment is not None
        assert deployment.state == "cancelled"
        assert deployment.error_code == "OTA_CANCELLED_BY_ADMINISTRATOR"
