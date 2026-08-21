from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import orjson
import pytest
from backend.app.config import get_settings
from backend.app.constants import MAX_COMMAND_DELIVERY_ATTEMPT, MAX_DEVICE_RESPONSE_BYTES
from backend.app.main import engine, session_factory
from backend.app.models import (
    AuditEvent,
    DeviceCommand,
    FirmwareDeployment,
    FirmwareDeploymentBatch,
    FirmwareRelease,
    Home,
    User,
)
from backend.app.routes import devices as firmware_routes_device
from backend.app.routes import firmware as firmware_routes
from backend.app.routes.firmware import OTA_MANIFEST_FIELDS, ota_manifest_canonical
from backend.app.schemas.device import CommandEnvelope, DeviceResponse
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)
from backend.app.services import commands as command_service
from backend.app.services import firmware_deployments
from backend.app.services.commands import create_command
from backend.app.services.firmware_deployments import (
    ARTIFACT_RECOVERY_GRACE,
    apply_firmware_deployment_retention,
    durable_replace,
    durable_unlink,
    durable_write_bytes,
    lock_firmware_ota_graph,
    recalculate_firmware_batch,
    reconcile_firmware_artifact_quarantines,
    reconcile_stale_firmware_deployments,
)
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from worker.app import jobs as worker_jobs


def test_ota_graph_lock_order_is_command_release_batch_deployment() -> None:
    source = inspect.getsource(lock_firmware_ota_graph)
    command_lock = source.index("lock_active_ota_commands_for_deployments")
    release_lock = source.index("select(FirmwareRelease)")
    batch_lock = source.index("select(FirmwareDeploymentBatch)")
    deployment_lock = source.rindex("select(FirmwareDeployment)")
    assert command_lock < release_lock < batch_lock < deployment_lock

    result_source = inspect.getsource(command_service.apply_command_results)
    command_lock = result_source.index(".order_by(DeviceCommand.id)")
    graph_lock = result_source.index("lock_firmware_ota_graph")
    first_deployment_mutation = result_source.index('deployment.state = "downloading"')
    assert command_lock < graph_lock < first_deployment_mutation

    delete_source = inspect.getsource(firmware_routes.permanently_delete_firmware_release)
    coordinator_lock = delete_source.index("_lock_firmware_lifecycle_coordinator")
    ordered_release_lock = delete_source.index("select(FirmwareRelease)")
    target_selection = delete_source.index("release = next")
    batch_lock = delete_source.index("select(FirmwareDeploymentBatch.id)", target_selection)
    deployment_lock = delete_source.index("select(FirmwareDeployment)", target_selection)
    assert coordinator_lock < ordered_release_lock < target_selection < batch_lock < deployment_lock
    assert ".order_by(FirmwareRelease.id)" in delete_source

    shared_reference_source = inspect.getsource(firmware_routes._shared_artifact_reference_ids)
    assert "with_for_update" not in shared_reference_source

    heartbeat_source = inspect.getsource(firmware_routes_device.heartbeat)
    result_graph = heartbeat_source.index("ingestion_graph = await apply_command_results")
    device_mutation = heartbeat_source.index("device.firmware_version =")
    reconciliation = heartbeat_source.index("reconcile_firmware_version_heartbeat")
    assert result_graph < device_mutation < reconciliation
    assert "locked_graph=ingestion_graph" in heartbeat_source

    stateless_source = inspect.getsource(firmware_routes_device.stateless_telemetry)
    result_graph = stateless_source.index("ingestion_graph = await apply_command_results")
    device_ingestion = stateless_source.index("ingest_stateless_sample")
    reconciliation = stateless_source.index("reconcile_firmware_version_heartbeat")
    assert result_graph < device_ingestion < reconciliation
    assert "locked_graph=ingestion_graph" in stateless_source


def _esp32s3_test_image(
    semantic_version: str,
    payload: bytes,
    *,
    project_name: str = "power-monitor-sensor-headless",
    build_bytes: bytes | None = None,
    segment_count: int = 1,
) -> bytes:
    """Build the bounded ESP image/app descriptor prefix used by upload tests."""

    if len(semantic_version.encode("ascii")) > 31 or len(project_name.encode("ascii")) > 31:
        raise ValueError("test application identity exceeds ESP descriptor field size")
    descriptor = bytearray(256)
    descriptor[0:4] = (0xABCD5432).to_bytes(4, "little")
    version_bytes = semantic_version.encode("ascii")
    descriptor[16 : 16 + len(version_bytes)] = version_bytes
    project_bytes = project_name.encode("ascii")
    descriptor[48 : 48 + len(project_bytes)] = project_bytes
    identity = (
        build_bytes
        if build_bytes is not None
        else hashlib.sha256(b"test-elf\0" + version_bytes + b"\0" + payload).digest()
    )
    if len(identity) != 32:
        raise ValueError("test firmware build identity must contain 32 bytes")
    descriptor[144:176] = identity
    segment_data = bytes(descriptor) + payload
    header = bytearray(24)
    header[0] = 0xE9
    header[1] = segment_count
    header[12:14] = (9).to_bytes(2, "little")
    segment_header = bytearray(8)
    segment_header[0:4] = (0x3C000020).to_bytes(4, "little")
    segment_header[4:8] = len(segment_data).to_bytes(4, "little")
    return bytes(header + segment_header + segment_data)


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


def test_artifact_transitions_sync_content_and_directory_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synced_files: list[int] = []
    synced_directories: list[Path] = []
    monkeypatch.setattr(os, "fsync", synced_files.append)
    monkeypatch.setattr(
        firmware_deployments,
        "_fsync_directory",
        synced_directories.append,
    )
    directory = Path(".test-runtime") / f"artifact-fsync-{uuid.uuid4()}"
    directory.mkdir(parents=True)
    pending = directory / "artifact.pending-upload"
    final = directory / "artifact.bin"
    try:
        durable_write_bytes(pending, b"durable artifact")
        assert pending.read_bytes() == b"durable artifact"
        assert len(synced_files) == 1
        assert synced_directories == [directory]

        durable_replace(pending, final)
        assert final.read_bytes() == b"durable artifact"
        assert len(synced_files) == 2
        assert synced_directories == [directory, directory]

        durable_unlink(final)
        assert not final.exists()
        assert synced_directories == [directory, directory, directory]
    finally:
        pending.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        directory.rmdir()


