from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encrypt_secret(master_key: bytes, plaintext: bytes, *, context: bytes) -> bytes:
    nonce = os.urandom(12)
    return b"PME1" + nonce + AESGCM(master_key).encrypt(nonce, plaintext, context)


def decrypt_secret(master_key: bytes, value: bytes, *, context: bytes) -> bytes:
    if len(value) < 32 or not value.startswith(b"PME1"):
        raise ValueError("unsupported encrypted-secret format")
    return AESGCM(master_key).decrypt(value[4:16], value[16:], context)


def secret_fingerprint(secret: bytes) -> str:
    """Return the non-secret, collision-resistant identifier used by rotation evidence."""

    return hashlib.sha256(secret).hexdigest()
