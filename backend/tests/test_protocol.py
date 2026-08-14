from __future__ import annotations

import base64
import hashlib

from backend.app.security.protocol import (
    body_sha256,
    canonical_query,
    canonical_request,
    derive_directional_key,
    sign_request,
    validate_timestamp,
    verify_signature,
)


def test_canonical_query_is_sorted_and_encoded() -> None:
    assert canonical_query("z=last&a=hello%20world&a=first&empty=") == (
        "a=first&a=hello%20world&empty=&z=last"
    )


def test_protocol_vector_is_deterministic_and_directional() -> None:
    secret = bytes(range(32))
    device_id = "11111111-2222-3333-4444-555555555555"
    body = b'{"protocol_id":"pm-protocol/1.0.0"}'
    digest = body_sha256(body)
    canonical = canonical_request(
        "post", "/api/v1/device/readings", "b=2&a=1", "1786665600", "fixed-nonce-0001", digest
    )
    assert canonical.decode() == (
        "PM-HMAC-SHA256-V1\nPOST\n/api/v1/device/readings?a=1&b=2\n"
        "1786665600\nfixed-nonce-0001\n" + hashlib.sha256(body).hexdigest()
    )
    request_key = derive_directional_key(secret, device_id, "device-to-server")
    response_key = derive_directional_key(secret, device_id, "server-to-device")
    assert request_key != response_key
    signature = sign_request(request_key, canonical)
    assert base64.b64decode(signature)
    assert verify_signature(request_key, canonical, signature)
    assert not verify_signature(response_key, canonical, signature)


def test_timestamp_window_rejects_replay_age() -> None:
    assert validate_timestamp("1000", now=1200, window_seconds=300) == 1000
    try:
        validate_timestamp("1000", now=1301, window_seconds=300)
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("expired timestamp accepted")