def _heartbeat_body(
    *,
    firmware_version: str = "0.1.0-rc.7",
    firmware_build_id: str | None = None,
    command_results: list[dict[str, object]] | None = None,
) -> bytes:
    payload: dict[str, object] = {
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
    if firmware_build_id is not None:
        payload["firmware_build_id"] = firmware_build_id
    return orjson.dumps(payload)


async def _heartbeat(
    client: AsyncClient,
    *,
    device_id: str,
    secret: bytes,
    firmware_version: str = "0.1.0-rc.7",
    firmware_build_id: str | None = None,
    command_results: list[dict[str, object]] | None = None,
) -> Response:
    path = "/api/v1/device/heartbeat"
    body = _heartbeat_body(
        firmware_version=firmware_version,
        firmware_build_id=firmware_build_id,
        command_results=command_results,
    )
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


async def _stateless_firmware_report(
    client: AsyncClient,
    *,
    device_id: str,
    secret: bytes,
    firmware_version: str,
    firmware_build_id: str,
    sample_sequence: int,
) -> Response:
    path = "/api/v1/device/telemetry/v2"
    body = orjson.dumps(
        {
            "telemetry_protocol": "pm-telemetry/2.0.0",
            "sensor_id": device_id,
            "boot_id": "323e4567-e89b-12d3-a456-426614174000",
            "sample_sequence": sample_sequence,
            "sampled_at": None,
            "uptime_ms": sample_sequence * 5000,
            "voltage_v": "240.000",
            "current_a": "0.5000",
            "active_power_w": "120.000",
            "frequency_hz": "60.000",
            "power_factor": "0.9000",
            "pzem_energy_wh": 1000 + sample_sequence,
            "pzem_status": "ok",
            "firmware_version": firmware_version,
            "firmware_build_id": firmware_build_id,
            "time_status": "untrusted",
            "wifi_rssi": -55,
            "command_results": [],
        }
    )
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
    image = _esp32s3_test_image(
        semantic_version, (f"PowerMeter OTA fixture {semantic_version}\0".encode()) * 64
    )
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

    image = _esp32s3_test_image("0.1.1-rc.1", b"PowerMeter V2 OTA contract fixture\0" * 64)
    image_sha256 = hashlib.sha256(image).hexdigest()
    firmware_build_id = image[176:208].hex()
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
            "firmware_build_id": firmware_build_id,
            "release_notes": "Exact OTA command contract fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release = uploaded.json()["release"]
    assert release["build_number"] == 101
    assert release["minimum_boot_version"] == 1
    assert release["artifact_available"] is True
    assert release["firmware_build_id"] == firmware_build_id
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
@pytest.mark.parametrize(
    "invalid_case, expected_status",
    [
        ("wrong_version", 409),
        ("wrong_project", 409),
        ("zero_segments", 422),
        ("too_many_segments", 422),
        ("zero_build_id", 422),
        ("truncated_first_segment", 422),
        ("client_build_id_mismatch", 409),
    ],
)
async def test_upload_rejects_untrusted_or_ambiguous_esp_application_identity(
    owner_client: AsyncClient,
    invalid_case: str,
    expected_status: int,
) -> None:
    submitted_version = "0.1.0-rc.75"
    embedded_version = "0.1.0-rc.74" if invalid_case == "wrong_version" else submitted_version
    project_name = (
        "not-power-meter" if invalid_case == "wrong_project" else "power-monitor-sensor-headless"
    )
    segment_count = {
        "zero_segments": 0,
        "too_many_segments": 17,
    }.get(invalid_case, 1)
    build_bytes = b"\0" * 32 if invalid_case == "zero_build_id" else None
    image = bytearray(
        _esp32s3_test_image(
            embedded_version,
            b"invalid ESP identity fixture",
            project_name=project_name,
            build_bytes=build_bytes,
            segment_count=segment_count,
        )
    )
    if invalid_case == "truncated_first_segment":
        image[28:32] = (len(image) * 2).to_bytes(4, "little")
    image_bytes = bytes(image)
    data = {
        "semantic_version": submitted_version,
        "build_number": "75",
        "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
        "minimum_boot_version": "1",
        "minimum_config_version": "1",
        "expected_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "release_notes": "Rejected ESP application identity fixture.",
    }
    if invalid_case == "client_build_id_mismatch":
        data["firmware_build_id"] = "f" * 64
    rejected = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image_bytes, "application/octet-stream")},
        data=data,
    )
    assert rejected.status_code == expected_status, rejected.text
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(FirmwareRelease.id).where(
                    FirmwareRelease.semantic_version == submitted_version
                )
            )
            is None
        )


@pytest.mark.asyncio
async def test_ota_rejects_same_version_before_creating_a_device_command(
    owner_client: AsyncClient,
) -> None:
    device_id, _device_secret, _home_id = await _enroll_ota_target(owner_client)
    image = _esp32s3_test_image("0.1.0-rc.7", b"PowerMeter same-version OTA fixture\0" * 64)
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
    image = _esp32s3_test_image("0.1.0-rc.8", b"PowerMeter disposable OTA fixture\0" * 64)
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
    retired = await owner_client.delete(f"/api/v1/firmware/releases/{release_id}")
    assert retired.status_code == 422, retired.text
    blocked = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/delete-permanently",
        json={
            "confirmation": "DELETE RELEASE PERMANENTLY",
            "semantic_version": "0.1.0-rc.8",
            "build_number": "8",
            "sha256": digest,
        },
    )
    assert blocked.status_code == 409, blocked.text

    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployed.json()["deployments"][0]["id"])
        assert deployment is not None
        deployment.state = "failed"
        deployment.completed_at = datetime.now(UTC)
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert command is not None
        command.state = "failed"
        await session.commit()

    removed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/delete-permanently",
        json={
            "confirmation": "DELETE RELEASE PERMANENTLY",
            "semantic_version": "0.1.0-rc.8",
            "build_number": "8",
            "sha256": digest,
        },
    )
    assert removed.status_code == 204, removed.text
    assert not stored_path.exists()
    listed = await owner_client.get("/api/v1/firmware/releases", params={"show_deleted": "true"})
    row = next(item for item in listed.json()["releases"] if item["release_id"] == release_id)
    assert row["artifact_available"] is False
    redeploy = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert redeploy.status_code == 409, redeploy.text


@pytest.mark.asyncio
async def test_release_archive_restore_and_confirmed_permanent_delete(
    owner_client: AsyncClient,
) -> None:
    image = _esp32s3_test_image("0.1.0-rc.88", b"PowerMeter lifecycle release fixture\0" * 64)
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.88",
            "build_number": "88",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Lifecycle release fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        artifact = Path(release.image_path)

    archived = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/archive",
        json={"confirmation": "ARCHIVE FIRMWARE RECORD"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["release_state"] == "archived"
    assert artifact.is_file()
    default_list = await owner_client.get("/api/v1/firmware/releases")
    assert release_id not in {row["release_id"] for row in default_list.json()["releases"]}
    archived_list = await owner_client.get(
        "/api/v1/firmware/releases", params={"show_archived": "true"}
    )
    assert release_id in {row["release_id"] for row in archived_list.json()["releases"]}

    restored = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/restore",
        json={"confirmation": "RESTORE FIRMWARE RECORD"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["release_state"] == "available"

    wrong_confirmation = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/delete-permanently",
        json={
            "confirmation": "DELETE RELEASE PERMANENTLY",
            "semantic_version": "0.1.0-rc.88",
            "build_number": "88",
            "sha256": "0" * 64,
        },
    )
    assert wrong_confirmation.status_code == 409, wrong_confirmation.text
    deleted = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/delete-permanently",
        json={
            "confirmation": "DELETE RELEASE PERMANENTLY",
            "semantic_version": "0.1.0-rc.88",
            "build_number": "88",
            "sha256": digest,
        },
    )
    assert deleted.status_code == 204, deleted.text
    assert not artifact.exists()
    deleted_list = await owner_client.get(
        "/api/v1/firmware/releases", params={"show_deleted": "true"}
    )
    tombstone = next(
        row for row in deleted_list.json()["releases"] if row["release_id"] == release_id
    )
    assert tombstone["release_state"] == "deleted"
    assert tombstone["artifact_available"] is False
    assert tombstone["deploy_eligible"] is False


