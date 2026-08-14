from __future__ import annotations

import base64
import os
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
import orjson
import pytest
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)

LIVE_API_URL = os.getenv("PM_LIVE_API_URL")
RESET_LIVE_DATABASE = os.getenv("PM_LIVE_RESET_DATABASE") == "1"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not LIVE_API_URL, reason="PM_LIVE_API_URL is not configured"),
]


def _signed_headers(*, device_id: str, secret: bytes, path: str, body: bytes) -> dict[str, str]:
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


def _verify_device_response(
    response: httpx.Response, *, device_id: str, secret: bytes, path: str
) -> None:
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


def test_live_postgres_bootstrap_signed_ingestion_retry_and_history() -> None:
    assert LIVE_API_URL is not None
    with httpx.Client(base_url=LIVE_API_URL, timeout=20, trust_env=False) as client:
        status = client.get("/api/v1/auth/bootstrap/status")
        assert status.status_code == 200
        if status.json() != {"required": True}:
            if RESET_LIVE_DATABASE:
                pytest.fail("PM_LIVE_RESET_DATABASE=1 but the test database is not empty")
            pytest.skip("live database was not explicitly reset for the destructive probe")
        bootstrap = client.post(
            "/api/v1/auth/bootstrap",
            json={
                "email": "postgres-probe@example.com",
                "display_name": "PostgreSQL Probe Owner",
                "password": "integration-only correct horse 2026!",
                "home_name": "PostgreSQL Probe Home",
                "timezone": "America/Los_Angeles",
            },
        )
        assert bootstrap.status_code == 201, bootstrap.text
        session_cookie = bootstrap.cookies.get("pm_session")
        csrf = bootstrap.cookies.get("pm_csrf")
        assert session_cookie and csrf
        cookie = f"pm_session={session_cookie}; pm_csrf={csrf}"
        get_headers = {"Cookie": cookie}
        post_headers = {"Cookie": cookie, "X-CSRF-Token": csrf}

        devices = client.get("/api/v1/devices", headers=get_headers)
        assert devices.status_code == 200 and devices.json()["devices"] == []
        home = client.get("/api/v1/settings/home-utility", headers=get_headers)
        assert home.status_code == 200, home.text
        home_id = home.json()["home"]["id"]

        enrollment_token = client.post(
            "/api/v1/enrollment-tokens",
            headers=post_headers,
            json={
                "home_id": home_id,
                "friendly_name": "PostgreSQL Main Panel",
                "ct_rating_a": "100",
                "pzem_variant": "pzem004t-v4-classic-candidate",
                "expires_minutes": 15,
            },
        )
        assert enrollment_token.status_code == 201, enrollment_token.text
        enrolled = client.post(
            "/api/v1/devices/enroll",
            json={
                "enrollment_token": enrollment_token.json()["token"],
                "protocol_id": "pm-protocol/1.0.0",
                "firmware_version": "0.1.0-rc.1",
                "hardware_fingerprint": "postgres-live-probe",
            },
        )
        assert enrolled.status_code == 201, enrolled.text
        device_id = enrolled.json()["device_id"]
        secret = base64.b64decode(enrolled.json()["device_secret"])

        measured_at = datetime.now(UTC)
        heartbeat_body = orjson.dumps(
            {
                "protocol_id": "pm-protocol/1.0.0",
                "boot_id": "123e4567-e89b-12d3-a456-426614174000",
                "firmware_version": "0.1.0-rc.1",
                "measurement": {
                    "measured_at": measured_at.isoformat(),
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
            device_id=device_id, secret=secret, path=heartbeat_path, body=heartbeat_body
        )
        heartbeat = client.post(heartbeat_path, content=heartbeat_body, headers=heartbeat_headers)
        assert heartbeat.status_code == 200, heartbeat.text
        _verify_device_response(heartbeat, device_id=device_id, secret=secret, path=heartbeat_path)
        replay = client.post(heartbeat_path, content=heartbeat_body, headers=heartbeat_headers)
        assert replay.status_code == 409 and replay.json()["code"] == "DEVICE_NONCE_REPLAY"

        interval_start = measured_at - timedelta(minutes=1)
        reading_body = orjson.dumps(
            {
                "protocol_id": "pm-protocol/1.0.0",
                "records": [
                    {
                        "sequence": 1,
                        "reset_generation": 0,
                        "interval_start_utc": interval_start.isoformat(),
                        "interval_end_utc": measured_at.isoformat(),
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
        accepted = client.post(
            reading_path,
            content=reading_body,
            headers=_signed_headers(
                device_id=device_id, secret=secret, path=reading_path, body=reading_body
            ),
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["highest_contiguous_sequence"] == 1
        _verify_device_response(accepted, device_id=device_id, secret=secret, path=reading_path)
        retry = client.post(
            reading_path,
            content=reading_body,
            headers=_signed_headers(
                device_id=device_id, secret=secret, path=reading_path, body=reading_body
            ),
        )
        assert retry.status_code == 200
        assert retry.json()["accepted"] == 0
        assert retry.json()["identical_retries"] == 1

        dashboard = client.get("/api/v1/home", headers=get_headers)
        assert dashboard.status_code == 200, dashboard.text
        assert dashboard.json()["devices"][0]["measurement"]["active_power_w"] == "245.200"
        assert (
            dashboard.json()["disclosure"]["usage_source"]
            == "authenticated PZEM-004T sensor intervals only"
        )
        history = client.get(
            "/api/v1/history",
            headers=get_headers,
            params={
                "from": (interval_start - timedelta(seconds=1)).isoformat(),
                "to": (measured_at + timedelta(seconds=1)).isoformat(),
                "metric": "energy",
                "device_id": device_id,
                "resolution_seconds": 60,
            },
        )
        assert history.status_code == 200, history.text
        assert history.json()["energy_kwh"] == "0.2452"
        assert history.json()["usage_source"] == "authenticated PZEM-004T sensor intervals only"
