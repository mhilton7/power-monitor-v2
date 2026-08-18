from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import orjson
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..constants import (
    MAX_DEVICE_RESPONSE_BYTES,
    MAX_FUTURE_TIME_SECONDS,
    MAX_HEARTBEAT_BODY_BYTES,
    MAX_READING_BODY_BYTES,
    PROTOCOL_ID,
)
from ..db import get_session
from ..errors import AuthenticationError, IntegrityConflict, NotFound
from ..models import (
    AuditEvent,
    Device,
    DeviceCommand,
    DeviceCredential,
    DeviceHeartbeat,
    EnrollmentToken,
    aware_utc,
    new_uuid,
    user_home_scopes,
)
from ..schemas.api import (
    CredentialRotationCancelRequest,
    CredentialRotationRequest,
    DeviceEnrollmentRequest,
    EnrollmentTokenRequest,
)
from ..schemas.device import (
    DeviceResponse,
    HeartbeatRequest,
    PermanentLossRequest,
    ReadingBatchRequest,
    StatelessTelemetryConfiguration,
    StatelessTelemetryRequest,
    StatelessTelemetryResponse,
    StatelessTelemetrySampleIdentity,
)
from ..security.auth import CurrentUser, require_permission, token_hash
from ..security.crypto import encrypt_secret, secret_fingerprint
from ..security.device_auth import authenticate_device_request
from ..security.protocol import (
    body_sha256,
    canonical_request,
    derive_directional_key,
    sign_request,
)
from ..services.commands import (
    ROTATION_SCHEMA,
    apply_command_results,
    create_command,
    deliver_commands,
    expire_rotation_credentials,
)
from ..services.firmware_deployments import reconcile_firmware_version_heartbeat
from ..services.ingestion import find_gaps, ingest_batch, record_permanent_loss
from ..services.stateless_telemetry import ingest_stateless_sample

router = APIRouter(prefix="/api/v1", tags=["devices"])


async def _require_home_scope(session: AsyncSession, user_id: str, home_id: str) -> None:
    permitted = await session.scalar(
        select(user_home_scopes.c.user_id).where(
            user_home_scopes.c.user_id == user_id,
            user_home_scopes.c.home_id == home_id,
        )
    )
    if permitted is None:
        raise NotFound("home does not exist")


def _device_response_body(payload: BaseModel | dict[str, object]) -> bytes:
    value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def _signed_device_response(
    *,
    payload: BaseModel | dict[str, object],
    request: Request,
    device_id: str,
    device_secret: bytes,
    status_code: int = 200,
) -> Response:
    body = _device_response_body(payload)
    timestamp = str(int(datetime.now(UTC).timestamp()))
    nonce = secrets.token_urlsafe(24)
    digest = body_sha256(body)
    canonical = canonical_request(
        "RESPONSE", request.url.path, request.url.query, timestamp, nonce, digest
    )
    key = derive_directional_key(device_secret, device_id, "server-to-device")
    return Response(
        content=body,
        status_code=status_code,
        media_type="application/json",
        headers={
            "X-PM-Protocol": PROTOCOL_ID,
            "X-PM-Device-ID": device_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Nonce": nonce,
            "X-PM-Content-SHA256": digest,
            "X-PM-Signature": sign_request(key, canonical),
        },
    )


async def _scoped_device_for_update(
    session: AsyncSession, *, user_id: str, device_id: str
) -> Device:
    device = await session.scalar(
        select(Device)
        .join(user_home_scopes, user_home_scopes.c.home_id == Device.home_id)
        .where(Device.id == device_id, user_home_scopes.c.user_id == user_id)
        .with_for_update()
    )
    if device is None:
        raise NotFound("device does not exist")
    return device


def _public_rotation(credential: DeviceCredential) -> dict[str, object]:
    return {
        "rotation_id": credential.rotation_id,
        "credential_fingerprint": credential.fingerprint,
        "state": credential.state,
        "overlap_expires_at": credential.overlap_expires_at,
        "prepare_command_id": credential.prepare_command_id,
        "commit_command_id": credential.commit_command_id,
        "cancel_command_id": credential.cancel_command_id,
    }