@pytest.mark.asyncio
async def test_retry_view_never_advertises_an_undeployable_release(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Retry release eligibility target"
    )
    release_id, deployment_id = await _upload_and_deploy(
        owner_client,
        device_id=device_id,
        sequence=76,
    )
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        release = await session.get(FirmwareRelease, release_id)
        assert deployment is not None and deployment.batch_id is not None
        assert release is not None and release.firmware_build_id is not None
        batch_id = deployment.batch_id
        original_build_id = release.firmware_build_id
        artifact = Path(release.image_path)
        deployment.state = "failed"
        deployment.completed_at = datetime.now(UTC)
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert command is not None
        command.state = "failed"
        await recalculate_firmware_batch(session, batch_id)
        await session.commit()

    archived = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/archive",
        json={"confirmation": "ARCHIVE FIRMWARE RECORD"},
    )
    assert archived.status_code == 200, archived.text
    batches = await owner_client.get("/api/v1/firmware/deployment-batches")
    batch = next(row for row in batches.json()["deployment_batches"] if row["id"] == batch_id)
    assert batch["jobs"][0]["retry_eligible"] is False
    blocked = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/retry",
        json={"device_ids": [device_id]},
    )
    assert blocked.status_code == 409, blocked.text

    restored = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/restore",
        json={"confirmation": "RESTORE FIRMWARE RECORD"},
    )
    assert restored.status_code == 200, restored.text
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        release.firmware_build_id = None
        await session.commit()
    batches = await owner_client.get("/api/v1/firmware/deployment-batches")
    batch = next(row for row in batches.json()["deployment_batches"] if row["id"] == batch_id)
    assert batch["jobs"][0]["retry_eligible"] is False

    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        release.firmware_build_id = original_build_id
        await session.commit()
    artifact.unlink()
    batches = await owner_client.get("/api/v1/firmware/deployment-batches")
    batch = next(row for row in batches.json()["deployment_batches"] if row["id"] == batch_id)
    assert batch["jobs"][0]["retry_eligible"] is False


