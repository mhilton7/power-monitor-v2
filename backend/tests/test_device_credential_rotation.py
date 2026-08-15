from __future__ import annotations

import base64
import secrets
import time

import orjson
import pytest
from backend.app.main import session_factory
from backend.app.models import DeviceCredential, Home
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)
from httpx import AsyncClient, Response
from sqlalchemy import select


def _headers(*, device_id: str, secret: bytes, path: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    digest = body_sha256(body)
    canonical = canonical_request("POST", path, "", timestamp, nonce, digest)
    return {
        "X-PM-Protocol": "pm-protocol/1.0.0",
        "X-PM-Device-ID": device_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Nonce": nonce,
        "X-PM-Content-SHA256": digest,
        "X-PM-Signature": sign_request(
            derive_directional_key(secret, device_id, "device-to-server"), canonical
        ),
        "Content-Type": "application/json",
    }


def _heartbeat_body(results: list[dict[str, object]] | None = None) -> bytes:
    return orjson.dumps(
        {
            "protocol_id": "pm-protocol/1.0.0",
            "boot_id": "123e4567-e89b-12d3-a456-426614174000",
            "firmware_version": "0.1.0-rc.1",
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
            "command_results": results or [],
        }
    )


async def _enroll(owner_client: AsyncClient) -> tuple[str, bytes]:
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
    assert home_id is not None
    token = await owner_client.post(
        "/api/v1/enrollment-tokens",
        json={
            "home_id": home_id,
            "friendly_name": "Rotation target",
            "ct_rating_a": "100",
            "pzem_variant": "pzem004t-v4-classic-candidate",
            "expires_minutes": 15,
        },
    )
    enrolled = await owner_client.post(
        "/api/v1/devices/enroll",
        json={
            "enrollment_token": token.json()["token"],
            "protocol_id": "pm-protocol/1.0.0",
            "firmware_version": "0.1.0-rc.1",
            "hardware_fingerprint": "credential-rotation-fixture",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json()["device_id"], base64.b64decode(enrolled.json()["device_secret"])


async def _heartbeat(
    client: AsyncClient,
    *,
    device_id: str,
    secret: bytes,
    results: list[dict[str, object]] | None = None,
) -> Response:
    path = "/api/v1/device/heartbeat"
    body = _heartbeat_body(results)
    return await client.post(
        path,
        content=body,
        headers=_headers(device_id=device_id, secret=secret, path=path, body=body),
    )


def _verify_response(response: Response, *, device_id: str, secret: bytes) -> None:
    digest = body_sha256(response.content)
    canonical = canonical_request(
        "RESPONSE",
        "/api/v1/device/heartbeat",
        "",
        response.headers["X-PM-Timestamp"],
        response.headers["X-PM-Nonce"],
        digest,
    )
    assert verify_signature(
        derive_directional_key(secret, device_id, "server-to-device"),
        canonical,
        response.headers["X-PM-Signature"],
    )


@pytest.mark.asyncio
async def test_rotation_never_exposes_secret_to_browser_and_requires_new_key_commit(
    owner_client: AsyncClient,
) -> None:
    device_id, old_secret = await _enroll(owner_client)

    generic = await owner_client.post(
        f"/api/v1/devices/{device_id}/commands",
        json={
            "command_type": "rotate_device_credentials",
            "idempotency_key": "browser-must-not-supply-secret",
            "payload": {"device_secret_hex": "00" * 32},
        },
    )
    assert generic.status_code == 422

    requested = await owner_client.post(
        f"/api/v1/devices/{device_id}/credentials/rotate",
        json={
            "idempotency_key": "credential-rotation-prepare-001",
            "typed_confirmation": "ROTATE SENSOR CREDENTIALS",
        },
    )
    assert requested.status_code == 202, requested.text
    public_rotation = requested.json()["rotation"]
    assert "secret" not in requested.text.lower()
    assert len(public_rotation["credential_fingerprint"]) == 64

    delivered = await _heartbeat(owner_client, device_id=device_id, secret=old_secret)
    assert delivered.status_code == 200, delivered.text
    _verify_response(delivered, device_id=device_id, secret=old_secret)
    prepare = next(
        command
        for command in delivered.json()["commands"]
        if command["command_type"] == "rotate_device_credentials"
    )
    assert set(prepare["payload"]) == {
        "schema",
        "rotation_id",
        "device_secret_hex",
        "credential_fingerprint",
        "overlap_expires_at",
    }
    candidate_secret = bytes.fromhex(prepare["payload"]["device_secret_hex"])
    assert prepare["payload"]["credential_fingerprint"] == public_rotation["credential_fingerprint"]

    premature = await _heartbeat(owner_client, device_id=device_id, secret=candidate_secret)
    assert premature.status_code == 401

    prepared = await _heartbeat(
        owner_client,
        device_id=device_id,
        secret=old_secret,
        results=[
            {
                "command_id": prepare["command_id"],
                "state": "succeeded",
                "progress_percent": 100,
                "result_code": "CREDENTIAL_ROTATION_PREPARED",
                "evidence": {
                    "rotation_id": public_rotation["rotation_id"],
                    "credential_fingerprint": public_rotation["credential_fingerprint"],
                    "ready": True,
                },
            }
        ],
    )
    assert prepared.status_code == 200, prepared.text
    _verify_response(prepared, device_id=device_id, secret=old_secret)
    commit = next(
        command
        for command in prepared.json()["commands"]
        if command["command_type"] == "rotate_device_credentials"
        and set(command["payload"])
        == {
            "schema",
            "rotation_id",
            "credential_fingerprint",
        }
    )

    activated = await _heartbeat(
        owner_client,
        device_id=device_id,
        secret=candidate_secret,
        results=[
            {
                "command_id": commit["command_id"],
                "state": "succeeded",
                "progress_percent": 100,
                "result_code": "CREDENTIAL_ROTATION_ACTIVATED",
                "evidence": {
                    "rotation_id": public_rotation["rotation_id"],
                    "credential_fingerprint": public_rotation["credential_fingerprint"],
                    "activated": True,
                },
            }
        ],
    )
    assert activated.status_code == 200, activated.text
    _verify_response(activated, device_id=device_id, secret=candidate_secret)

    old_rejected = await _heartbeat(owner_client, device_id=device_id, secret=old_secret)
    assert old_rejected.status_code == 401
    new_accepted = await _heartbeat(owner_client, device_id=device_id, secret=candidate_secret)
    assert new_accepted.status_code == 200

    async with session_factory() as session:
        credentials = (
            await session.scalars(
                select(DeviceCredential)
                .where(DeviceCredential.device_id == device_id)
                .order_by(DeviceCredential.key_version)
            )
        ).all()
    assert [credential.state for credential in credentials] == ["revoked", "active"]
    assert credentials[0].revoked_at is not None
    assert credentials[1].activated_at is not None
    assert old_secret not in credentials[0].encrypted_secret
    assert candidate_secret not in credentials[1].encrypted_secret


@pytest.mark.asyncio
async def test_rotation_cancel_is_bound_to_old_key_and_zeroizes_candidate(
    owner_client: AsyncClient,
) -> None:
    device_id, old_secret = await _enroll(owner_client)
    requested = await owner_client.post(
        f"/api/v1/devices/{device_id}/credentials/rotate",
        json={
            "idempotency_key": "credential-rotation-cancel-prepare",
            "typed_confirmation": "ROTATE SENSOR CREDENTIALS",
        },
    )
    rotation = requested.json()["rotation"]
    delivered = await _heartbeat(owner_client, device_id=device_id, secret=old_secret)
    prepare = delivered.json()["commands"][0]
    candidate_secret = bytes.fromhex(prepare["payload"]["device_secret_hex"])

    cancellation = await owner_client.post(
        f"/api/v1/devices/{device_id}/credentials/rotations/{rotation['rotation_id']}/cancel",
        json={"idempotency_key": "credential-rotation-cancel-001"},
    )
    assert cancellation.status_code == 202, cancellation.text
    cancel_delivery = await _heartbeat(owner_client, device_id=device_id, secret=old_secret)
    cancel_command = next(
        command
        for command in cancel_delivery.json()["commands"]
        if command["payload"].get("cancelled") is True
    )
    cancelled = await _heartbeat(
        owner_client,
        device_id=device_id,
        secret=old_secret,
        results=[
            {
                "command_id": cancel_command["command_id"],
                "state": "succeeded",
                "progress_percent": 100,
                "result_code": "CREDENTIAL_ROTATION_CANCELLED",
                "evidence": {"rotation_id": rotation["rotation_id"], "cancelled": True},
            }
        ],
    )
    assert cancelled.status_code == 200, cancelled.text
    rejected = await _heartbeat(owner_client, device_id=device_id, secret=candidate_secret)
    assert rejected.status_code == 401
