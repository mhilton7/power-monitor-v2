from __future__ import annotations

import argparse
import base64
import io
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import orjson
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
    verify_signature,
)
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

RATE_ONLY_SCHEDULE = """
Rate plan: TOU-D-4-9PM
Base Services Charge $0.79 per day
Baseline Credit $0.10/kWh
All All Off-Peak 00:00-16:00 $0.34/kWh
All All On-Peak 16:00-21:00 $0.58/kWh
All All Off-Peak 21:00-24:00 $0.34/kWh
""".strip()


def _require(response: httpx.Response, status_code: int) -> dict[str, Any]:
    if response.status_code != status_code:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path} returned "
            f"{response.status_code}: {response.text[:1000]}"
        )
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{response.request.url.path} did not return a JSON object")
    return cast(dict[str, Any], value)


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
    if response.headers.get("X-PM-Content-SHA256") != digest:
        raise RuntimeError("device response content digest was not authenticated")
    canonical = canonical_request(
        "RESPONSE",
        path,
        "",
        response.headers["X-PM-Timestamp"],
        response.headers["X-PM-Nonce"],
        digest,
    )
    if not verify_signature(
        derive_directional_key(secret, device_id, "server-to-device"),
        canonical,
        response.headers["X-PM-Signature"],
    ):
        raise RuntimeError("device response signature was invalid")


def _heartbeat_body(
    *, measured_at: datetime, acknowledged_sequence: int, command_results: list[dict[str, Any]]
) -> bytes:
    return orjson.dumps(
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
            "backlog": 1 if acknowledged_sequence == 0 else 0,
            "oldest_sequence": 1 if acknowledged_sequence == 0 else None,
            "newest_sequence": 1,
            "acknowledged_sequence": acknowledged_sequence,
            "free_internal_heap": 200000,
            "largest_internal_block": 120000,
            "task_stack_watermarks": {"measurement": 2048},
            "reboot_reason": "power_on",
            "health_flags": [],
            "command_results": command_results,
        }
    )


def _post_heartbeat(
    client: httpx.Client,
    *,
    device_id: str,
    secret: bytes,
    body: bytes,
) -> dict[str, Any]:
    path = "/api/v1/device/heartbeat"
    response = client.post(
        path,
        content=body,
        headers=_signed_headers(device_id=device_id, secret=secret, path=path, body=body),
    )
    value = _require(response, 200)
    _verify_device_response(response, device_id=device_id, secret=secret, path=path)
    return value


def _rate_source_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), pageCompression=0)
    text = document.beginText(54, 744)
    text.setFont("Helvetica", 9)
    for line in RATE_ONLY_SCHEDULE.splitlines():
        text.textLine(line)
    document.drawText(text)
    document.save()
    return output.getvalue()