@pytest.mark.asyncio
async def test_current_transition_is_explicit_unique_and_pins_the_prior_release(
    owner_client: AsyncClient,
) -> None:
    uploads: list[tuple[str, str, str]] = []
    for suffix in (86, 87):
        version = f"0.1.0-rc.{suffix}"
        image = _esp32s3_test_image(
            version, f"PowerMeter current transition {suffix}\0".encode() * 64
        )
        digest = hashlib.sha256(image).hexdigest()
        uploaded = await owner_client.post(
            "/api/v1/firmware/releases",
            files={"image": ("firmware.bin", image, "application/octet-stream")},
            data={
                "semantic_version": version,
                "build_number": str(suffix),
                "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
                "minimum_boot_version": "1",
                "minimum_config_version": "1",
                "expected_sha256": digest,
                "release_notes": "Explicit current transition fixture.",
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        release = uploaded.json()["release"]
        assert release["release_state"] == "available"
        assert release["make_current_eligible"] is True
        uploads.append((release["release_id"], version, digest))

    first_id, first_version, first_digest = uploads[0]
    second_id, second_version, second_digest = uploads[1]
    first_current = await owner_client.post(
        f"/api/v1/firmware/releases/{first_id}/make-current",
        json={
            "confirmation": "MAKE CURRENT FIRMWARE",
            "semantic_version": first_version,
            "sha256": first_digest,
        },
    )
    assert first_current.status_code == 200, first_current.text
    assert first_current.json()["release_state"] == "current"

    second_current = await owner_client.post(
        f"/api/v1/firmware/releases/{second_id}/make-current",
        json={
            "confirmation": "MAKE CURRENT FIRMWARE",
            "semantic_version": second_version,
            "sha256": second_digest,
        },
    )
    assert second_current.status_code == 200, second_current.text
    listed = await owner_client.get("/api/v1/firmware/releases")
    assert listed.status_code == 200, listed.text
    by_id = {row["release_id"]: row for row in listed.json()["releases"]}
    assert by_id[second_id]["release_state"] == "current"
    assert by_id[second_id]["rollback_pinned"] is False
    assert by_id[first_id]["release_state"] == "available"
    assert by_id[first_id]["rollback_pinned"] is True
    assert by_id[first_id]["delete_eligibility"]["eligible"] is False
    assert "pinned_for_rollback" in by_id[first_id]["delete_eligibility"]["protection_reasons"]
    assert sum(row["release_state"] == "current" for row in by_id.values()) == 1

    unpinned = await owner_client.patch(
        f"/api/v1/firmware/releases/{first_id}/rollback-pin",
        json={
            "confirmation": "UPDATE ROLLBACK PROTECTION",
            "rollback_pinned": False,
        },
    )
    assert unpinned.status_code == 200, unpinned.text
    assert unpinned.json()["rollback_pinned"] is False
    repromote_current = await owner_client.post(
        f"/api/v1/firmware/releases/{second_id}/make-current",
        json={
            "confirmation": "MAKE CURRENT FIRMWARE",
            "semantic_version": second_version,
            "sha256": second_digest,
        },
    )
    assert repromote_current.status_code == 409, repromote_current.text


@pytest.mark.asyncio
async def test_artifact_reconciliation_repairs_both_crash_boundaries_and_flags_unknowns(
    owner_client: AsyncClient,
) -> None:
    image = _esp32s3_test_image("0.1.0-rc.85", b"PowerMeter artifact crash recovery fixture\0" * 64)
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.85",
            "build_number": "85",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Artifact recovery fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        original = Path(release.image_path)
    firmware_dir = original.parent
    recovery_now = datetime.now(UTC) + ARTIFACT_RECOVERY_GRACE + timedelta(seconds=1)

    pending_upload = firmware_dir / f".{release_id}.0123456789abcdef.pending-upload"
    original.replace(pending_upload)
    async with session_factory() as session:
        result = await reconcile_firmware_artifact_quarantines(
            session, firmware_dir=firmware_dir, apply=True, now=recovery_now
        )
        await session.commit()
    assert result["promoted_upload_release_ids"] == [release_id]
    assert original.read_bytes() == image

    pending_delete = firmware_dir / f".{release_id}.fedcba9876543210.pending-delete"
    original.replace(pending_delete)
    async with session_factory() as session:
        deferred = await reconcile_firmware_artifact_quarantines(
            session, firmware_dir=firmware_dir, apply=True
        )
    assert deferred["restored_release_ids"] == []
    assert deferred["purged_release_ids"] == []
    assert release_id in cast(list[str], deferred["deferred_recovery_release_ids"])
    assert pending_delete.is_file() and not original.exists()
    async with session_factory() as session:
        precommit = await reconcile_firmware_artifact_quarantines(
            session, firmware_dir=firmware_dir, apply=True, now=recovery_now
        )
        await session.commit()
    assert precommit["restored_release_ids"] == [release_id]
    assert original.read_bytes() == image

    original.replace(pending_delete)
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        release.lifecycle_state = "deleted"
        release.image_path = ""
        release.deleted_at = datetime.now(UTC)
        release.artifact_deleted_at = release.deleted_at
        await session.commit()
    async with session_factory() as session:
        postcommit = await reconcile_firmware_artifact_quarantines(
            session, firmware_dir=firmware_dir, apply=True, now=recovery_now
        )
        await session.commit()
    assert postcommit["purged_release_ids"] == [release_id]
    assert not pending_delete.exists()

    unknown_id = str(uuid.uuid4())
    unknown_upload = firmware_dir / f".{unknown_id}.0011223344556677.pending-upload"
    unknown_final = firmware_dir / f"{unknown_id}.bin"
    unknown_temp = firmware_dir / f"{unknown_id}.tmp"
    unknown_upload.write_bytes(b"unknown upload")
    unknown_final.write_bytes(b"unknown final")
    unknown_temp.write_bytes(b"unknown temp")
    try:
        async with session_factory() as session:
            fresh_diagnostics = await reconcile_firmware_artifact_quarantines(
                session, firmware_dir=firmware_dir, apply=True
            )
        assert unknown_id in cast(list[str], fresh_diagnostics["deferred_recovery_release_ids"])
        assert unknown_upload.exists()
        async with session_factory() as session:
            diagnostics = await reconcile_firmware_artifact_quarantines(
                session, firmware_dir=firmware_dir, apply=True, now=recovery_now
            )
            await session.commit()
        assert diagnostics["attention_required"] is True
        assert diagnostics["purged_unknown_upload_release_ids"] == [unknown_id]
        assert unknown_id in cast(list[str], diagnostics["orphan_final_release_ids"])
        assert unknown_id in cast(list[str], diagnostics["orphan_temp_release_ids"])
        assert not unknown_upload.exists()
        assert unknown_final.exists() and unknown_temp.exists()
    finally:
        unknown_upload.unlink(missing_ok=True)
        unknown_final.unlink(missing_ok=True)
        unknown_temp.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_partial_pending_upload_is_removed_when_durable_write_fails(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firmware_dir = Path(get_settings().firmware_dir)
    before = set(firmware_dir.glob(".*.pending-upload")) if firmware_dir.is_dir() else set()
    image = _esp32s3_test_image("0.1.0-rc.83", b"PowerMeter partial durable write fixture\0" * 64)
    digest = hashlib.sha256(image).hexdigest()

    def fail_after_partial_write(path: Path, data: bytes) -> None:
        path.write_bytes(data[:17])
        raise OSError("injected durable write failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(firmware_routes, "durable_write_bytes", fail_after_partial_write)
        with pytest.raises(OSError, match="injected durable write failure"):
            await owner_client.post(
                "/api/v1/firmware/releases",
                files={"image": ("firmware.bin", image, "application/octet-stream")},
                data={
                    "semantic_version": "0.1.0-rc.83",
                    "build_number": "83",
                    "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
                    "minimum_boot_version": "1",
                    "minimum_config_version": "1",
                    "expected_sha256": digest,
                    "release_notes": "Partial durable write failure fixture.",
                },
            )

    assert set(firmware_dir.glob(".*.pending-upload")) == before
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(FirmwareRelease.id).where(FirmwareRelease.semantic_version == "0.1.0-rc.83")
            )
            is None
        )


@pytest.mark.asyncio
async def test_corrupt_final_artifact_is_diagnosed_and_never_deployable_or_restorable(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Corrupt artifact target"
    )
    image = _esp32s3_test_image(
        "0.1.0-rc.82", b"PowerMeter final artifact integrity fixture\0" * 64
    )
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.82",
            "build_number": "82",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Final artifact integrity fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        artifact = Path(release.image_path)
    artifact.write_bytes(b"X" + image[1:])

    listed = await owner_client.get("/api/v1/firmware/releases")
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json()["releases"] if item["release_id"] == release_id)
    assert row["artifact_available"] is False
    assert row["deploy_eligible"] is False
    assert "deployable_state_without_artifact" in row["consistency"]["issues"]
    assert (
        release_id
        in listed.json()["reconciliation"]["artifact_quarantines"]["corrupt_artifact_release_ids"]
    )
    blocked = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert blocked.status_code == 409, blocked.text
    archived = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/archive",
        json={"confirmation": "ARCHIVE FIRMWARE RECORD"},
    )
    assert archived.status_code == 200, archived.text
    restored = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/restore",
        json={"confirmation": "RESTORE FIRMWARE RECORD"},
    )
    assert restored.status_code == 409, restored.text