@router.post("/enrollment-tokens", status_code=201)
async def create_enrollment_token(
    payload: EnrollmentTokenRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("sensors.enroll")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await _require_home_scope(session, user.id, payload.home_id)
    plaintext = secrets.token_urlsafe(36)
    now = datetime.now(UTC)
    row = EnrollmentToken(
        token_hash=token_hash(plaintext),
        home_id=payload.home_id,
        friendly_name=payload.friendly_name,
        ct_rating_a=payload.ct_rating_a,
        pzem_variant=payload.pzem_variant,
        issued_by_user_id=user.id,
        created_at=now,
        expires_at=now + timedelta(minutes=payload.expires_minutes),
    )
    session.add(row)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="DEVICE_ENROLLMENT_TOKEN_CREATED",
            target_type="home",
            target_id=payload.home_id,
            correlation_id=request.state.correlation_id,
            details={"expires_at": row.expires_at.isoformat()},
        )
    )
    await session.commit()
    return {"token": plaintext, "expires_at": row.expires_at}


@router.post("/devices/enroll", status_code=201)
async def enroll_device(
    payload: DeviceEnrollmentRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    now = datetime.now(UTC)
    async with session.begin():
        token = await session.scalar(
            select(EnrollmentToken)
            .where(
                EnrollmentToken.token_hash == token_hash(payload.enrollment_token),
                EnrollmentToken.consumed_at.is_(None),
                EnrollmentToken.expires_at > now,
            )
            .with_for_update()
        )
        if token is None:
            raise AuthenticationError("enrollment token is invalid, expired, or already used")
        device = Device(
            home_id=token.home_id,
            friendly_name=token.friendly_name,
            protocol_id=payload.protocol_id,
            pzem_variant=token.pzem_variant,
            ct_rating_a=token.ct_rating_a,
            measurement_scope="energy_only",
            state="enrolled",
            firmware_version=payload.firmware_version,
        )
        session.add(device)
        await session.flush()
        secret = secrets.token_bytes(32)
        credential = DeviceCredential(
            device_id=device.id,
            encrypted_secret=encrypt_secret(
                settings.master_key, secret, context=device.id.encode()
            ),
            fingerprint=secret_fingerprint(secret),
            key_version=1,
            state="active",
            activated_at=now,
        )
        session.add(credential)
        token.consumed_at = now
        token.consumed_by_device_id = device.id
        session.add(
            AuditEvent(
                actor_user_id=token.issued_by_user_id,
                event_code="DEVICE_ENROLLED",
                target_type="device",
                target_id=device.id,
                correlation_id=request.state.correlation_id,
                details={
                    "credential_fingerprint": credential.fingerprint,
                    "hardware_fingerprint": hashlib.sha256(
                        payload.hardware_fingerprint.encode()
                    ).hexdigest()[:16],
                    "pzem_variant": device.pzem_variant,
                },
            )
        )
    return {
        "device_id": device.id,
        "device_secret": base64.b64encode(secret).decode("ascii"),
        "credential_fingerprint": credential.fingerprint,
        "protocol_id": PROTOCOL_ID,
    }


@router.post("/devices/{device_id}/credentials/rotate", status_code=202)
async def rotate_device_credential(
    device_id: str,
    payload: CredentialRotationRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("sensors.configure")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    device = await _scoped_device_for_update(session, user_id=user.id, device_id=device_id)
    now = datetime.now(UTC)
    await expire_rotation_credentials(session, now=now)
    existing_command = await session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.device_id == device.id,
            DeviceCommand.idempotency_key == payload.idempotency_key,
        )
    )
    if existing_command is not None:
        existing_credential = await session.scalar(
            select(DeviceCredential).where(
                DeviceCredential.prepare_command_id == existing_command.id,
                DeviceCredential.device_id == device.id,
            )
        )
        if existing_credential is None:
            raise IntegrityConflict("idempotency key was used for another operation")
        return {"rotation": _public_rotation(existing_credential)}
    in_progress = await session.scalar(
        select(DeviceCredential).where(
            DeviceCredential.device_id == device.id,
            DeviceCredential.state.in_(("pending", "prepared")),
            DeviceCredential.revoked_at.is_(None),
        )
    )
    if in_progress is not None:
        raise IntegrityConflict("a credential rotation is already in progress")
    active = await session.scalar(
        select(DeviceCredential).where(
            DeviceCredential.device_id == device.id,
            DeviceCredential.state == "active",
            DeviceCredential.revoked_at.is_(None),
        )
    )
    if active is None:
        raise IntegrityConflict("device has no active credential")
    latest_key_version = await session.scalar(
        select(func.max(DeviceCredential.key_version)).where(
            DeviceCredential.device_id == device.id
        )
    )
    overlap_expires_at = now + timedelta(minutes=10)
    rotation_id = new_uuid()
    candidate_secret = bytearray(secrets.token_bytes(32))
    try:
        fingerprint = secret_fingerprint(bytes(candidate_secret))
        encrypted_secret = encrypt_secret(
            settings.master_key,
            bytes(candidate_secret),
            context=device.id.encode(),
        )
    finally:
        candidate_secret[:] = b"\0" * len(candidate_secret)
    command, _unused_token = await create_command(
        session,
        device_id=device.id,
        command_type="rotate_device_credentials",
        issued_by_user_id=user.id,
        idempotency_key=payload.idempotency_key,
        payload={
            "schema": ROTATION_SCHEMA,
            "rotation_id": rotation_id,
            "credential_fingerprint": fingerprint,
            "overlap_expires_at": overlap_expires_at.isoformat().replace("+00:00", "Z"),
        },
        expires_at=overlap_expires_at,
    )
    candidate = DeviceCredential(
        device_id=device.id,
        encrypted_secret=encrypted_secret,
        fingerprint=fingerprint,
        key_version=int(latest_key_version or 0) + 1,
        state="pending",
        rotation_id=rotation_id,
        overlap_expires_at=overlap_expires_at,
        prepare_command_id=command.id,
        initiated_by_user_id=user.id,
    )
    session.add(candidate)
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="DEVICE_CREDENTIAL_ROTATION_PREPARED",
            target_type="device",
            target_id=device.id,
            correlation_id=request.state.correlation_id,
            details={
                "rotation_id": rotation_id,
                "credential_fingerprint": fingerprint,
                "overlap_expires_at": overlap_expires_at.isoformat(),
            },
        )
    )
    await session.commit()
    return {"rotation": _public_rotation(candidate)}