def run_probe(*, base_url: str, ca_file: Path, email: str, password: str, output: Path) -> None:
    timeout = httpx.Timeout(90)
    with (
        httpx.Client(
            base_url=base_url,
            verify=str(ca_file),
            timeout=timeout,
            trust_env=False,
        ) as browser,
        httpx.Client(
            base_url=base_url,
            verify=str(ca_file),
            timeout=timeout,
            trust_env=False,
        ) as device_client,
    ):
        login = _require(
            browser.post("/api/v1/auth/login", json={"email": email, "password": password}),
            200,
        )
        if login.get("user", {}).get("email") != email:
            raise RuntimeError("authenticated owner identity did not match the smoke principal")
        csrf = browser.cookies.get("pm_csrf")
        if not csrf:
            raise RuntimeError("authenticated session did not issue a CSRF token")
        browser.headers["X-CSRF-Token"] = csrf

        settings = _require(browser.get("/api/v1/settings/home-utility"), 200)
        home = cast(dict[str, Any], settings["home"])
        utility = cast(dict[str, Any], settings["utility"])
        token = _require(
            browser.post(
                "/api/v1/enrollment-tokens",
                json={
                    "home_id": home["id"],
                    "friendly_name": "Release Evidence Main Panel",
                    "ct_rating_a": "100",
                    "pzem_variant": "pzem004t-v4-classic-candidate",
                    "expires_minutes": 15,
                },
            ),
            201,
        )
        enrolled = _require(
            device_client.post(
                "/api/v1/devices/enroll",
                json={
                    "enrollment_token": token["token"],
                    "protocol_id": "pm-protocol/1.0.0",
                    "firmware_version": "0.1.0-rc.1",
                    "hardware_fingerprint": "release-deployment-evidence-probe",
                },
            ),
            201,
        )
        device_id = str(enrolled["device_id"])
        secret = base64.b64decode(str(enrolled["device_secret"]), validate=True)

        measured_at = datetime.now(UTC)
        _post_heartbeat(
            device_client,
            device_id=device_id,
            secret=secret,
            body=_heartbeat_body(
                measured_at=measured_at,
                acknowledged_sequence=0,
                command_results=[],
            ),
        )
        interval_start = measured_at - timedelta(minutes=1)
        reading_path = "/api/v1/device/readings"
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
        reading_response = device_client.post(
            reading_path,
            content=reading_body,
            headers=_signed_headers(
                device_id=device_id,
                secret=secret,
                path=reading_path,
                body=reading_body,
            ),
        )
        reading = _require(reading_response, 200)
        _verify_device_response(
            reading_response,
            device_id=device_id,
            secret=secret,
            path=reading_path,
        )
        if reading.get("accepted") != 1 or reading.get("highest_contiguous_sequence") != 1:
            raise RuntimeError("authenticated sensor interval was not durably accepted")

        history_parameters: dict[str, str | int] = {
            "from": (interval_start - timedelta(seconds=1)).isoformat(),
            "to": (measured_at + timedelta(seconds=1)).isoformat(),
            "device_id": device_id,
            "resolution_seconds": 60,
        }
        history = _require(
            browser.get("/api/v1/history", params={**history_parameters, "metric": "energy"}),
            200,
        )
        if history.get("energy_kwh") != "0.2452":
            raise RuntimeError("History did not reflect the authenticated PZEM interval")
        usage_source = history.get("usage_source")
        if usage_source != "authenticated PZEM-004T sensor intervals only":
            raise RuntimeError("History reported a non-PZEM usage source")
        dashboard = _require(browser.get("/api/v1/home"), 200)
        devices = cast(list[dict[str, Any]], dashboard.get("devices", []))
        if not devices or devices[0].get("measurement", {}).get("active_power_w") != "245.200":
            raise RuntimeError("dashboard live power did not originate from the signed heartbeat")

        imported = _require(
            browser.post(
                "/api/v1/bill-rate-imports",
                files={
                    "document": (
                        "release-smoke-rate-source.pdf",
                        _rate_source_pdf(),
                        "application/pdf",
                    )
                },
            ),
            201,
        )
        extraction = cast(dict[str, Any], imported["extraction"])
        if extraction.get("state") != "review_required":
            raise RuntimeError("rate-only PDF did not enter mandatory review")
        if imported.get("ignored_prohibited_categories") != []:
            raise RuntimeError("synthetic rate source unexpectedly contained prohibited bill data")
        published = _require(
            browser.post(
                f"/api/v1/bill-rate-imports/{extraction['id']}/publish",
                json={
                    "effective_start": (interval_start - timedelta(days=1)).isoformat(),
                    "effective_end": None,
                    "administrator_confirmed_effective_date": True,
                    "assign_to_utility_account_id": utility["id"],
                },
            ),
            201,
        )
        rate = cast(dict[str, Any], published["rate_plan_version"])

        cost_history: dict[str, Any] | None = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            candidate = _require(
                browser.get("/api/v1/history", params={**history_parameters, "metric": "cost"}),
                200,
            )
            if candidate.get("cost") is not None:
                cost_history = candidate
                break
            time.sleep(3)
        if cost_history is None:
            raise RuntimeError(
                "worker did not price the authenticated interval before the deadline"
            )
        cost = Decimal(str(cost_history["cost"]))
        if cost <= 0 or cost_history.get("usage_source") != usage_source:
            raise RuntimeError("priced History evidence was not positive PZEM-derived cost")

        queued = _require(
            browser.post(
                f"/api/v1/devices/{device_id}/commands",
                json={
                    "command_type": "reboot",
                    "idempotency_key": "release-evidence-reboot-v1",
                    "payload": {},
                },
            ),
            202,
        )
        command_id = str(cast(dict[str, Any], queued["command"])["id"])
        delivered = _post_heartbeat(
            device_client,
            device_id=device_id,
            secret=secret,
            body=_heartbeat_body(
                measured_at=datetime.now(UTC),
                acknowledged_sequence=1,
                command_results=[],
            ),
        )
        commands = cast(list[dict[str, Any]], delivered.get("commands", []))
        if not any(
            command.get("command_id") == command_id and command.get("command_type") == "reboot"
            for command in commands
        ):
            raise RuntimeError("queued reboot was not delivered on the authenticated channel")
        _post_heartbeat(
            device_client,
            device_id=device_id,
            secret=secret,
            body=_heartbeat_body(
                measured_at=datetime.now(UTC),
                acknowledged_sequence=1,
                command_results=[
                    {
                        "command_id": command_id,
                        "state": "succeeded",
                        "progress_percent": 100,
                        "result_code": "REBOOT_COMPLETED",
                        "evidence": {"rebooted": True},
                    }
                ],
            ),
        )
        device_listing = _require(browser.get("/api/v1/devices"), 200)
        listed_devices = cast(list[dict[str, Any]], device_listing.get("devices", []))
        last_command = cast(dict[str, Any], listed_devices[0].get("last_command", {}))
        if last_command.get("id") != command_id or last_command.get("state") != "succeeded":
            raise RuntimeError("authenticated command result was not persisted")

        evidence = {
            "schema": "pm-deployment-authenticated-evidence/1.0.0",
            "status": "passed",
            "protocol": "pm-protocol/1.0.0",
            "device_id": device_id,
            "enrollment": "authenticated",
            "heartbeat": "authenticated_pzem",
            "reading_sequence": 1,
            "energy_kwh": history["energy_kwh"],
            "usage_source": usage_source,
            "rate_source": "reviewed_rate_only_pdf",
            "rate_source_sha256": rate["source_artifact_sha256"],
            "cost": str(cost),
            "cost_rule": cost_history["aggregation"]["cost"],
            "command": {
                "id": command_id,
                "type": "reboot",
                "delivery": "authenticated",
                "state": "succeeded",
                "result_code": "REBOOT_COMPLETED",
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, separators=(",", ":"), sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--ca-file", required=True, type=Path)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    run_probe(
        base_url=arguments.base_url,
        ca_file=arguments.ca_file,
        email=arguments.email,
        password=arguments.password,
        output=arguments.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