@pytest.mark.asyncio
async def test_shared_physical_artifact_path_protects_every_live_release_reference(
    owner_client: AsyncClient,
) -> None:
    uploads: list[tuple[str, str, str, Path]] = []
    for sequence in (81, 80):
        version = f"0.1.0-rc.{sequence}"
        image = _esp32s3_test_image(
            version, f"PowerMeter shared path fixture {sequence}\0".encode() * 64
        )
        digest = hashlib.sha256(image).hexdigest()
        uploaded = await owner_client.post(
            "/api/v1/firmware/releases",
            files={"image": ("firmware.bin", image, "application/octet-stream")},
            data={
                "semantic_version": version,
                "build_number": str(sequence),
                "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
                "minimum_boot_version": "1",
                "minimum_config_version": "1",
                "expected_sha256": digest,
                "release_notes": "Shared artifact reference fixture.",
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        release_id = uploaded.json()["release"]["release_id"]
        async with session_factory() as session:
            release = await session.get(FirmwareRelease, release_id)
            assert release is not None
            path = Path(release.image_path)
        uploads.append((release_id, version, digest, path))

    first_id, first_version, first_digest, first_path = uploads[0]
    second_id, _second_version, _second_digest, second_path = uploads[1]
    async with session_factory() as session:
        second = await session.get(FirmwareRelease, second_id)
        assert second is not None
        second.image_path = str(first_path)
        await session.commit()
    try:
        listed = await owner_client.get("/api/v1/firmware/releases")
        first = next(item for item in listed.json()["releases"] if item["release_id"] == first_id)
        assert "shared_artifact_reference" in first["delete_eligibility"]["protection_reasons"]
        blocked = await owner_client.post(
            f"/api/v1/firmware/releases/{first_id}/delete-permanently",
            json={
                "confirmation": "DELETE RELEASE PERMANENTLY",
                "semantic_version": first_version,
                "build_number": "81",
                "sha256": first_digest,
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert first_path.is_file()
    finally:
        async with session_factory() as session:
            second = await session.get(FirmwareRelease, second_id)
            assert second is not None
            second.image_path = str(second_path)
            await session.commit()


@pytest.mark.asyncio
async def test_artifact_upload_and_delete_restore_files_when_database_commit_fails(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    firmware_dir = Path(get_settings().firmware_dir)
    before = set(firmware_dir.glob(".*.pending-upload")) if firmware_dir.is_dir() else set()
    image = _esp32s3_test_image("0.1.0-rc.84", b"PowerMeter commit failure fixture\0" * 64)
    digest = hashlib.sha256(image).hexdigest()
    original_commit = AsyncSession.commit

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("injected firmware commit failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected firmware commit failure"):
            await owner_client.post(
                "/api/v1/firmware/releases",
                files={"image": ("firmware.bin", image, "application/octet-stream")},
                data={
                    "semantic_version": "0.1.0-rc.84",
                    "build_number": "84",
                    "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
                    "minimum_boot_version": "1",
                    "minimum_config_version": "1",
                    "expected_sha256": digest,
                    "release_notes": "Commit failure upload fixture.",
                },
            )
    assert set(firmware_dir.glob(".*.pending-upload")) == before
    async with session_factory() as session:
        assert (
            await session.scalar(
                select(FirmwareRelease.id).where(FirmwareRelease.semantic_version == "0.1.0-rc.84")
            )
            is None
        )

    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.84",
            "build_number": "84",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Commit failure delete fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        artifact = Path(release.image_path)
    assert artifact.is_file()

    with monkeypatch.context() as patcher:
        patcher.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected firmware commit failure"):
            await owner_client.post(
                f"/api/v1/firmware/releases/{release_id}/delete-permanently",
                json={
                    "confirmation": "DELETE RELEASE PERMANENTLY",
                    "semantic_version": "0.1.0-rc.84",
                    "build_number": "84",
                    "sha256": digest,
                },
            )
    assert artifact.read_bytes() == image
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        assert release.lifecycle_state == "available"
        assert release.image_path == str(artifact)
    assert AsyncSession.commit is original_commit


@pytest.mark.asyncio
async def test_terminal_deployment_archive_hold_restore_and_tombstone_delete(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Deployment lifecycle target"
    )
    image = _esp32s3_test_image("0.1.0-rc.89", b"PowerMeter lifecycle deployment fixture\0" * 64)
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.89",
            "build_number": "89",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Lifecycle deployment fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None
        artifact = Path(release.image_path)
    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert deployed.status_code == 202, deployed.text
    batch_id = deployed.json()["batch_id"]
    deployment_id = deployed.json()["deployments"][0]["id"]
    active_archive = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/archive",
        json={"confirmation": "ARCHIVE DEPLOYMENT RECORD"},
    )
    assert active_archive.status_code == 409, active_archive.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None
        deployment.state = "failed"
        deployment.completed_at = datetime.now(UTC)
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert command is not None
        command.state = "failed"
        await recalculate_firmware_batch(session, batch_id)
        await session.commit()

    archived = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/archive",
        json={"confirmation": "ARCHIVE DEPLOYMENT RECORD"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["deployment_state"] == "archived"
    assert archived.json()["result_state"] == "failed"
    assert archived.json()["jobs"][0]["retry_eligible"] is False
    archived_retry = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/retry",
        json={"device_ids": [device_id]},
    )
    assert archived_retry.status_code == 409, archived_retry.text

    held = await owner_client.patch(
        f"/api/v1/firmware/deployment-batches/{batch_id}/troubleshooting-hold",
        json={"troubleshooting_hold": True, "reason": "Preserve failure evidence"},
    )
    assert held.status_code == 200, held.text
    blocked = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/delete-permanently",
        json={
            "confirmation": "DELETE DEPLOYMENT RECORD",
            "deployment_batch_id": batch_id,
        },
    )
    assert blocked.status_code == 409, blocked.text
    cleared = await owner_client.patch(
        f"/api/v1/firmware/deployment-batches/{batch_id}/troubleshooting-hold",
        json={"troubleshooting_hold": False, "reason": "Review complete"},
    )
    assert cleared.status_code == 200, cleared.text

    restored = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/restore",
        json={"confirmation": "RESTORE DEPLOYMENT RECORD"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["deployment_state"] == "failed"
    await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/archive",
        json={"confirmation": "ARCHIVE DEPLOYMENT RECORD"},
    )
    deleted = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/delete-permanently",
        json={
            "confirmation": "DELETE DEPLOYMENT RECORD",
            "deployment_batch_id": batch_id,
        },
    )
    assert deleted.status_code == 204, deleted.text
    assert artifact.is_file()
    listed = await owner_client.get(
        "/api/v1/firmware/deployment-batches", params={"show_deleted": "true"}
    )
    tombstone = next(row for row in listed.json()["deployment_batches"] if row["id"] == batch_id)
    assert tombstone["deployment_state"] == "deleted"
    assert tombstone["jobs"][0]["state"] == "failed"
    assert tombstone["jobs"][0]["retry_eligible"] is False
    deleted_retry = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/retry",
        json={"device_ids": [device_id]},
    )
    assert deleted_retry.status_code == 409, deleted_retry.text

    current_settings = await owner_client.get("/api/v1/firmware/lifecycle-settings")
    assert current_settings.status_code == 200, current_settings.text
    assert current_settings.json()["deployment_retention_days"] == 365
    rejected_settings = await owner_client.patch(
        "/api/v1/firmware/lifecycle-settings",
        json={"deployment_retention_days": 90},
    )
    assert rejected_settings.status_code == 422, rejected_settings.text
    updated_settings = await owner_client.patch(
        "/api/v1/firmware/lifecycle-settings",
        json={
            "deployment_retention_days": 90,
            "confirmation": "DELETE EXPIRED DEPLOYMENT HISTORY",
        },
    )
    assert updated_settings.status_code == 200, updated_settings.text
    assert updated_settings.json()["retention_policy"] == "90_days"


@pytest.mark.asyncio
async def test_retention_compacts_only_expired_archived_deployments_and_preserves_artifact(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Retention lifecycle target"
    )
    image = _esp32s3_test_image("0.1.0-rc.90", b"PowerMeter deployment retention fixture\0" * 64)
    digest = hashlib.sha256(image).hexdigest()
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.90",
            "build_number": "90",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": digest,
            "release_notes": "Deployment retention fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [device_id], "rollout": "immediate"},
    )
    assert deployed.status_code == 202, deployed.text
    batch_id = deployed.json()["batch_id"]
    deployment_id = deployed.json()["deployments"][0]["id"]
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None
        expected_build_id = deployment.evidence["expected_firmware_build_id"]
        assert isinstance(expected_build_id, str)
        deployment.evidence = {
            **deployment.evidence,
            "post_reboot_firmware_build_id": expected_build_id,
        }
        deployment.state = "failed"
        deployment.completed_at = datetime.now(UTC)
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert command is not None
        command.state = "failed"
        await recalculate_firmware_batch(session, batch_id)
        await session.commit()
    archived = await owner_client.post(
        f"/api/v1/firmware/deployment-batches/{batch_id}/archive",
        json={"confirmation": "ARCHIVE DEPLOYMENT RECORD"},
    )
    assert archived.status_code == 200, archived.text
    configured = await owner_client.patch(
        "/api/v1/firmware/lifecycle-settings",
        json={
            "deployment_retention_days": 90,
            "confirmation": "DELETE EXPIRED DEPLOYMENT HISTORY",
        },
    )
    assert configured.status_code == 200, configured.text
    effective_now = datetime.now(UTC)
    async with session_factory() as session:
        batch = await session.get(FirmwareDeploymentBatch, batch_id)
        release = await session.get(FirmwareRelease, release_id)
        assert batch is not None and release is not None
        batch.archived_at = effective_now - timedelta(days=91)
        artifact = Path(release.image_path)
        await session.commit()
    assert artifact.is_file()
    async with session_factory() as session:
        removed = await apply_firmware_deployment_retention(session, now=effective_now)
        assert removed == (batch_id,)
        await session.commit()
    async with session_factory() as session:
        batch = await session.get(FirmwareDeploymentBatch, batch_id)
        deployment = await session.get(FirmwareDeployment, deployment_id)
        release = await session.get(FirmwareRelease, release_id)
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_code == "FIRMWARE_DEPLOYMENT_RETENTION_APPLIED",
                AuditEvent.target_id == batch_id,
            )
        )
        assert batch is not None and batch.deleted_at is not None
        assert deployment is not None
        assert deployment.evidence["expected_firmware_build_id"] == expected_build_id
        assert deployment.evidence["post_reboot_firmware_build_id"] == expected_build_id
        assert deployment.evidence["audit_tombstone"] is True
        assert release is not None and release.image_path == str(artifact)
        assert event is not None and event.details["artifact_preserved"] is True
    assert artifact.is_file()


