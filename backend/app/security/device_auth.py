from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.exceptions import InvalidTag
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..errors import AuthenticationError, ReplayDetected
from ..models import Device, DeviceCredential, DeviceNonce, aware_utc
from .crypto import decrypt_secret
from .protocol import (
    ProtocolHeaders,
    body_sha256,
    canonical_request,
    derive_directional_key,
    validate_timestamp,
    verify_signature,
)


@dataclass(frozen=True)
class AuthenticatedDeviceRequest:
    device: Device
    credential: DeviceCredential
    secret: bytes


async def authenticate_device_request(
    request: Request, session: AsyncSession, settings: Settings, body: bytes
) -> AuthenticatedDeviceRequest:
    try:
        headers = ProtocolHeaders.from_mapping(request.headers)
        validate_timestamp(headers.timestamp)
    except ValueError as exc:
        raise AuthenticationError(str(exc)) from exc
    actual_hash = body_sha256(body)
    if not hmac.compare_digest(actual_hash, headers.content_sha256):
        raise AuthenticationError("content digest mismatch")
    now = datetime.now(UTC)
    credentials = (
        await session.scalars(
        select(DeviceCredential)
        .join(Device, Device.id == DeviceCredential.device_id)
        .where(
            DeviceCredential.device_id == headers.device_id,
            DeviceCredential.revoked_at.is_(None),
            Device.revoked_at.is_(None),
            DeviceCredential.state.in_(("active", "prepared", "retiring")),
        )
        .order_by(DeviceCredential.key_version.desc())
        )
    ).all()
    credentials = [
        credential
        for credential in credentials
        if credential.state == "active"
        or (
            credential.overlap_expires_at is not None
            and aware_utc(credential.overlap_expires_at) > now
        )
    ]
    if not credentials or len(credentials) > 2:
        raise AuthenticationError("device credential is invalid")
    canonical = canonical_request(
        request.method,
        request.url.path,
        request.url.query,
        headers.timestamp,
        headers.nonce,
        headers.content_sha256,
    )
    matches: list[tuple[DeviceCredential, bytes]] = []
    for candidate in credentials:
        candidate_is_valid = True
        try:
            candidate_secret = decrypt_secret(
                settings.master_key,
                candidate.encrypted_secret,
                context=headers.device_id.encode(),
            )
        except (InvalidTag, ValueError):
            # Preserve one verification operation per candidate without accepting corrupt state.
            candidate_is_valid = False
            candidate_secret = b"\0" * 32
        key = derive_directional_key(candidate_secret, headers.device_id, "device-to-server")
        if verify_signature(key, canonical, headers.signature) and candidate_is_valid:
            matches.append((candidate, candidate_secret))
    if len(matches) != 1:
        raise AuthenticationError("device signature is invalid")
    credential, secret = matches[0]
    nonce_hash = hashlib.sha256(headers.nonce.encode()).hexdigest()
    session.add(DeviceNonce(device_id=headers.device_id, nonce_hash=nonce_hash))
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ReplayDetected("device nonce has already been accepted") from exc
    device = await session.get(Device, headers.device_id)
    if device is None:
        raise AuthenticationError("device is invalid")
    return AuthenticatedDeviceRequest(device=device, credential=credential, secret=secret)
