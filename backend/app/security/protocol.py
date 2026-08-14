from __future__ import annotations

import hashlib
import hmac
import time
from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..constants import PROTOCOL_ID

SIGNING_PREFIX = "PM-HMAC-SHA256-V1"


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_query(raw_query: str) -> str:
    pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=False)
    encoded = [(quote(key, safe="~-._"), quote(value, safe="~-._")) for key, value in pairs]
    return "&".join(f"{key}={value}" for key, value in sorted(encoded))


def canonical_path(path: str, raw_query: str = "") -> str:
    encoded_path = quote(path, safe="/~:@!$&'()*+,;=-._")
    query = canonical_query(raw_query)
    return encoded_path + ("?" + query if query else "")


def canonical_request(
    method: str,
    path: str,
    raw_query: str,
    timestamp: str,
    nonce: str,
    content_sha256: str,
) -> bytes:
    if content_sha256 != content_sha256.lower() or len(content_sha256) != 64:
        raise ValueError("content hash must be lowercase SHA-256 hex")
    return "\n".join(
        (
            SIGNING_PREFIX,
            method.upper(),
            canonical_path(path, raw_query),
            timestamp,
            nonce,
            content_sha256,
        )
    ).encode("utf-8")


def derive_directional_key(device_secret: bytes, device_id: str, direction: str) -> bytes:
    if direction not in ("device-to-server", "server-to-device"):
        raise ValueError("invalid protocol key direction")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=(PROTOCOL_ID + "\0" + device_id).encode(),
        info=("PowerMeter V2\0" + direction).encode(),
    ).derive(device_secret)


def sign_request(key: bytes, canonical: bytes) -> str:
    return b64encode(hmac.new(key, canonical, hashlib.sha256).digest()).decode("ascii")


def verify_signature(key: bytes, canonical: bytes, presented: str) -> bool:
    try:
        decoded = b64decode(presented, validate=True)
    except ValueError:
        return False
    expected = hmac.new(key, canonical, hashlib.sha256).digest()
    return hmac.compare_digest(expected, decoded)


def validate_timestamp(timestamp: str, *, now: int | None = None, window_seconds: int = 300) -> int:
    try:
        value = int(timestamp)
    except ValueError as exc:
        raise ValueError("timestamp must be Unix seconds") from exc
    current = int(time.time()) if now is None else now
    if abs(current - value) > window_seconds:
        raise ValueError("timestamp outside acceptance window")
    return value


@dataclass(frozen=True)
class ProtocolHeaders:
    protocol: str
    device_id: str
    timestamp: str
    nonce: str
    content_sha256: str
    signature: str

    @classmethod
    def from_mapping(cls, headers: Mapping[str, str]) -> ProtocolHeaders:
        def required(name: str) -> str:
            value = headers.get(name)
            if not value:
                raise ValueError("missing device authentication header")
            return value

        result = cls(
            protocol=required("X-PM-Protocol"),
            device_id=required("X-PM-Device-ID"),
            timestamp=required("X-PM-Timestamp"),
            nonce=required("X-PM-Nonce"),
            content_sha256=required("X-PM-Content-SHA256"),
            signature=required("X-PM-Signature"),
        )
        if result.protocol != PROTOCOL_ID:
            raise ValueError("unsupported device protocol")
        if not (16 <= len(result.nonce) <= 128):
            raise ValueError("invalid nonce length")
        return result