@router.post("/devices/{device_id}/credentials/rotations/{rotation_id}/cancel", status_code=202)
async def cancel_device_credential_rotation(
    device_id: str,
    rotation_id: str,
    payload: CredentialRotationCancelRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("sensors.configure")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    device = await _scoped_device_for_update(session, user_id=user.id, device_id=device_id)
    now = datetime.now(UTC)
    await expire_rotation_credentials(session, now=now)
    candidate = await session.scalar(
        select(DeviceCredential)
        .where(
            DeviceCredential.device_id == device.id,
            DeviceCredential.rotation_id == rotation_id,
        )
        .with_for_update()
    )
    if (
        candidate is None
        or candidate.state not in {"pending", "prepared"}
        or candidate.overlap_expires_at is None
        or aware_utc(candidate.overlap_expires_at) <= now
    ):
        raise NotFound("credential rotation does not exist")
    existing = await session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.device_id == device.id,
            DeviceCommand.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None:
        if existing.id != candidate.cancel_command_id:
            raise IntegrityConflict("idempotency key was used for another operation")
        return {"rotation": _public_rotation(candidate)}
    if candidate.cancel_command_id is not None:
        raise IntegrityConflict("credential rotation cancellation is already queued")
    if candidate.commit_command_id is not None:
        commit = await session.get(DeviceCommand, candidate.commit_command_id)
        if commit is not None and commit.state != "queued":
            raise IntegrityConflict("credential activation has already been delivered")
        if commit is not None:
            commit.state = "superseded"
    cancel, _unused_token = await create_command(
        session,
        device_id=device.id,
        command_type="rotate_device_credentials",
        issued_by_user_id=user.id,
        idempotency_key=payload.idempotency_key,
        payload={
            "schema": ROTATION_SCHEMA,
            "rotation_id": rotation_id,
            "cancelled": True,
        },
        expires_at=candidate.overlap_expires_at,
    )
    candidate.cancel_command_id = cancel.id
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="DEVICE_CREDENTIAL_ROTATION_CANCEL_QUEUED",
            target_type="device",
            target_id=device.id,
            correlation_id=request.state.correlation_id,
            details={"rotation_id": rotation_id},
        )
    )
    await session.commit()
    return {"rotation": _public_rotation(candidate)}


