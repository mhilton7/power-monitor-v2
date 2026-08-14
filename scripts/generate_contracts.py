"""Generate deterministic shared protocol contracts and cryptographic vectors."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("PM_ENV", "test")

from backend.app.main import app
from backend.app.schemas.billing import RatePlanDraft
from backend.app.schemas.device import (
    DeviceResponse,
    HeartbeatRequest,
    PermanentLossRequest,
    ReadingBatchRequest,
)
from backend.app.security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
)

ROOT = Path(__file__).resolve().parents[1]


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def schemas() -> dict[str, dict[str, Any]]:
    return {
        "device-heartbeat.schema.json": HeartbeatRequest.model_json_schema(),
        "device-reading-batch.schema.json": ReadingBatchRequest.model_json_schema(),
        "device-permanent-loss.schema.json": PermanentLossRequest.model_json_schema(),
        "server-device-response.schema.json": DeviceResponse.model_json_schema(),
        "bill-rate-plan-draft.schema.json": RatePlanDraft.model_json_schema(),
    }


def authentication_vectors() -> dict[str, Any]:
    secret = bytes(range(32))
    device_id = "123e4567-e89b-12d3-a456-426614174000"
    body = b"sample-body"
    digest = body_sha256(body)
    common = {
        "device_secret_hex": secret.hex(),
        "device_id": device_id,
        "timestamp": "1786641600",
        "nonce": "0123456789abcdef0123456789abcdef",
        "body_utf8": body.decode(),
        "body_sha256": digest,
    }
    vectors: list[dict[str, str]] = []
    for direction, method in (
        ("device-to-server", "POST"),
        ("server-to-device", "RESPONSE"),
    ):
        key = derive_directional_key(secret, device_id, direction)
        canonical = canonical_request(
            method,
            "/api/v1/device/readings",
            "b=two&a=1",
            common["timestamp"],
            common["nonce"],
            digest,
        )
        vectors.append(
            {
                "direction": direction,
                "method": method,
                "path": "/api/v1/device/readings",
                "raw_query": "b=two&a=1",
                "canonical_query": "a=1&b=two",
                "derived_key_hex": key.hex(),
                "canonical_utf8": canonical.decode(),
                "signature_base64": sign_request(key, canonical),
            }
        )
    return {
        "protocol": "pm-protocol/1.0.0",
        "algorithm": "PM-HMAC-SHA256-V1",
        "common": common,
        "vectors": vectors,
    }


def generated_files() -> dict[Path, bytes]:
    values = {
        ROOT / "shared" / "schemas" / name: _json_bytes(schema)
        for name, schema in schemas().items()
    }
    values[ROOT / "shared" / "auth-test-vectors" / "hmac-sha256-v1.json"] = _json_bytes(
        authentication_vectors()
    )
    values[ROOT / "shared" / "openapi" / "power-meter-v2.openapi.json"] = _json_bytes(app.openapi())
    return values


def main() -> None:
    for path, content in generated_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


if __name__ == "__main__":
    main()