@pytest.mark.asyncio
async def test_retry_creation_and_audit_roll_back_together_when_commit_fails(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Atomic retry target"
    )
    release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=79
    )
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None and deployment.batch_id is not None
        source_batch_id = deployment.batch_id
        deployment.state = "failed"
        deployment.completed_at = datetime.now(UTC)
        commands = (
            await session.scalars(
                select(DeviceCommand).where(
                    DeviceCommand.device_id == device_id,
                    DeviceCommand.command_type == "ota_install",
                )
            )
        ).all()
        command = next(
            (row for row in commands if row.payload.get("deployment_id") == deployment_id),
            None,
        )
        assert command is not None
        command.state = "failed"
        await recalculate_firmware_batch(session, source_batch_id)
        await session.commit()
    async with session_factory() as session:
        batch_count_before = len(
            (
                await session.scalars(
                    select(FirmwareDeploymentBatch).where(
                        FirmwareDeploymentBatch.firmware_release_id == release_id
                    )
                )
            ).all()
        )
        command_count_before = len(
            (
                await session.scalars(
                    select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
                )
            ).all()
        )

    async def fail_commit(_session: AsyncSession) -> None:
        raise RuntimeError("injected retry commit failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected retry commit failure"):
            await owner_client.post(
                f"/api/v1/firmware/deployment-batches/{source_batch_id}/retry",
                json={"device_ids": [device_id]},
            )

    async with session_factory() as session:
        batches_after = (
            await session.scalars(
                select(FirmwareDeploymentBatch).where(
                    FirmwareDeploymentBatch.firmware_release_id == release_id
                )
            )
        ).all()
        commands_after = (
            await session.scalars(
                select(DeviceCommand).where(DeviceCommand.command_type == "ota_install")
            )
        ).all()
        retry_events = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.event_code == "FIRMWARE_DEPLOYMENT_RETRIED")
            )
        ).all()
    assert len(batches_after) == batch_count_before
    assert len(commands_after) == command_count_before
    assert retry_events == []


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
    image = _esp32s3_test_image("0.1.0-rc.8", b"PowerMeter staged OTA fixture\0" * 64)
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
    firmware_build_id = uploaded.json()["release"]["firmware_build_id"]
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
        firmware_build_id=firmware_build_id,
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
async def test_worker_stale_stage_scan_cannot_resurrect_a_cancelled_deployment(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id, _first_secret, _home_id = await _enroll_ota_target(
        owner_client, name="Worker completed target"
    )
    second_id, _second_secret, _home_id = await _enroll_ota_target(
        owner_client, name="Worker cancellation target"
    )
    image = _esp32s3_test_image("0.1.0-rc.73", b"PowerMeter worker stage race fixture\0" * 64)
    uploaded = await owner_client.post(
        "/api/v1/firmware/releases",
        files={"image": ("firmware.bin", image, "application/octet-stream")},
        data={
            "semantic_version": "0.1.0-rc.73",
            "build_number": "73",
            "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
            "minimum_boot_version": "1",
            "minimum_config_version": "1",
            "expected_sha256": hashlib.sha256(image).hexdigest(),
            "release_notes": "Worker stage race fixture.",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    release_id = uploaded.json()["release"]["release_id"]
    deployed = await owner_client.post(
        f"/api/v1/firmware/releases/{release_id}/deploy",
        json={"device_ids": [first_id, second_id], "rollout": "staged"},
    )
    assert deployed.status_code == 202, deployed.text
    first_deployment_id, second_deployment_id = [
        row["id"] for row in deployed.json()["deployments"]
    ]
    async with session_factory() as session:
        first = await session.get(FirmwareDeployment, first_deployment_id)
        first_command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == first_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert first is not None and first_command is not None
        first.state = "succeeded"
        first.completed_at = datetime.now(UTC)
        first_command.state = "succeeded"
        await session.commit()

    original_advance = worker_jobs.advance_next_staged_firmware_deployment

    async def cancel_after_worker_scan(
        session: AsyncSession, scanned_release_id: str
    ) -> FirmwareDeployment | None:
        staged = await session.scalar(
            select(FirmwareDeployment).where(
                FirmwareDeployment.id == second_deployment_id,
                FirmwareDeployment.state == "staged",
            )
        )
        assert staged is not None
        staged.state = "cancelled"
        staged.completed_at = datetime.now(UTC)
        await session.flush()
        return await original_advance(session, scanned_release_id)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            worker_jobs,
            "advance_next_staged_firmware_deployment",
            cancel_after_worker_scan,
        )
        async with session_factory() as session:
            advanced = await worker_jobs.advance_staged_rollouts(session)
            await session.commit()
    assert advanced == 0
    async with session_factory() as session:
        second = await session.get(FirmwareDeployment, second_deployment_id)
        second_command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == second_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert second is not None and second.state == "cancelled"
        assert second_command is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported_build_id",
    [None, "0" * 64],
    ids=["legacy-build-unavailable", "same-version-different-build"],
)
async def test_post_reboot_same_version_never_confirms_without_exact_build_identity(
    owner_client: AsyncClient,
    reported_build_id: str | None,
) -> None:
    device_id, secret, _home_id = await _enroll_ota_target(
        owner_client, name=f"Build identity target {reported_build_id is None}"
    )
    release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=76
    )
    offered = await _heartbeat(owner_client, device_id=device_id, secret=secret)
    assert offered.status_code == 200, offered.text
    command = offered.json()["commands"][0]
    async with session_factory() as session:
        release = await session.get(FirmwareRelease, release_id)
        assert release is not None and release.firmware_build_id is not None
        assert reported_build_id != release.firmware_build_id
        target_version = release.semantic_version
        expected_build_id = release.firmware_build_id

    result = await _heartbeat(
        owner_client,
        device_id=device_id,
        secret=secret,
        firmware_version=target_version,
        firmware_build_id=reported_build_id,
        command_results=[
            {
                "command_id": command["command_id"],
                "state": "succeeded",
                "progress_percent": 100,
                "result_code": "ok",
                "evidence": {"post_boot_valid": True},
            }
        ],
    )
    assert result.status_code == 200, result.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None
        assert deployment.state == "failed"
        assert deployment.error_code == "OTA_BUILD_ID_NOT_CONFIRMED"
        assert deployment.evidence["expected_firmware_build_id"] == expected_build_id
        assert deployment.evidence["post_reboot_firmware_build_id"] == reported_build_id


