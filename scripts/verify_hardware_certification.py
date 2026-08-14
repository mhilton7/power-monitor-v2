#!/usr/bin/env python3
"""Verify schema and stable-release semantics for marked-unit HIL evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_ID = "pm-hardware-certification/1.0.0"
PROTOCOL_ID = "pm-protocol/1.0.0"
FIRMWARE_REPOSITORY = "https://github.com/mhilton7/power-monitor-sensor-headless"
REQUIRED_TESTS = {
    "pzem_authenticated_samples",
    "crc_rejection",
    "wrong_slave_rejection",
    "sd_recovery",
    "sequence_monotonic",
    "ack_replay",
    "https_chain",
    "https_hostname",
    "hmac_replay",
    "ota_success",
    "ota_rollback",
    "com_recovery",
    "watchdog_recovery",
}
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


def reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is prohibited: {value}")


def load_strict_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_nonfinite)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def object_at(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ValueError(f"{key} must be an object")
    return result


def nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def rfc3339(value: object, name: str) -> datetime:
    text = nonempty_string(value, name)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def canonical_record_sha256(evidence: dict[str, Any]) -> str:
    canonical = copy.deepcopy(evidence)
    signoff = object_at(canonical, "signoff")
    signoff.pop("record_sha256", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verify(
    evidence: dict[str, Any],
    *,
    expected_commit: str,
    expected_image_sha256: str,
    expected_version: str,
) -> None:
    if evidence.get("schema") != SCHEMA_ID or evidence.get("result") != "pass":
        raise ValueError("stable evidence must use the physical schema and result=pass")
    firmware = object_at(evidence, "firmware")
    if firmware.get("repository") != FIRMWARE_REPOSITORY:
        raise ValueError("firmware repository does not match the coordinated public repository")
    expected = {
        "commit": expected_commit,
        "image_sha256": expected_image_sha256,
        "version": expected_version,
        "esp_idf_version": "v6.0.2",
        "target": "esp32s3",
        "board_profile": "esp32-s3-devkitc-n16r8-reference/1",
        "protocol": PROTOCOL_ID,
    }
    for key, value in expected.items():
        if firmware.get(key) != value:
            raise ValueError(f"firmware.{key} does not match the promoted firmware artifact")
    marked = object_at(evidence, "marked_unit")
    for key in (
        "unit_id",
        "esp32s3_marking",
        "pzem_model_marking",
        "pzem_revision_marking",
        "pzem_terminal_labels",
        "ct_marking",
        "sd_module_marking",
    ):
        nonempty_string(marked.get(key), f"marked_unit.{key}")
    photos = marked.get("photo_sha256")
    if (
        not isinstance(photos, list)
        or not photos
        or len(set(photos)) != len(photos)
        or any(not isinstance(item, str) or not HEX_SHA256.fullmatch(item) for item in photos)
    ):
        raise ValueError("marked_unit.photo_sha256 requires unique physical-photo hashes")

    electrical = object_at(evidence, "electrical")
    for key in ("qualified_person", "isolated_test_fixture", "register_map_variant"):
        nonempty_string(electrical.get(key), f"electrical.{key}")
    if (electrical.get("uart_baud"), electrical.get("data_bits")) != (9600, 8):
        raise ValueError("electrical UART must be verified at 9600 8N1")
    if (electrical.get("parity"), electrical.get("stop_bits")) != ("none", 1):
        raise ValueError("electrical UART must be verified at 9600 8N1")

    tests = object_at(evidence, "tests")
    if set(tests) != REQUIRED_TESTS or any(tests[name] is not True for name in REQUIRED_TESTS):
        raise ValueError("every required physical/TLS/HMAC/OTA/recovery test must be true")

    soak = object_at(evidence, "soak")
    started = rfc3339(soak.get("started_at"), "soak.started_at")
    ended = rfc3339(soak.get("ended_at"), "soak.ended_at")
    elapsed_hours = (ended - started).total_seconds() / 3600
    if elapsed_hours < 72 or float(soak.get("duration_hours", 0)) < 72:
        raise ValueError("physical soak must span at least 72 hours")
    if soak.get("pass") is not True:
        raise ValueError("physical soak must pass")
    if soak.get("unexplained_reboots") != 0 or soak.get("sequence_regressions") != 0:
        raise ValueError("physical soak has an unexplained reboot or sequence regression")
    attempted = soak.get("samples_attempted")
    authenticated = soak.get("samples_authenticated")
    if (
        not isinstance(attempted, int)
        or isinstance(attempted, bool)
        or not isinstance(authenticated, int)
        or isinstance(authenticated, bool)
        or authenticated <= 0
        or authenticated > attempted
    ):
        raise ValueError("physical soak sample counts are inconsistent")

    signoff = object_at(evidence, "signoff")
    nonempty_string(signoff.get("operator"), "signoff.operator")
    nonempty_string(signoff.get("reviewer"), "signoff.reviewer")
    declared_record_hash = signoff.get("record_sha256")
    if declared_record_hash != canonical_record_sha256(evidence):
        raise ValueError("signoff.record_sha256 does not match canonical evidence")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--firmware-bin", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()

    schema = load_strict_json(args.schema)
    Draft202012Validator.check_schema(schema)
    evidence = load_strict_json(args.evidence)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(evidence)
    image_sha256 = hashlib.sha256(args.firmware_bin.read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.expected_commit):
        raise ValueError("expected firmware commit must be a full Git object ID")
    verify(
        evidence,
        expected_commit=args.expected_commit,
        expected_image_sha256=image_sha256,
        expected_version=args.expected_version.removeprefix("v"),
    )
    print("marked-unit hardware certification verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
