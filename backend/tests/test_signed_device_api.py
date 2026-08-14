from __future__ import annotations

import base64
import secrets
import time
from datetime import UTC, datetime, timedelta

import orjson
import pytest
from backend.app.main import session_factory
from backend.app.models import Home
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)
from httpx import AsyncClient
from sqlalchemy import select


def _signed_headers(
    *, device_id: str, secret: bytes, path: str, body: bytes, nonce: str | None = None
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = nonce or secrets.token_urlsafe(24)
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


def _verify_response(response, *, device_id: str, secret: bytes, path: str) -> None:  # type: ignore[no-untyped-def]
    digest = body_sha256(response.content)
    assert response.headers["X-PM-Content-SHA256"] == digest
    canonical = canonical_request(
        "RESPONSE",
        path,
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


async def _enroll(owner_client: AsyncClient) -> tuple[str, bytes]:
    async with session_factory() as session:
        home_id = await session.scalar(select(Home.id))
    assert home_id is not None
    token_response = await owner_client.post(
        "/api/v1/enrollment-tokens",
        json={
            "home_id": home_id,
            "friendly_name": "Main panel",
            "ct_rating_a": "100",
            "pzem_variant": "pzem004t-v4-classic-candidate",
            "expires_minutes": 15,
        },
    )
    assert token_response.status_code == 201, token_response.text
    enrolled = await owner_client.post(
        "/api/v1/devices/enroll",
        json={
            "enrollment_token": token_response.json()["token"],
            "protocol_id": "pm-protocol/1.0.0",
            "firmware_version": "0.1.0-rc.1",
            "hardware_fingerprint": "esp32s3-test-fixture",
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json()["device_id"], base64.b64decode(enrolled.json()["device_secret"])


@pytest.mark.asyncio
async def test_signed_heartbeat_reading_history_retry_and_replay(
    owner_client: AsyncClient,
) -> None:
    device_id, secret = await _enroll(owner_client)
    now = datetime.now(UTC)
    heartbeat_body = orjson.dumps(
        {
            "protocol_id": "pm-protocol/1.0.0",
            "boot_id": "123e4567-e89b-12d3-a456-426614174000",
            "firmware_version": "0.1.0-rc.1",
            "measurement": {
                "measured_at": now.isoformat(),
                "monotonic_us": 120_000_000,
                "voltage_v": "122.6",
                "current_a": "2.0",
                "active_power_w": "245.2",
                "frequency_hz": "60.01",
                "power_factor": "0.99",
                "pzem_energy_wh": 12345,
                "pzem_status": "ok",
                "pzem_error_code": None,
            },
            "storage_status": "ok",
            "time_status": "trusted",
            "wifi_rssi": -55,
            "ip_address": "192.0.2.20",
            "backlog": 1,
            "oldest_sequence": 1,
            "newest_sequence": 1,
            "acknowledged_sequence": 0,
            "free_internal_heap": 200000,
            "largest_internal_block": 120000,
            "task_stack_watermarks": {"measurement": 2048},
            "reboot_reason": "power_on",
            "health_flags": [],
            "command_results": [],
        }
    )
    heartbeat_path = "/api/v1/device/heartbeat"
    heartbeat_headers = _signed_headers(
        device_id=device_id,
        secret=secret,
        path=heartbeat_path,
        body=heartbeat_body,
        nonce="heartbeat-replay-nonce-00000001",
    )
    heartbeat = await owner_client.post(
        heartbeat_path, content=heartbeat_body, headers=heartbeat_headers
    )
    assert heartbeat.status_code == 200, heartbeat.text
    _verify_response(heartbeat, device_id=device_id, secret=secret, path=heartbeat_path)
    replay = await owner_client.post(
        heartbeat_path, content=heartbeat_body, headers=heartbeat_headers
    )
    assert replay.status_code == 409
    assert replay.json()["code"] == "DEVICE_NONCE_REPLAY"

    start = now - timedelta(minutes=1)
    reading_body = orjson.dumps(
        {
            "protocol_id": "pm-protocol/1.0.0",
            "records": [
                {
                    "sequence": 1,
                    "reset_generation": 0,
                    "interval_start_utc": start.isoformat(),
                    "interval_end_utc": now.isoformat(),
                    "monotonic_start_us": 60_000_000,
                    "monotonic_end_us": 120_000_000,
                    "sample_count": 60,
                    "expected_sample_count": 60,
                    "voltage_mv": 122600,
                    "current_ma": 2000,
                    "active_power_mw": 245200,
                    "frequency_mhz": 60010,
                    "power_factor_milli": 990,
                    "pzem_energy_wh": 12345,
                    "interval_energy_mwh": 245200,
                    "energy_selection": "pzem_delta",
                    "pzem_status": "ok",
                    "time_trusted": True,
                    "flags": [],
                    "record_crc32": 123456,
                }
            ],
        }
    )
    reading_path = "/api/v1/device/readings"
    accepted = await owner_client.post(
        reading_path,
        content=reading_body,
        headers=_signed_headers(
            device_id=device_id, secret=secret, path=reading_path, body=reading_body
        ),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["highest_contiguous_sequence"] == 1
    _verify_response(accepted, device_id=device_id, secret=secret, path=reading_path)

    retry = await owner_client.post(
        reading_path,
        content=reading_body,
        headers=_signed_headers(
            device_id=device_id, secret=secret, path=reading_path, body=reading_body
        ),
    )
    assert retry.status_code == 200
    assert retry.json()["accepted"] == 0
    assert retry.json()["identical_retries"] == 1

    home = await owner_client.get("/api/v1/home")
    assert home.status_code == 200
    assert home.json()["devices"][0]["measurement"]["active_power_w"] == "245.200"
    history = await owner_client.get(
        "/api/v1/history",
        params={
            "from": (start - timedelta(seconds=1)).isoformat(),
            "to": (now + timedelta(seconds=1)).isoformat(),
            "metric": "energy",
            "device_id": device_id,
            "resolution_seconds": 60,
        },
    )
    assert history.status_code == 200, history.text
    assert history.json()["energy_kwh"] == "0.2452"
    assert [point["value"] for point in history.json()["points"] if point["value"] is not None] == [
        "0.2452"
    ]


@pytest.mark.asyncio
async def test_future_durable_timestamp_is_rejected(owner_client: AsyncClient) -> None:
    device_id, secret = await _enroll(owner_client)
    future = datetime.now(UTC) + timedelta(hours=1)
    body = orjson.dumps(
        {
            "protocol_id": "pm-protocol/1.0.0",
            "records": [
                {
                    "sequence": 1,
                    "reset_generation": 0,
                    "interval_start_utc": (future - timedelta(minutes=1)).isoformat(),
                    "interval_end_utc": future.isoformat(),
                    "monotonic_start_us": 1,
                    "monotonic_end_us": 60_000_001,
                    "sample_count": 60,
                    "expected_sample_count": 60,
                    "voltage_mv": 120000,
                    "current_ma": 1000,
                    "active_power_mw": 120000,
                    "frequency_mhz": 60000,
                    "power_factor_milli": 1000,
                    "pzem_energy_wh": 1,
                    "interval_energy_mwh": 120000,
                    "energy_selection": "pzem_delta",
                    "pzem_status": "ok",
                    "time_trusted": True,
                    "flags": [],
                    "record_crc32": 1,
                }
            ],
        }
    )
    path = "/api/v1/device/readings"
    response = await owner_client.post(
        path,
        content=body,
        headers=_signed_headers(device_id=device_id, secret=secret, path=path, body=body),
    )
    assert response.status_code == 409