@pytest.mark.asyncio
async def test_two_sensor_partial_ota_retries_only_outdated_sensor(
    owner_client: AsyncClient,
) -> None:
    indoor_id, indoor_secret, _home_id = await _enroll_ota_target(owner_client, name="Indoor-AC")
    outdoor_id, outdoor_secret, _home_id = await _enroll_ota_target(owner_client, name="Outdoor-AC")
    image = _esp32s3_test_image("0.1.0-rc.8", b"PowerMeter two-sensor OTA fixture\0" * 64)
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
    firmware_build_id = uploaded.json()["release"]["firmware_build_id"]
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
        firmware_build_id=firmware_build_id,
        command_results=[{"command_id": indoor_command["command_id"], **succeeded_result}],
    )
    assert indoor_result.status_code == 200, indoor_result.text
    outdoor_result = await _heartbeat(
        owner_client,
        device_id=outdoor_id,
        secret=outdoor_secret,
        firmware_version="0.1.0-rc.7",
        firmware_build_id=firmware_build_id,
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
        firmware_build_id=firmware_build_id,
        command_results=[{"command_id": retry_command["command_id"], **succeeded_result}],
    )
    assert retry_result.status_code == 200, retry_result.text

    next_image = _esp32s3_test_image(
        "0.1.0-rc.9", b"PowerMeter upload after partial fixture\0" * 64
    )
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
    device_id, secret, _home_id = await _enroll_ota_target(
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
    download_path = f"/api/v1/device/firmware/{release_id}"
    blocked_download = await owner_client.get(
        download_path,
        headers=_device_headers(
            device_id=device_id,
            secret=secret,
            method="GET",
            path=download_path,
        ),
    )
    assert blocked_download.status_code == 404, blocked_download.text


@pytest.mark.asyncio
async def test_download_cannot_regress_validating_or_terminal_deployment(
    owner_client: AsyncClient,
) -> None:
    device_id, secret, _home_id = await _enroll_ota_target(
        owner_client, name="Download state regression target"
    )
    release_id, deployment_id = await _upload_and_deploy(
        owner_client,
        device_id=device_id,
        sequence=75,
    )
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.command_type == "ota_install",
                DeviceCommand.device_id == device_id,
            )
        )
        assert deployment is not None and command is not None
        deployment.state = "validating"
        deployment.progress_percent = 90
        command.state = "succeeded"
        await session.commit()

    download_path = f"/api/v1/device/firmware/{release_id}"

    async def download() -> Response:
        return await owner_client.get(
            download_path,
            headers=_device_headers(
                device_id=device_id,
                secret=secret,
                method="GET",
                path=download_path,
            ),
        )

    validating_download = await download()
    assert validating_download.status_code == 404, validating_download.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None and deployment.state == "validating"
        deployment.state = "failed"
        deployment.completed_at = datetime.now(UTC)
        await session.commit()

    terminal_download = await download()
    assert terminal_download.status_code == 404, terminal_download.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None and deployment.state == "failed"


@pytest.mark.asyncio
async def test_delivered_ota_command_cannot_be_overwritten_by_cancellation(
    owner_client: AsyncClient,
) -> None:
    device_id, secret, _home_id = await _enroll_ota_target(
        owner_client, name="Delivered cancellation target"
    )
    _release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=78
    )
    offered = await _heartbeat(owner_client, device_id=device_id, secret=secret)
    assert offered.status_code == 200, offered.text
    assert len(offered.json()["commands"]) == 1
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None and deployment.batch_id is not None
        batch_id = deployment.batch_id
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert command is not None and command.state == "delivered"

    cancelled = await owner_client.post(f"/api/v1/firmware/deployment-batches/{batch_id}/cancel")
    assert cancelled.status_code == 409, cancelled.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        command = await session.scalar(
            select(DeviceCommand).where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.command_type == "ota_install",
            )
        )
        assert deployment is not None and deployment.state == "queued"
        assert command is not None and command.state == "delivered"


@pytest.mark.asyncio
async def test_cancel_revalidates_a_download_transition_that_wins_after_preflight(
    owner_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="Cancellation race target"
    )
    _release_id, deployment_id = await _upload_and_deploy(
        owner_client, device_id=device_id, sequence=77
    )
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None and deployment.batch_id is not None
        batch_id = deployment.batch_id

    original_lock = firmware_routes.lock_active_ota_commands_for_deployments

    async def simulate_download_winning_after_preflight(
        session: AsyncSession,
        deployments: list[FirmwareDeployment] | tuple[FirmwareDeployment, ...],
    ) -> tuple[DeviceCommand, ...]:
        deployment = deployments[0]
        deployment.state = "downloading"
        deployment.progress_percent = 1
        await session.flush()
        return await original_lock(session, deployments)

    with monkeypatch.context() as patcher:
        patcher.setattr(
            firmware_routes,
            "lock_active_ota_commands_for_deployments",
            simulate_download_winning_after_preflight,
        )
        cancelled = await owner_client.post(
            f"/api/v1/firmware/deployment-batches/{batch_id}/cancel"
        )
    assert cancelled.status_code == 409, cancelled.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None and deployment.state == "queued"


