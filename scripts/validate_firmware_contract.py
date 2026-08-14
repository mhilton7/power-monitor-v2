#!/usr/bin/env python3
"""Fail closed unless immutable firmware fixtures match server wire contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

PROTOCOL = "pm-protocol/1.0.0"
CONTRACT_ID = "pm-server-device-contract/1.0.0"
FIRMWARE_REPOSITORY = "https://github.com/mhilton7/power-monitor-sensor-headless"
SERVER_REPOSITORY = "https://github.com/mhilton7/power-monitor-v2"
os.environ.setdefault("PM_ENV", "test")
REQUESTS = {
    "enrollment": {
        "method": "POST",
        "path": "/api/v1/devices/enroll",
        "authentication": "verified_tls_one_time_token",
        "fixture": "test/vectors/device-enrollment.json",
        "schema": "test/contracts/device-enrollment.schema.json",
    },
    "heartbeat": {
        "method": "POST",
        "path": "/api/v1/device/heartbeat",
        "authentication": "pm-hmac-sha256-v1",
        "fixture": "test/vectors/device-heartbeat.json",
        "schema": "test/contracts/device-heartbeat.schema.json",
    },
    "reading_batch": {
        "method": "POST",
        "path": "/api/v1/device/readings",
        "authentication": "pm-hmac-sha256-v1",
        "fixture": "test/vectors/device-reading-batch.json",
        "schema": "test/contracts/device-reading-batch.schema.json",
    },
    "permanent_loss": {
        "method": "POST",
        "path": "/api/v1/device/permanent-loss",
        "authentication": "pm-hmac-sha256-v1",
        "fixture": "test/vectors/device-permanent-loss.json",
        "schema": "test/contracts/device-permanent-loss.schema.json",
    },
}
DOWNLOADS: list[dict[str, str | int]] = [
    {
        "name": "firmware_image",
        "method": "GET",
        "path_template": "/api/v1/device/firmware/{release_id}",
        "request_authentication": "pm-hmac-sha256-v1-empty-body",
        "response_authentication": "pm-hmac-sha256-v1-full-body",
        "range_policy": "safe_restart_without_range",
    }
]
COMMANDS: list[dict[str, str | int]] = [
    {
        "name": "ota_install",
        "fixture": "test/vectors/device-ota-command.json",
        "schema": "test/contracts/device-ota-command.schema.json",
        "manifest_authentication": "per-device-server-to-device-hmac-sha256-base64",
        "canonical_prefix": "PM-OTA-MANIFEST-V1",
    },
    {
        "name": "destructive_commands",
        "fixture": "test/vectors/device-destructive-commands.json",
        "schema": "test/contracts/device-destructive-commands.schema.json",
        "confirmation_token": "server_generated_16_byte_lowercase_hex",
        "prepare_expiry_seconds": 600,
        "reboot_policy": "persist_then_explicitly_invalidate_and_erase",
    },
    {
        "name": "credential_rotation",
        "fixture": "test/vectors/device-credential-rotation.json",
        "schema": "test/contracts/device-credential-rotation.schema.json",
        "payload_schema": "pm-credential-rotation/1.0.0",
        "candidate_fingerprint": "sha256_lowercase_hex",
        "prepare_result_authentication": "old_device_to_server",
        "commit_result_authentication": "new_device_to_server",
        "expiry_policy": "absolute_rfc3339_dormant_without_trusted_utc",
        "reboot_policy": "durable_resume_until_authenticated_result_ack",
    },
]
SHARED_FILES = {
    "device-heartbeat.schema.json": "shared/schemas/device-heartbeat.schema.json",
    "device-reading-batch.schema.json": "shared/schemas/device-reading-batch.schema.json",
    "device-permanent-loss.schema.json": "shared/schemas/device-permanent-loss.schema.json",
    "server-device-response.schema.json": "shared/schemas/server-device-response.schema.json",
    "power-meter-v2.openapi.json": "shared/openapi/power-meter-v2.openapi.json",
}


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def confined_file(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError(f"contract path must be relative: {relative}")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"contract path escapes firmware checkout: {relative}")
    return path


def without_titles(value: Any) -> Any:
    """Remove JSON Schema annotations before comparing the enrollment snapshot."""
    if isinstance(value, dict):
        return {key: without_titles(item) for key, item in value.items() if key != "title"}
    if isinstance(value, list):
        return [without_titles(item) for item in value]
    return value


def validate_shared_snapshots(server: Path, firmware: Path, contract: dict[str, Any]) -> None:
    sys.path.insert(0, str(server))
    from scripts.generate_contracts import generated_files

    for path, expected_bytes in generated_files().items():
        if path.read_bytes() != expected_bytes:
            relative = path.relative_to(server)
            raise ValueError(
                f"generated server contract is stale: {relative}; run scripts/generate_contracts.py"
            )
    declared = contract.get("shared_contract_sha256")
    if not isinstance(declared, dict) or set(declared) != set(SHARED_FILES):
        raise ValueError("contract must hash every exact shared server contract")
    for name, relative in SHARED_FILES.items():
        server_path = (server / relative).resolve(strict=True)
        digest = sha256(server_path)
        if declared[name] != digest:
            raise ValueError(f"declared shared contract hash is stale: {name}")
        if name.endswith(".schema.json"):
            snapshot = confined_file(firmware, f"test/contracts/{name}")
            if sha256(snapshot) != digest:
                raise ValueError(f"firmware schema snapshot differs byte-for-byte: {name}")


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("command timestamp must include a UTC offset")
    return parsed


def validate_commands(server: Path, firmware: Path, contract: dict[str, Any]) -> None:
    if contract.get("downloads") != DOWNLOADS:
        raise ValueError("firmware download declaration differs from the server contract")
    if contract.get("commands") != COMMANDS:
        raise ValueError("firmware command declarations differ from the server contract")

    sys.path.insert(0, str(server))
    from backend.app.routes.firmware import ota_manifest_canonical
    from backend.app.security.protocol import derive_directional_key, sign_request
    from backend.app.services.commands import (
        COMMAND_CAPABILITIES,
        COMMAND_EXPIRY_SECONDS,
        COMMIT_CONFIRMATION_PHRASES,
        ROTATION_SCHEMA,
    )

    format_checker = FormatChecker()
    fixtures: dict[str, dict[str, Any]] = {}
    for declaration in COMMANDS:
        schema = load_object(confined_file(firmware, declaration["schema"]))
        Draft202012Validator.check_schema(schema)
        fixture = load_object(confined_file(firmware, declaration["fixture"]))
        Draft202012Validator(schema, format_checker=format_checker).validate(fixture)
        fixtures[declaration["name"]] = fixture

    response_schema = load_object(server / SHARED_FILES["server-device-response.schema.json"])
    heartbeat_schema = load_object(server / SHARED_FILES["device-heartbeat.schema.json"])
    envelope_validator = Draft202012Validator(
        response_schema["$defs"]["CommandEnvelope"], format_checker=format_checker
    )
    result_validator = Draft202012Validator(
        heartbeat_schema["$defs"]["CommandResult"], format_checker=format_checker
    )

    ota = fixtures["ota_install"]
    envelope_validator.validate(ota)
    if ota.get("command_type") != "ota_install":
        raise ValueError("OTA fixture is not an ota_install command")
    if ota.get("required_firmware_capability") != COMMAND_CAPABILITIES["ota_install"]:
        raise ValueError("OTA capability differs from the live server mapping")
    if int((_utc(ota["expires_at"]) - _utc(ota["not_before"])).total_seconds()) != int(
        COMMAND_EXPIRY_SECONDS["ota_install"]
    ):
        raise ValueError("OTA command lifetime differs from the live server mapping")
    payload = ota.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("OTA fixture payload is invalid")
    unsigned = {name: value for name, value in payload.items() if name != "signature"}
    canonical = ota_manifest_canonical(unsigned)
    if hashlib.sha256(canonical).hexdigest() != (
        "776085e83a14c0ecc89ee3170712fed6f841d5c06ba8e948ff702b3ec46d6469"
    ):
        raise ValueError("OTA canonical bytes differ from the locked contract")
    key = derive_directional_key(bytes(range(32)), payload["device_id"], "server-to-device")
    if sign_request(key, canonical) != payload.get("signature"):
        raise ValueError("OTA vector signature differs from the live server implementation")
    if payload.get("download_path") != f"/api/v1/device/firmware/{payload.get('release_id')}":
        raise ValueError("OTA download path is not same-origin and release-bound")

    destructive = fixtures["destructive_commands"]
    if destructive.get("contract_id") != "pm-destructive-command-contract/1.0.0":
        raise ValueError("destructive command contract identifier differs")
    if destructive.get("prepare_expiry_seconds") != 600:
        raise ValueError("destructive prepare expiry differs")
    commands = destructive.get("commands")
    results = destructive.get("results")
    if not isinstance(commands, list) or not isinstance(results, list):
        raise ValueError("destructive commands/results must be arrays")
    for command in commands:
        envelope_validator.validate(command)
    for result in results:
        result_validator.validate(result)
    by_type = {command["command_type"]: command for command in commands}
    expected_types = {
        "format_storage_prepare",
        "format_storage_commit",
        "data_reset_prepare",
        "data_reset_commit",
        "data_reset_cancel",
    }
    if set(by_type) != expected_types or len(by_type) != len(commands):
        raise ValueError("destructive fixture must contain every command exactly once")
    token_pattern = re.compile(r"^[0-9a-f]{32}$")
    for command_type, command in by_type.items():
        if command.get("required_firmware_capability") != COMMAND_CAPABILITIES[command_type]:
            raise ValueError(f"capability differs from live server: {command_type}")
        lifetime = int((_utc(command["expires_at"]) - _utc(command["not_before"])).total_seconds())
        if command_type.endswith("_prepare") and lifetime != COMMAND_EXPIRY_SECONDS[command_type]:
            raise ValueError(f"command lifetime differs from live server: {command_type}")
        if not command_type.endswith("_prepare") and not (
            0 < lifetime <= COMMAND_EXPIRY_SECONDS[command_type]
        ):
            raise ValueError(f"linked command lifetime exceeds its prepare: {command_type}")
    format_prepare = by_type["format_storage_prepare"]
    format_commit = by_type["format_storage_commit"]
    reset_prepare = by_type["data_reset_prepare"]
    reset_commit = by_type["data_reset_commit"]
    reset_cancel = by_type["data_reset_cancel"]
    if set(format_prepare["payload"]) != {"confirmation_token"} or not token_pattern.fullmatch(
        format_prepare["payload"]["confirmation_token"]
    ):
        raise ValueError("storage format prepare payload differs")
    if set(format_commit["payload"]) != {"prepare_command_id", "confirmation_token"}:
        raise ValueError("storage format commit payload differs")
    if format_commit["payload"]["prepare_command_id"] != format_prepare["command_id"]:
        raise ValueError("storage format commit is not bound to its prepare command")
    if format_commit["expires_at"] != format_prepare["expires_at"]:
        raise ValueError("storage format commit outlives its prepare command")
    if set(reset_prepare["payload"]) != {
        "confirmation_token",
        "reset_generation",
        "server_sequence_floor",
    }:
        raise ValueError("data reset prepare payload differs")
    if set(reset_commit["payload"]) != {
        "prepare_command_id",
        "confirmation_token",
        "reset_generation",
        "sequence_floor",
    }:
        raise ValueError("data reset commit payload differs")
    if reset_commit["payload"]["prepare_command_id"] != reset_prepare["command_id"]:
        raise ValueError("data reset commit is not bound to its prepare command")
    if reset_commit["expires_at"] != reset_prepare["expires_at"]:
        raise ValueError("data reset commit outlives its prepare command")
    if set(reset_cancel["payload"]) != {"prepare_command_id"}:
        raise ValueError("data reset cancel payload differs")
    if reset_cancel["payload"]["prepare_command_id"] != reset_prepare["command_id"]:
        raise ValueError("data reset cancel is not bound to its prepare command")
    if reset_cancel["expires_at"] != reset_prepare["expires_at"]:
        raise ValueError("data reset cancel outlives its prepare command")
    if COMMIT_CONFIRMATION_PHRASES != {
        "format_storage_commit": "FORMAT STORAGE",
        "data_reset_commit": "CLEAR READINGS",
    }:
        raise ValueError("live server typed confirmation phrases differ")

    results_by_id = {result["command_id"]: result for result in results}
    format_prepare_evidence = results_by_id[format_prepare["command_id"]]["evidence"]
    format_commit_evidence = results_by_id[format_commit["command_id"]]["evidence"]
    reset_prepare_evidence = results_by_id[reset_prepare["command_id"]]["evidence"]
    reset_commit_evidence = results_by_id[reset_commit["command_id"]]["evidence"]
    cancel_evidence = results_by_id[reset_cancel["command_id"]]["evidence"]
    if (
        set(format_prepare_evidence)
        != {
            "prepare_command_id",
            "acknowledged_records_lost",
            "unacknowledged_records_lost",
            "ready",
        }
        or format_prepare_evidence.get("ready") is not True
    ):
        raise ValueError("storage format prepare evidence differs")
    if (
        set(format_commit_evidence)
        != {
            "prepare_command_id",
            "acknowledged_records_lost",
            "unacknowledged_records_lost",
            "formatted",
        }
        or format_commit_evidence.get("formatted") is not True
    ):
        raise ValueError("storage format commit evidence differs")
    if (
        set(reset_prepare_evidence)
        != {
            "prepare_command_id",
            "reset_generation",
            "server_sequence_floor",
            "sequence_floor",
            "ready",
        }
        or reset_prepare_evidence.get("ready") is not True
    ):
        raise ValueError("data reset prepare evidence differs")
    if reset_prepare_evidence["sequence_floor"] < reset_prepare_evidence["server_sequence_floor"]:
        raise ValueError("data reset prepare actual floor regresses below the server floor")
    if set(reset_commit_evidence) != {
        "prepare_command_id",
        "reset_generation",
        "sequence_floor",
    }:
        raise ValueError("data reset commit evidence differs")
    if reset_commit["payload"]["sequence_floor"] != reset_prepare_evidence["sequence_floor"]:
        raise ValueError("data reset commit does not use the authenticated prepare floor")
    if reset_commit_evidence["sequence_floor"] != reset_commit["payload"]["sequence_floor"]:
        raise ValueError("data reset completion floor differs from its command")
    if cancel_evidence != {
        "prepare_command_id": reset_prepare["command_id"],
        "cancelled": True,
    }:
        raise ValueError("data reset cancel evidence differs")
    if "confirmation_token" in json.dumps(results):
        raise ValueError("destructive command result evidence leaks a confirmation token")

    rotation = fixtures["credential_rotation"]
    if (
        rotation.get("contract_id") != "pm-credential-rotation-contract/1.0.0"
        or rotation.get("schema") != ROTATION_SCHEMA
    ):
        raise ValueError("credential rotation contract identifier differs")
    rotation_commands = rotation.get("commands")
    rotation_results = rotation.get("results")
    if not isinstance(rotation_commands, list) or not isinstance(rotation_results, list):
        raise ValueError("credential rotation commands/results must be arrays")
    if len(rotation_commands) != 3 or len(rotation_results) != 3:
        raise ValueError("credential rotation requires exact prepare, commit, and cancel vectors")
    for rotation_command in rotation_commands:
        envelope_validator.validate(rotation_command)
        if (
            rotation_command.get("required_firmware_capability")
            != COMMAND_CAPABILITIES["rotate_device_credentials"]
        ):
            raise ValueError("credential rotation capability differs from the live server")
        lifetime = int(
            (
                _utc(rotation_command["expires_at"])
                - _utc(rotation_command["not_before"])
            ).total_seconds()
        )
        if not 0 < lifetime <= COMMAND_EXPIRY_SECONDS["rotate_device_credentials"]:
            raise ValueError("credential rotation command outlives the server overlap limit")
    for rotation_result in rotation_results:
        result_validator.validate(rotation_result)
    prepare_commands = [
        item for item in rotation_commands if "device_secret_hex" in item.get("payload", {})
    ]
    commit_commands = [
        item
        for item in rotation_commands
        if set(item.get("payload", {}))
        == {"schema", "rotation_id", "credential_fingerprint"}
    ]
    cancel_commands = [
        item for item in rotation_commands if item.get("payload", {}).get("cancelled") is True
    ]
    if not (len(prepare_commands) == len(commit_commands) == len(cancel_commands) == 1):
        raise ValueError("credential rotation phase payloads are ambiguous")
    rotation_prepare = prepare_commands[0]
    rotation_commit = commit_commands[0]
    rotation_cancel = cancel_commands[0]
    prepare_payload = rotation_prepare["payload"]
    candidate_secret = bytes.fromhex(prepare_payload["device_secret_hex"])
    if len(candidate_secret) != 32 or hashlib.sha256(candidate_secret).hexdigest() != (
        prepare_payload["credential_fingerprint"]
    ):
        raise ValueError("credential rotation fingerprint is not bound to its candidate secret")
    rotation_id = prepare_payload["rotation_id"]
    fingerprint = prepare_payload["credential_fingerprint"]
    if prepare_payload["schema"] != ROTATION_SCHEMA:
        raise ValueError("credential rotation prepare uses the wrong payload schema")
    if prepare_payload["overlap_expires_at"] != rotation_prepare["expires_at"]:
        raise ValueError("credential rotation overlap differs from prepare expiry")
    for linked in (rotation_commit, rotation_cancel):
        if linked["payload"].get("rotation_id") != rotation_id:
            raise ValueError("credential rotation command is not bound to its prepare")
        if linked["expires_at"] != rotation_prepare["expires_at"]:
            raise ValueError("credential rotation linked command outlives its prepare")
    if rotation_commit["payload"].get("credential_fingerprint") != fingerprint:
        raise ValueError("credential rotation commit fingerprint differs")
    if rotation.get("authentication") != {
        "prepare_command": "old_server_to_device",
        "prepare_result": "old_device_to_server",
        "commit_command": "old_server_to_device",
        "commit_result": "new_device_to_server",
        "cancel_command": "old_server_to_device",
        "cancel_result": "old_device_to_server",
    }:
        raise ValueError("credential rotation directional authentication differs")
    if rotation.get("recovery") != {
        "prepared_candidate": "persist_and_resume",
        "untrusted_time": "dormant_fail_closed",
        "commit_intent": "resume_activation",
        "result_cleanup": "authenticated_result_ack_required",
    }:
        raise ValueError("credential rotation durable recovery policy differs")
    rotation_results_by_id = {item["command_id"]: item for item in rotation_results}
    if rotation_results_by_id[rotation_prepare["command_id"]]["evidence"] != {
        "rotation_id": rotation_id,
        "credential_fingerprint": fingerprint,
        "ready": True,
    }:
        raise ValueError("credential rotation prepare evidence differs")
    if rotation_results_by_id[rotation_commit["command_id"]]["evidence"] != {
        "rotation_id": rotation_id,
        "credential_fingerprint": fingerprint,
        "activated": True,
    }:
        raise ValueError("credential rotation commit evidence differs")
    if rotation_results_by_id[rotation_cancel["command_id"]]["evidence"] != {
        "rotation_id": rotation_id,
        "cancelled": True,
    }:
        raise ValueError("credential rotation cancel evidence differs")
    if "device_secret_hex" in json.dumps(rotation_results):
        raise ValueError("credential rotation results leak the candidate secret")

    openapi = load_object(server / SHARED_FILES["power-meter-v2.openapi.json"])
    download_path = openapi.get("paths", {}).get("/api/v1/device/firmware/{release_id}", {})
    responses = download_path.get("get", {}).get("responses", {})
    if "200" not in responses or "206" in responses:
        raise ValueError("server OpenAPI does not enforce full-image OTA restart semantics")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--firmware-root", type=Path, required=True)
    args = parser.parse_args()
    server = args.server_root.resolve(strict=True)
    firmware = args.firmware_root.resolve(strict=True)
    contract = load_object(firmware / "test/vectors/server-contract.json")
    if set(contract) != {
        "contract_id",
        "protocol_id",
        "firmware_repository",
        "server_repository",
        "requests",
        "downloads",
        "commands",
        "shared_contract_sha256",
    }:
        raise ValueError("firmware server contract contains an unexpected top-level shape")
    if contract.get("contract_id") != CONTRACT_ID or contract.get("protocol_id") != PROTOCOL:
        raise ValueError("firmware server contract identifier/protocol mismatch")
    if contract.get("firmware_repository") != FIRMWARE_REPOSITORY:
        raise ValueError("firmware contract names the wrong firmware repository")
    if contract.get("server_repository") != SERVER_REPOSITORY:
        raise ValueError("firmware contract names the wrong server repository")

    requests = contract.get("requests")
    if not isinstance(requests, list) or len(requests) != len(REQUESTS):
        raise ValueError("firmware contract must declare every exact request")
    declared_requests: dict[str, dict[str, Any]] = {}
    for request in requests:
        if not isinstance(request, dict) or not isinstance(request.get("name"), str):
            raise ValueError("firmware request declaration is invalid")
        name = request["name"]
        if name in declared_requests:
            raise ValueError(f"duplicate firmware request declaration: {name}")
        declared_requests[name] = request
    if set(declared_requests) != set(REQUESTS):
        raise ValueError("firmware request set does not match the server contract")
    for name, expected in REQUESTS.items():
        if declared_requests[name] != {"name": name, **expected}:
            raise ValueError(f"firmware request declaration differs from server: {name}")

    validate_shared_snapshots(server, firmware, contract)
    validate_commands(server, firmware, contract)
    openapi = load_object(server / "shared/openapi/power-meter-v2.openapi.json")
    enrollment_schema = openapi["components"]["schemas"]["DeviceEnrollmentRequest"]
    firmware_enrollment_schema = load_object(
        confined_file(firmware, REQUESTS["enrollment"]["schema"])
    )
    if without_titles(enrollment_schema) != without_titles(firmware_enrollment_schema):
        raise ValueError("firmware enrollment schema differs from the server OpenAPI contract")

    live_schemas = {
        "enrollment": enrollment_schema,
        "heartbeat": load_object(server / SHARED_FILES["device-heartbeat.schema.json"]),
        "reading_batch": load_object(server / SHARED_FILES["device-reading-batch.schema.json"]),
        "permanent_loss": load_object(server / SHARED_FILES["device-permanent-loss.schema.json"]),
    }
    format_checker = FormatChecker()
    fixtures: dict[str, dict[str, Any]] = {}
    for name, expected in REQUESTS.items():
        schema = live_schemas[name]
        Draft202012Validator.check_schema(schema)
        fixture = load_object(confined_file(firmware, expected["fixture"]))
        Draft202012Validator(schema, format_checker=format_checker).validate(fixture)
        if fixture.get("protocol_id") != PROTOCOL:
            raise ValueError(f"fixture {name} lacks exact protocol_id")
        fixtures[name] = fixture

    measurement = fixtures["heartbeat"].get("measurement")
    if not isinstance(measurement, dict) or measurement.get("pzem_status") != "ok":
        raise ValueError("heartbeat fixture must contain authenticated PZEM evidence")
    for field in ("voltage_v", "current_a", "active_power_w", "frequency_hz", "power_factor"):
        if field not in measurement or measurement[field] is None:
            raise ValueError(f"heartbeat fixture lacks measured {field}")

    records = fixtures["reading_batch"].get("records")
    if not isinstance(records, list) or len(records) < 2:
        raise ValueError("reading fixture must exercise multiple ordered records")
    if not any(record.get("interval_energy_mwh") == 0 for record in records):
        raise ValueError("reading fixture must preserve a measured zero-energy interval")
    if not any(record.get("interval_energy_mwh") is None for record in records):
        raise ValueError("reading fixture must distinguish unavailable null energy")
    sequences = [record.get("sequence") for record in records]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        raise ValueError("reading fixture sequences are not strictly ordered and unique")

    ranges = fixtures["permanent_loss"].get("ranges")
    if not isinstance(ranges, list) or not ranges:
        raise ValueError("permanent-loss fixture must contain durable loss evidence")
    print(
        "firmware fixtures, endpoints, downloads, OTA canonical bytes, destructive commands, "
        "credential rotation, and locked schemas match live server contracts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