async def _validated_device_payload(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    schema: (
        type[HeartbeatRequest]
        | type[ReadingBatchRequest]
        | type[PermanentLossRequest]
        | type[StatelessTelemetryRequest]
    ),
    max_bytes: int,
) -> tuple[
    Device,
    DeviceCredential,
    bytes,
    HeartbeatRequest | ReadingBatchRequest | PermanentLossRequest | StatelessTelemetryRequest,
    bytes,
]:
    body_buffer = bytearray()
    async for chunk in request.stream():
        if len(body_buffer) + len(chunk) > max_bytes:
            raise IntegrityConflict("device request exceeds the configured body limit")
        body_buffer.extend(chunk)
    body = bytes(body_buffer)
    authenticated = await authenticate_device_request(request, session, settings, body)
    try:
        payload = schema.model_validate_json(body)
    except ValidationError:
        raise
    return (
        authenticated.device,
        authenticated.credential,
        authenticated.secret,
        payload,
        body,
    )


@router.post("/device/heartbeat")
async def heartbeat(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    device, credential, secret, generic_payload, _body = await _validated_device_payload(
        request, session, settings, HeartbeatRequest, MAX_HEARTBEAT_BODY_BYTES
    )
    payload = generic_payload
    assert isinstance(payload, HeartbeatRequest)
    now = datetime.now(UTC)
    await apply_command_results(
        session,
        device.id,
        payload.command_results,
        authenticated_credential_id=credential.id,
    )
    measurement = payload.measurement
    if measurement.measured_at is not None and measurement.measured_at.astimezone(
        UTC
    ) > now + timedelta(seconds=MAX_FUTURE_TIME_SECONDS):
        raise IntegrityConflict("heartbeat measurement timestamp is unacceptably far in the future")
    session.add(
        DeviceHeartbeat(
            device_id=device.id,
            boot_id=payload.boot_id,
            received_at=now,
            measured_at=measurement.measured_at,
            voltage_v=measurement.voltage_v,
            current_a=measurement.current_a,
            active_power_w=measurement.active_power_w,
            frequency_hz=measurement.frequency_hz,
            power_factor=measurement.power_factor,
            pzem_status=measurement.pzem_status,
            storage_status=payload.storage_status,
            storage_bytes_total=payload.storage_bytes_total,
            storage_bytes_free=payload.storage_bytes_free,
            time_status=payload.time_status,
            wifi_rssi=payload.wifi_rssi,
            ip_address=payload.ip_address,
            backlog=payload.backlog,
            oldest_sequence=payload.oldest_sequence,
            newest_sequence=payload.newest_sequence,
            free_internal_heap=payload.free_internal_heap,
            largest_internal_block=payload.largest_internal_block,
            reboot_reason=payload.reboot_reason,
            health_flags=payload.health_flags,
        )
    )
    device.last_heartbeat_at = now
    device.firmware_version = payload.firmware_version
    await reconcile_firmware_version_heartbeat(
        session,
        device_id=device.id,
        firmware_version=payload.firmware_version,
        now=now,
    )
    device.maximum_sequence = max(device.maximum_sequence, payload.newest_sequence or 0)
    if payload.acknowledged_sequence > device.contiguous_ack:
        raise IntegrityConflict("device claims an acknowledgement the server has not committed")
    gaps = await find_gaps(session, device)
    empty_response = DeviceResponse(
        server_time=now,
        highest_contiguous_sequence=device.contiguous_ack,
        gaps=gaps,
        commands=[],
    )
    minimum_response = DeviceResponse(
        server_time=now,
        highest_contiguous_sequence=device.contiguous_ack,
        gaps=[],
        commands=[],
    )
    # Gap evidence is advisory and will be returned again. Bound it before
    # command delivery so the complete authenticated response always fits the
    # firmware's fixed receive buffer.
    while gaps and len(_device_response_body(empty_response)) > MAX_DEVICE_RESPONSE_BYTES:
        gaps.pop()
        empty_response = DeviceResponse(
            server_time=now,
            highest_contiguous_sequence=device.contiguous_ack,
            gaps=gaps,
            commands=[],
        )
    empty_response_size = len(_device_response_body(empty_response))
    if empty_response_size > MAX_DEVICE_RESPONSE_BYTES:
        raise IntegrityConflict("device response exceeds the protocol byte limit")
    commands = await deliver_commands(
        session,
        device.id,
        settings=settings,
        response_byte_budget=MAX_DEVICE_RESPONSE_BYTES - empty_response_size,
        maximum_single_envelope_bytes=(
            MAX_DEVICE_RESPONSE_BYTES - len(_device_response_body(minimum_response))
        ),
    )
    response = DeviceResponse(
        server_time=now,
        highest_contiguous_sequence=device.contiguous_ack,
        gaps=gaps,
        commands=commands,
    )
    if len(_device_response_body(response)) > MAX_DEVICE_RESPONSE_BYTES:
        raise IntegrityConflict("device response exceeds the protocol byte limit")
    await session.commit()
    return _signed_device_response(
        payload=response, request=request, device_id=device.id, device_secret=secret
    )


@router.post(
    "/device/telemetry/v2",
    response_model=StatelessTelemetryResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": StatelessTelemetryRequest.model_json_schema()}
            },
        }
    },
)
async def stateless_telemetry(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    device, credential, secret, generic_payload, _body = await _validated_device_payload(
        request,
        session,
        settings,
        StatelessTelemetryRequest,
        MAX_HEARTBEAT_BODY_BYTES,
    )
    payload = generic_payload
    assert isinstance(payload, StatelessTelemetryRequest)
    await apply_command_results(
        session,
        device.id,
        payload.command_results,
        authenticated_credential_id=credential.id,
    )
    result = await ingest_stateless_sample(session, device.id, payload)
    if result.advances_live_state:
        await reconcile_firmware_version_heartbeat(
            session,
            device_id=device.id,
            firmware_version=payload.firmware_version,
            now=result.received_at,
        )
    identity = StatelessTelemetrySampleIdentity(
        sensor_id=device.id,
        boot_id=payload.boot_id,
        sample_sequence=payload.sample_sequence,
    )
    configuration = StatelessTelemetryConfiguration(
        version=result.config_version,
        telemetry_interval_seconds=result.telemetry_interval_seconds,
    )
    empty = StatelessTelemetryResponse(
        status=result.status,
        server_received_at=result.received_at,
        sample=identity,
        timestamp_source=result.timestamp_source,
        configuration=configuration,
        commands=[],
    )
    empty_size = len(_device_response_body(empty))
    if empty_size > MAX_DEVICE_RESPONSE_BYTES:
        raise IntegrityConflict("device response exceeds the protocol byte limit")
    commands = await deliver_commands(
        session,
        device.id,
        settings=settings,
        response_byte_budget=MAX_DEVICE_RESPONSE_BYTES - empty_size,
        maximum_single_envelope_bytes=MAX_DEVICE_RESPONSE_BYTES - empty_size,
    )
    response = StatelessTelemetryResponse(
        status=result.status,
        server_received_at=result.received_at,
        sample=identity,
        timestamp_source=result.timestamp_source,
        configuration=configuration,
        commands=commands,
    )
    if len(_device_response_body(response)) > MAX_DEVICE_RESPONSE_BYTES:
        raise IntegrityConflict("device response exceeds the protocol byte limit")
    await session.commit()
    return _signed_device_response(
        payload=response,
        request=request,
        device_id=device.id,
        device_secret=secret,
    )


@router.post("/device/readings")
async def readings(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    device, _credential, secret, generic_payload, _body = await _validated_device_payload(
        request, session, settings, ReadingBatchRequest, MAX_READING_BODY_BYTES
    )
    payload = generic_payload
    assert isinstance(payload, ReadingBatchRequest)
    result = await ingest_batch(session, device.id, payload)
    await session.commit()
    return _signed_device_response(
        payload={
            "protocol_id": PROTOCOL_ID,
            "server_time": datetime.now(UTC),
            "accepted": result.accepted,
            "identical_retries": result.identical_retries,
            "highest_contiguous_sequence": result.highest_contiguous_sequence,
            "gaps": result.gaps,
        },
        request=request,
        device_id=device.id,
        device_secret=secret,
    )


@router.post("/device/permanent-loss")
async def permanent_loss(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    device, _credential, secret, generic_payload, _body = await _validated_device_payload(
        request, session, settings, PermanentLossRequest, MAX_HEARTBEAT_BODY_BYTES
    )
    payload = generic_payload
    assert isinstance(payload, PermanentLossRequest)
    result = await record_permanent_loss(session, device.id, payload.ranges)
    await session.commit()
    return _signed_device_response(
        payload={
            "protocol_id": PROTOCOL_ID,
            "server_time": datetime.now(UTC),
            "accepted": result.accepted,
            "highest_contiguous_sequence": result.highest_contiguous_sequence,
            "gaps": result.gaps,
        },
        request=request,
        device_id=device.id,
        device_secret=secret,
    )