@pytest.mark.asyncio
@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL row-lock concurrency regression",
)
async def test_postgresql_ota_graph_lock_serializes_command_and_lifecycle_mutation(
    owner_client: AsyncClient,
) -> None:
    device_id, _secret, _home_id = await _enroll_ota_target(
        owner_client, name="PostgreSQL OTA lock target"
    )
    _release_id, deployment_id = await _upload_and_deploy(
        owner_client,
        device_id=device_id,
        sequence=72,
    )
    first_locked = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_graph() -> None:
        async with session_factory() as session:
            preflight = await session.get(FirmwareDeployment, deployment_id)
            assert preflight is not None
            graph = await lock_firmware_ota_graph(session, (preflight,), lock_commands=True)
            first_locked.set()
            await release_first.wait()
            locked = next(row for row in graph.deployments if row.id == deployment_id)
            locked.state = "failed"
            locked.completed_at = datetime.now(UTC)
            for command in graph.commands:
                command.state = "failed"
            await recalculate_firmware_batch(session, locked.batch_id)
            await session.commit()

    async def contend_for_graph() -> str:
        async with session_factory() as session:
            preflight = await session.get(FirmwareDeployment, deployment_id)
            assert preflight is not None
            graph = await lock_firmware_ota_graph(session, (preflight,), lock_commands=True)
            locked = next(row for row in graph.deployments if row.id == deployment_id)
            locked_state = locked.state
            await session.rollback()
            return locked_state

    holder = asyncio.create_task(hold_graph())
    await asyncio.wait_for(first_locked.wait(), timeout=5)
    contender = asyncio.create_task(contend_for_graph())
    try:
        await asyncio.sleep(0.1)
        assert not contender.done()
    finally:
        release_first.set()
    await asyncio.wait_for(holder, timeout=5)
    assert await asyncio.wait_for(contender, timeout=5) == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("ingestion_kind", ("legacy", "stateless"))
@pytest.mark.parametrize("lifecycle_action", ("delete", "deploy"))
@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL row-lock concurrency regression",
)
async def test_postgresql_ingestion_and_lifecycle_routes_share_graph_then_device_order(
    owner_client: AsyncClient,
    ingestion_kind: str,
    lifecycle_action: str,
) -> None:
    device_id, secret, _home_id = await _enroll_ota_target(
        owner_client,
        name=f"{ingestion_kind} {lifecycle_action} lock target",
    )
    release_id, deployment_id = await _upload_and_deploy(
        owner_client,
        device_id=device_id,
        sequence=73,
    )
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        release = await session.get(FirmwareRelease, release_id)
        commands = list(
            (
                await session.scalars(
                    select(DeviceCommand).where(
                        DeviceCommand.command_type == "ota_install",
                        DeviceCommand.device_id == device_id,
                    )
                )
            ).all()
        )
        command = next(row for row in commands if row.payload.get("deployment_id") == deployment_id)
        assert deployment is not None and release is not None
        assert release.firmware_build_id is not None
        deployment.state = "validating"
        deployment.progress_percent = 90
        command.state = "succeeded"
        version = release.semantic_version
        build_id = release.firmware_build_id
        build_number = release.build_number
        digest = release.sha256
        await session.commit()

    start = asyncio.Event()

    async def ingest() -> Response:
        await start.wait()
        if ingestion_kind == "legacy":
            return await _heartbeat(
                owner_client,
                device_id=device_id,
                secret=secret,
                firmware_version=version,
                firmware_build_id=build_id,
            )
        return await _stateless_firmware_report(
            owner_client,
            device_id=device_id,
            secret=secret,
            firmware_version=version,
            firmware_build_id=build_id,
            sample_sequence=701,
        )

    async def mutate_lifecycle() -> Response:
        await start.wait()
        if lifecycle_action == "delete":
            return await owner_client.post(
                f"/api/v1/firmware/releases/{release_id}/delete-permanently",
                json={
                    "confirmation": "DELETE RELEASE PERMANENTLY",
                    "semantic_version": version,
                    "build_number": build_number,
                    "sha256": digest,
                },
            )
        return await owner_client.post(
            f"/api/v1/firmware/releases/{release_id}/deploy",
            json={"device_ids": [device_id], "rollout": "immediate"},
        )

    ingestion_task = asyncio.create_task(ingest())
    lifecycle_task = asyncio.create_task(mutate_lifecycle())
    start.set()
    ingestion_response, lifecycle_response = await asyncio.wait_for(
        asyncio.gather(ingestion_task, lifecycle_task),
        timeout=10,
    )
    assert ingestion_response.status_code == 200, ingestion_response.text
    assert lifecycle_response.status_code in {409, 422}, lifecycle_response.text
    async with session_factory() as session:
        deployment = await session.get(FirmwareDeployment, deployment_id)
        assert deployment is not None and deployment.state == "succeeded"


@pytest.mark.asyncio
@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="PostgreSQL row-lock concurrency regression",
)
async def test_postgresql_distinct_release_deletes_use_one_release_lock_order(
    owner_client: AsyncClient,
) -> None:
    releases: list[tuple[str, str, str, str, Path]] = []
    for sequence in (96, 97):
        version = f"7.{sequence}.0-rc.1"
        build_number = str(sequence * 100 + 1)
        image = _esp32s3_test_image(
            version,
            f"PowerMeter concurrent release delete {sequence}\0".encode() * 64,
        )
        digest = hashlib.sha256(image).hexdigest()
        uploaded = await owner_client.post(
            "/api/v1/firmware/releases",
            files={"image": ("firmware.bin", image, "application/octet-stream")},
            data={
                "semantic_version": version,
                "build_number": build_number,
                "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
                "minimum_boot_version": "1",
                "minimum_config_version": "1",
                "expected_sha256": digest,
                "release_notes": "Concurrent release deletion lock-order fixture.",
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        release_id = uploaded.json()["release"]["release_id"]
        async with session_factory() as session:
            release = await session.get(FirmwareRelease, release_id)
            assert release is not None
            artifact = Path(release.image_path)
        releases.append((release_id, version, build_number, digest, artifact))

    start = asyncio.Event()

    async def delete_release(release: tuple[str, str, str, str, Path]) -> Response:
        release_id, version, build_number, digest, _artifact = release
        await start.wait()
        return await owner_client.post(
            f"/api/v1/firmware/releases/{release_id}/delete-permanently",
            json={
                "confirmation": "DELETE RELEASE PERMANENTLY",
                "semantic_version": version,
                "build_number": build_number,
                "sha256": digest,
            },
        )

    tasks = [asyncio.create_task(delete_release(release)) for release in releases]
    start.set()
    responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    assert [response.status_code for response in responses] == [204, 204]
    assert all(not release[4].exists() for release in releases)
    async with session_factory() as session:
        deleted_states = (
            await session.scalars(
                select(FirmwareRelease.lifecycle_state)
                .where(FirmwareRelease.id.in_([release[0] for release in releases]))
                .order_by(FirmwareRelease.id)
            )
        ).all()
    assert deleted_states == ["deleted", "deleted"]
