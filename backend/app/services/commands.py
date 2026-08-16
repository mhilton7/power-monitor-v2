from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..constants import MAX_COMMAND_DELIVERY_ATTEMPT
from ..errors import IntegrityConflict, NotFound
from ..models import (
    AuditEvent,
    Device,
    DeviceCommand,
    DeviceCommandAttempt,
    DeviceCredential,
    FirmwareDeployment,
    aware_utc,
)
from ..schemas.device import CommandEnvelope, CommandResult
from ..security.crypto import decrypt_secret

COMMAND_PERMISSIONS = {
    "reboot": "sensors.command.reboot",
    "maintenance_sleep": "sensors.command.sleep",
    "sync_now": "sensors.configure",
    "diagnostics_snapshot": "sensors.view",
    "network_self_test": "sensors.configure",
    "meter_self_test": "sensors.configure",
    "storage_self_test": "sensors.command.storage_test",
    "format_storage_prepare": "sensors.command.storage_format",
    "format_storage_commit": "sensors.command.storage_format",
    "apply_configuration": "sensors.configure",
    "rotate_device_credentials": "sensors.configure",
    "ota_install": "sensors.command.ota",
    "data_reset_prepare": "sensors.command.data_reset",
    "data_reset_commit": "sensors.command.data_reset",
    "data_reset_cancel": "sensors.command.data_reset",
}

DESTRUCTIVE_PREPARE_TYPES = {"format_storage_prepare", "data_reset_prepare"}
DESTRUCTIVE_COMMIT_TYPES = {"format_storage_commit", "data_reset_commit"}
COMMIT_PREPARE_TYPES = {
    "format_storage_commit": "format_storage_prepare",
    "data_reset_commit": "data_reset_prepare",
}
COMMIT_CONFIRMATION_PHRASES = {
    "format_storage_commit": "FORMAT STORAGE",
    "data_reset_commit": "CLEAR READINGS",
}
COMMAND_CAPABILITIES = {
    "ota_install": "ota_v1",
    "rotate_device_credentials": "credential_rotation_v1",
    "format_storage_prepare": "destructive_commands_v1",
    "format_storage_commit": "destructive_commands_v1",
    "data_reset_prepare": "destructive_commands_v1",
    "data_reset_commit": "destructive_commands_v1",
    "data_reset_cancel": "destructive_commands_v1",
}
ROTATION_SCHEMA = "pm-credential-rotation/1.0.0"
COMMAND_EXPIRY_SECONDS = {
    "ota_install": 86_400,
    "rotate_device_credentials": 600,
    "format_storage_prepare": 600,
    "format_storage_commit": 600,
    "data_reset_prepare": 600,
    "data_reset_commit": 600,
    "data_reset_cancel": 600,
}
TERMINAL_STATES = {"succeeded", "failed", "expired", "cancelled", "superseded", "rolled_back"}
FORBIDDEN_EVIDENCE_KEYS = {
    "authorization",
    "confirmation_token",
    "cookie",
    "device_secret",
    "enrollment_token",
    "hmac_key",
    "password",
    "private_key",
    "secret",
    "token",
}


def _redact_prepare_token(command: DeviceCommand, marker: str) -> None:
    command.prepare_token_hash = None
    if "confirmation_token" in command.payload:
        command.payload = {**command.payload, "confirmation_token": marker}


def _invalidate_rotation_credential(credential: DeviceCredential, now: datetime) -> None:
    """Make an unactivated candidate permanently unusable without retaining its ciphertext."""

    credential.encrypted_secret = secrets.token_bytes(len(credential.encrypted_secret))
    credential.state = "revoked"
    credential.revoked_at = now


async def _terminalize_linked_ota_deployment(
    session: AsyncSession,
    command: DeviceCommand,
    *,
    now: datetime,
    result_code: str,
    evidence: Mapping[str, str | int | bool | None] | None = None,
) -> None:
    """Fail the deployment when its delivery command can no longer complete."""

    if command.command_type != "ota_install":
        return
    deployment_id = command.payload.get("deployment_id")
    if not isinstance(deployment_id, str):
        return
    deployment = await session.scalar(
        select(FirmwareDeployment).where(FirmwareDeployment.id == deployment_id).with_for_update()
    )
    if deployment is None or deployment.state in {
        "succeeded",
        "failed",
        "rolled_back",
        "cancelled",
    }:
        return
    deployment.state = "failed"
    deployment.completed_at = now
    deployment.evidence = {
        **deployment.evidence,
        "server_result_code": result_code,
        "command_id": command.id,
        "command_state": command.state,
        "delivery_attempt": command.attempt,
        **(evidence or {}),
    }


async def expire_rotation_credentials(session: AsyncSession, *, now: datetime | None = None) -> int:
    effective_now = now or datetime.now(UTC)
    candidates = (
        await session.scalars(
            select(DeviceCredential)
            .where(
                DeviceCredential.state.in_(("pending", "prepared")),
                DeviceCredential.overlap_expires_at.is_not(None),
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    credentials = [
        credential
        for credential in candidates
        if credential.overlap_expires_at is not None
        and aware_utc(credential.overlap_expires_at) <= effective_now
    ]
    for credential in credentials:
        _invalidate_rotation_credential(credential, effective_now)
        command_ids = tuple(
            command_id
            for command_id in (
                credential.prepare_command_id,
                credential.commit_command_id,
                credential.cancel_command_id,
            )
            if command_id is not None
        )
        if command_ids:
            commands = (
                await session.scalars(
                    select(DeviceCommand)
                    .where(
                        DeviceCommand.id.in_(command_ids),
                        DeviceCommand.state.not_in(TERMINAL_STATES),
                    )
                    .with_for_update()
                )
            ).all()
            for command in commands:
                command.state = "expired"
    return len(credentials)


async def expire_prepare_tokens(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Remove replayable prepare secrets once their authoritative expiry passes."""
    effective_now = now or datetime.now(UTC)
    commands = (
        await session.scalars(
            select(DeviceCommand)
            .where(
                DeviceCommand.prepare_token_hash.is_not(None),
                DeviceCommand.expires_at <= effective_now,
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    for command in commands:
        _redact_prepare_token(command, "[expired]")
        if command.state not in TERMINAL_STATES:
            command.state = "expired"
    return len(commands)


async def create_command(
    session: AsyncSession,
    *,
    device_id: str,
    command_type: str,
    issued_by_user_id: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    expires_in: timedelta | None = None,
    expires_at: datetime | None = None,
) -> tuple[DeviceCommand, str | None]:
    if command_type not in COMMAND_PERMISSIONS:
        raise ValueError("unsupported command type")
    existing = await session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.device_id == device_id,
            DeviceCommand.idempotency_key == idempotency_key,
        )
    )
    requested_payload = dict(payload or {})
    if existing is not None:
        comparable_payload = dict(existing.payload)
        existing_token = comparable_payload.pop("confirmation_token", None)
        if existing.command_type != command_type or comparable_payload != requested_payload:
            raise IntegrityConflict("idempotency key was already used for a different command")
        now = datetime.now(UTC)
        if (
            command_type in DESTRUCTIVE_PREPARE_TYPES
            and existing.prepare_token_hash is not None
            and aware_utc(existing.expires_at) <= now
        ):
            _redact_prepare_token(existing, "[expired]")
            existing.state = "expired"
            existing_token = None
        retry_token = (
            existing_token
            if command_type in DESTRUCTIVE_PREPARE_TYPES
            and existing.prepare_token_hash is not None
            and isinstance(existing_token, str)
            and existing.state in {"queued", "delivered", "succeeded"}
            else None
        )
        return existing, retry_token
    now = datetime.now(UTC)
    lifetime = expires_in or timedelta(seconds=COMMAND_EXPIRY_SECONDS.get(command_type, 600))
    command_expires_at = aware_utc(expires_at) if expires_at is not None else now + lifetime
    if command_expires_at <= now:
        raise IntegrityConflict("command expiry must be in the future")
    audit = AuditEvent(
        actor_user_id=issued_by_user_id,
        event_code="DEVICE_COMMAND_CREATED",
        target_type="device",
        target_id=device_id,
        details={"command_type": command_type},
    )
    session.add(audit)
    await session.flush()
    confirmation_token: str | None = None
    token_hash: str | None = None
    if command_type in DESTRUCTIVE_PREPARE_TYPES:
        confirmation_token = secrets.token_hex(16)
        token_hash = hashlib.sha256(confirmation_token.encode()).hexdigest()
        requested_payload["confirmation_token"] = confirmation_token
    command = DeviceCommand(
        device_id=device_id,
        command_type=command_type,
        issued_by_user_id=issued_by_user_id,
        issued_at=now,
        not_before=now,
        expires_at=command_expires_at,
        idempotency_key=idempotency_key,
        required_firmware_capability=COMMAND_CAPABILITIES.get(command_type),
        payload=requested_payload,
        state="queued",
        created_audit_id=audit.id,
        prepare_token_hash=token_hash,
    )
    session.add(command)
    await session.flush()
    return command, confirmation_token


async def validate_commit_token(
    session: AsyncSession,
    *,
    prepare_command_id: str,
    confirmation_token: str,
    commit_command_type: str,
) -> DeviceCommand:
    command = await session.scalar(
        select(DeviceCommand).where(DeviceCommand.id == prepare_command_id).with_for_update()
    )
    expected_prepare = COMMIT_PREPARE_TYPES.get(commit_command_type)
    if command is None or command.command_type != expected_prepare:
        raise NotFound("prepare command does not exist")
    if command.state != "succeeded" or command.prepare_token_hash is None:
        raise IntegrityConflict("prepare command has not completed successfully")
    if aware_utc(command.expires_at) <= datetime.now(UTC):
        raise IntegrityConflict("prepare confirmation token has expired")
    presented = hashlib.sha256(confirmation_token.encode()).hexdigest()
    if not secrets.compare_digest(command.prepare_token_hash, presented):
        raise IntegrityConflict("typed confirmation token does not match prepare evidence")
    command.prepare_token_hash = None
    command.payload = {**command.payload, "confirmation_token": "[consumed]"}
    return command


async def cancel_data_reset_prepare(
    session: AsyncSession, *, device_id: str, prepare_command_id: str
) -> DeviceCommand:
    command = await session.scalar(
        select(DeviceCommand)
        .where(
            DeviceCommand.id == prepare_command_id,
            DeviceCommand.device_id == device_id,
        )
        .with_for_update()
    )
    if command is None or command.command_type != "data_reset_prepare":
        raise NotFound("data reset prepare command does not exist")
    if command.state == "cancelled" and command.prepare_token_hash is None:
        return command
    if aware_utc(command.expires_at) <= datetime.now(UTC):
        _redact_prepare_token(command, "[expired]")
        command.state = "expired"
        raise IntegrityConflict("data reset prepare confirmation has expired")
    if command.state not in {"queued", "delivered", "succeeded"}:
        raise IntegrityConflict("data reset prepare command is no longer cancellable")
    _redact_prepare_token(command, "[cancelled]")
    command.state = "cancelled"
    return command


async def deliver_commands(
    session: AsyncSession,
    device_id: str,
    limit: int = 4,
    *,
    settings: Settings | None = None,
    response_byte_budget: int | None = None,
    maximum_single_envelope_bytes: int | None = None,
) -> list[CommandEnvelope]:
    now = datetime.now(UTC)
    # This session intentionally disables autoflush. Persist authenticated result transitions
    # before querying deliverable rows so a just-completed prepare cannot be redelivered.
    await session.flush()
    await expire_rotation_credentials(session, now=now)
    await session.flush()
    expired = (
        await session.scalars(
            select(DeviceCommand)
            .where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.state.in_(("queued", "delivered")),
                DeviceCommand.expires_at <= now,
            )
            # Keep the same command-then-deployment lock order used by result
            # application before terminalizing a linked OTA deployment.
            .with_for_update(skip_locked=True)
        )
    ).all()
    for command in expired:
        command.state = "expired"
        command.last_result = {
            "result_code": "COMMAND_EXPIRED",
            "evidence": {"delivery_attempt": command.attempt},
        }
        if command.prepare_token_hash is not None:
            _redact_prepare_token(command, "[expired]")
        await _terminalize_linked_ota_deployment(
            session,
            command,
            now=now,
            result_code="COMMAND_EXPIRED",
        )
    commands = (
        await session.scalars(
            select(DeviceCommand)
            .where(
                DeviceCommand.device_id == device_id,
                DeviceCommand.state.in_(("queued", "delivered")),
                DeviceCommand.not_before <= now,
                DeviceCommand.expires_at > now,
            )
            .order_by(DeviceCommand.issued_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    envelopes: list[CommandEnvelope] = []
    serialized_bytes = 0
    for command in commands:
        delivered_payload = command.payload
        if command.command_type == "rotate_device_credentials":
            candidate = await session.scalar(
                select(DeviceCredential).where(
                    DeviceCredential.device_id == device_id,
                    DeviceCredential.prepare_command_id == command.id,
                )
            )
            if candidate is not None:
                if (
                    settings is None
                    or candidate.state != "pending"
                    or candidate.rotation_id is None
                    or candidate.overlap_expires_at is None
                    or aware_utc(candidate.overlap_expires_at) <= now
                ):
                    command.state = "expired"
                    _invalidate_rotation_credential(candidate, now)
                    continue
                candidate_secret = bytearray(
                    decrypt_secret(
                        settings.master_key,
                        candidate.encrypted_secret,
                        context=device_id.encode(),
                    )
                )
                try:
                    delivered_payload = {
                        "schema": ROTATION_SCHEMA,
                        "rotation_id": candidate.rotation_id,
                        "device_secret_hex": candidate_secret.hex(),
                        "credential_fingerprint": candidate.fingerprint,
                        "overlap_expires_at": aware_utc(candidate.overlap_expires_at)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                finally:
                    candidate_secret[:] = b"\0" * len(candidate_secret)
        if command.attempt >= MAX_COMMAND_DELIVERY_ATTEMPT:
            command.state = "failed"
            command.last_result = {
                "result_code": "DELIVERY_ATTEMPTS_EXHAUSTED",
                "evidence": {"delivery_attempt": command.attempt},
            }
            await _terminalize_linked_ota_deployment(
                session,
                command,
                now=now,
                result_code="DELIVERY_ATTEMPTS_EXHAUSTED",
            )
            continue
        next_attempt = command.attempt + 1
        envelope = CommandEnvelope(
            command_id=command.id,
            command_type=command.command_type,
            not_before=command.not_before,
            expires_at=command.expires_at,
            attempt=next_attempt,
            idempotency_key=command.idempotency_key,
            required_firmware_capability=command.required_firmware_capability,
            payload=delivered_payload,
        )
        # Match the response serializer exactly. Replacing `commands:[]` with
        # one envelope increases the response by exactly the serialized
        # envelope length; each later envelope adds one comma byte as well.
        envelope_bytes = len(
            orjson.dumps(envelope.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
        )
        if (
            maximum_single_envelope_bytes is not None
            and envelope_bytes > maximum_single_envelope_bytes
        ):
            size_evidence = {
                "serialized_envelope_bytes": envelope_bytes,
                "maximum_envelope_bytes": maximum_single_envelope_bytes,
            }
            command.state = "failed"
            command.last_result = {
                "result_code": "DELIVERY_RESPONSE_TOO_LARGE",
                "evidence": size_evidence,
            }
            await _terminalize_linked_ota_deployment(
                session,
                command,
                now=now,
                result_code="DELIVERY_RESPONSE_TOO_LARGE",
                evidence=size_evidence,
            )
            continue
        incremental_bytes = envelope_bytes + (1 if envelopes else 0)
        if (
            response_byte_budget is not None
            and serialized_bytes + incremental_bytes > response_byte_budget
        ):
            continue
        command.attempt = next_attempt
        command.state = "delivered"
        session.add(
            DeviceCommandAttempt(command_id=command.id, attempt=command.attempt, delivered_at=now)
        )
        envelopes.append(envelope)
        serialized_bytes += incremental_bytes
    return envelopes


async def apply_command_results(
    session: AsyncSession,
    device_id: str,
    results: list[CommandResult],
    *,
    authenticated_credential_id: str | None = None,
) -> None:
    for result in results:
        command = await session.scalar(
            select(DeviceCommand)
            .where(DeviceCommand.id == result.command_id, DeviceCommand.device_id == device_id)
            .with_for_update()
        )
        if command is None:
            raise NotFound("command result does not belong to this device")
        if command.state in TERMINAL_STATES:
            if command.state != result.state:
                server_result_code = (command.last_result or {}).get("result_code")
                if command.command_type == "ota_install" and server_result_code in {
                    "COMMAND_EXPIRED",
                    "DELIVERY_ATTEMPTS_EXHAUSTED",
                    "DELIVERY_RESPONSE_TOO_LARGE",
                }:
                    # Server expiry/failure is authoritative. A device that
                    # parsed the final delivery may still report its local
                    # outcome on the next heartbeat; accept that heartbeat
                    # without reviving the terminal server decision.
                    attempt = await session.scalar(
                        select(DeviceCommandAttempt).where(
                            DeviceCommandAttempt.command_id == command.id,
                            DeviceCommandAttempt.attempt == command.attempt,
                        )
                    )
                    if attempt is not None:
                        attempt.result_at = datetime.now(UTC)
                        attempt.result_code = f"IGNORED_AFTER_{command.state.upper()}"
                    continue
                raise IntegrityConflict("terminal command result cannot change")
            continue
        if result.progress_percent < command.progress_percent:
            raise IntegrityConflict("command progress cannot move backward")
        for evidence_key in result.evidence:
            normalized_key = evidence_key.lower()
            if normalized_key in FORBIDDEN_EVIDENCE_KEYS or normalized_key.endswith(
                ("_password", "_secret", "_token", "_private_key", "_hmac_key")
            ):
                raise IntegrityConflict("command result evidence contains a forbidden secret field")
        rotation_credential: DeviceCredential | None = None
        if command.command_type == "rotate_device_credentials":
            rotation_id = command.payload.get("rotation_id")
            rotation_credential = await session.scalar(
                select(DeviceCredential)
                .where(
                    DeviceCredential.device_id == device_id,
                    DeviceCredential.rotation_id == rotation_id,
                )
                .with_for_update()
            )
            if rotation_credential is None:
                raise IntegrityConflict("credential rotation command is not bound to a candidate")
            is_prepare = rotation_credential.prepare_command_id == command.id
            is_commit = rotation_credential.commit_command_id == command.id
            is_cancel = rotation_credential.cancel_command_id == command.id
            if sum((is_prepare, is_commit, is_cancel)) != 1:
                raise IntegrityConflict("credential rotation command phase is ambiguous")
            overlap_expires_at = rotation_credential.overlap_expires_at
            if overlap_expires_at is None:
                raise IntegrityConflict("credential rotation has no bounded overlap")
            if is_prepare:
                expected_payload: dict[str, Any] = {
                    "schema": ROTATION_SCHEMA,
                    "rotation_id": rotation_credential.rotation_id,
                    "credential_fingerprint": rotation_credential.fingerprint,
                    "overlap_expires_at": aware_utc(overlap_expires_at)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            elif is_commit:
                expected_payload = {
                    "schema": ROTATION_SCHEMA,
                    "rotation_id": rotation_credential.rotation_id,
                    "credential_fingerprint": rotation_credential.fingerprint,
                }
            else:
                expected_payload = {
                    "schema": ROTATION_SCHEMA,
                    "rotation_id": rotation_credential.rotation_id,
                    "cancelled": True,
                }
            if command.payload != expected_payload:
                raise IntegrityConflict("credential rotation payload is not canonical")
            if result.state == "succeeded" and is_prepare:
                active_credential = await session.scalar(
                    select(DeviceCredential).where(
                        DeviceCredential.id == authenticated_credential_id,
                        DeviceCredential.device_id == device_id,
                        DeviceCredential.state == "active",
                        DeviceCredential.revoked_at.is_(None),
                    )
                )
                expected_evidence = {
                    "rotation_id": rotation_credential.rotation_id,
                    "credential_fingerprint": rotation_credential.fingerprint,
                    "ready": True,
                }
                if (
                    active_credential is None
                    or active_credential.id == rotation_credential.id
                    or rotation_credential.state != "pending"
                    or aware_utc(overlap_expires_at) <= datetime.now(UTC)
                    or result.evidence != expected_evidence
                ):
                    raise IntegrityConflict(
                        "credential rotation prepare must be authenticated by the active key"
                    )
                rotation_credential.state = "prepared"
                rotation_credential.prepared_at = datetime.now(UTC)
                commit, _unused = await create_command(
                    session,
                    device_id=device_id,
                    command_type="rotate_device_credentials",
                    issued_by_user_id=command.issued_by_user_id,
                    idempotency_key=f"credential-rotation-commit:{rotation_credential.rotation_id}",
                    payload={
                        "schema": ROTATION_SCHEMA,
                        "rotation_id": rotation_credential.rotation_id,
                        "credential_fingerprint": rotation_credential.fingerprint,
                    },
                    expires_at=overlap_expires_at,
                )
                rotation_credential.commit_command_id = commit.id
            elif result.state == "succeeded" and is_commit:
                expected_evidence = {
                    "rotation_id": rotation_credential.rotation_id,
                    "credential_fingerprint": rotation_credential.fingerprint,
                    "activated": True,
                }
                if (
                    authenticated_credential_id != rotation_credential.id
                    or rotation_credential.state != "prepared"
                    or result.evidence != expected_evidence
                ):
                    raise IntegrityConflict(
                        "credential rotation commit must be authenticated by the candidate key"
                    )
                now = datetime.now(UTC)
                superseded = (
                    await session.scalars(
                        select(DeviceCredential)
                        .where(
                            DeviceCredential.device_id == device_id,
                            DeviceCredential.id != rotation_credential.id,
                            DeviceCredential.state.in_(("active", "retiring")),
                            DeviceCredential.revoked_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).all()
                for old_credential in superseded:
                    _invalidate_rotation_credential(old_credential, now)
                rotation_credential.state = "active"
                rotation_credential.activated_at = now
            elif result.state == "succeeded" and is_cancel:
                active_credential = await session.scalar(
                    select(DeviceCredential).where(
                        DeviceCredential.id == authenticated_credential_id,
                        DeviceCredential.device_id == device_id,
                        DeviceCredential.state == "active",
                        DeviceCredential.revoked_at.is_(None),
                    )
                )
                if active_credential is None or result.evidence != {
                    "rotation_id": rotation_credential.rotation_id,
                    "cancelled": True,
                }:
                    raise IntegrityConflict(
                        "credential rotation cancellation must use the active key"
                    )
                _invalidate_rotation_credential(rotation_credential, datetime.now(UTC))
            elif result.state in TERMINAL_STATES - {"succeeded"}:
                _invalidate_rotation_credential(rotation_credential, datetime.now(UTC))
        if command.command_type == "format_storage_prepare" and result.state == "succeeded":
            acknowledged_lost = result.evidence.get("acknowledged_records_lost")
            unacknowledged_lost = result.evidence.get("unacknowledged_records_lost")
            if (
                set(result.evidence)
                != {
                    "prepare_command_id",
                    "acknowledged_records_lost",
                    "unacknowledged_records_lost",
                    "ready",
                }
                or result.evidence.get("prepare_command_id") != command.id
                or isinstance(acknowledged_lost, bool)
                or not isinstance(acknowledged_lost, int)
                or acknowledged_lost < 0
                or isinstance(unacknowledged_lost, bool)
                or not isinstance(unacknowledged_lost, int)
                or unacknowledged_lost < 0
                or result.evidence.get("ready") is not True
            ):
                raise IntegrityConflict(
                    "storage format prepare completion evidence is inconsistent"
                )
        if command.command_type == "format_storage_commit" and result.state == "succeeded":
            prepare_id = command.payload.get("prepare_command_id")
            prepare = (
                await session.get(DeviceCommand, prepare_id)
                if isinstance(prepare_id, str)
                else None
            )
            prepare_evidence = (prepare.last_result or {}).get("evidence") if prepare else None
            if (
                not isinstance(prepare_evidence, dict)
                or set(result.evidence)
                != {
                    "prepare_command_id",
                    "acknowledged_records_lost",
                    "unacknowledged_records_lost",
                    "formatted",
                }
                or result.evidence.get("prepare_command_id") != prepare_id
                or result.evidence.get("acknowledged_records_lost")
                != prepare_evidence.get("acknowledged_records_lost")
                or result.evidence.get("unacknowledged_records_lost")
                != prepare_evidence.get("unacknowledged_records_lost")
                or result.evidence.get("formatted") is not True
            ):
                raise IntegrityConflict("storage format commit completion evidence is inconsistent")
        if command.command_type == "data_reset_prepare" and result.state == "succeeded":
            generation = command.payload.get("reset_generation")
            server_floor = command.payload.get("server_sequence_floor")
            evidence = result.evidence
            evidence_floor = evidence.get("sequence_floor")
            if (
                set(evidence)
                != {
                    "prepare_command_id",
                    "reset_generation",
                    "server_sequence_floor",
                    "sequence_floor",
                    "ready",
                }
                or evidence.get("prepare_command_id") != command.id
                or evidence.get("reset_generation") != generation
                or evidence.get("server_sequence_floor") != server_floor
                or isinstance(evidence_floor, bool)
                or not isinstance(evidence_floor, int)
                or not isinstance(server_floor, int)
                or evidence_floor < server_floor
                or evidence.get("ready") is not True
            ):
                raise IntegrityConflict("data reset prepare completion evidence is inconsistent")
        if command.command_type == "data_reset_commit" and result.state == "succeeded":
            result_generation = result.evidence.get("reset_generation")
            result_floor = result.evidence.get("sequence_floor")
            if (
                set(result.evidence) != {"prepare_command_id", "reset_generation", "sequence_floor"}
                or result.evidence.get("prepare_command_id")
                != command.payload.get("prepare_command_id")
                or isinstance(result_generation, bool)
                or not isinstance(result_generation, int)
                or result_generation != command.payload.get("reset_generation")
                or isinstance(result_floor, bool)
                or not isinstance(result_floor, int)
                or result_floor != command.payload.get("sequence_floor")
            ):
                raise IntegrityConflict("data reset commit completion evidence is inconsistent")
        if (
            command.command_type == "data_reset_cancel"
            and result.state == "succeeded"
            and result.evidence
            != {
                "prepare_command_id": command.payload.get("prepare_command_id"),
                "cancelled": True,
            }
        ):
            raise IntegrityConflict("data reset cancel completion evidence is inconsistent")
        command.state = result.state
        command.progress_percent = result.progress_percent
        command.last_result = {"result_code": result.result_code, "evidence": result.evidence}
        if (
            command.command_type in DESTRUCTIVE_PREPARE_TYPES
            and result.state in TERMINAL_STATES - {"succeeded"}
            and command.prepare_token_hash is not None
        ):
            _redact_prepare_token(command, "[invalidated]")
        if command.command_type == "data_reset_commit" and result.state == "succeeded":
            generation = command.payload.get("reset_generation")
            floor = result.evidence.get("sequence_floor")
            device = await session.scalar(
                select(Device).where(Device.id == device_id).with_for_update()
            )
            if (
                device is None
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation != device.reset_generation + 1
                or isinstance(floor, bool)
                or not isinstance(floor, int)
                or floor < device.maximum_sequence
            ):
                raise IntegrityConflict("data reset completion evidence is inconsistent")
            device.reset_generation = generation
            device.maximum_sequence = floor
            device.contiguous_ack = floor
        if command.command_type == "ota_install":
            deployment_id = command.payload.get("deployment_id")
            deployment = (
                await session.get(FirmwareDeployment, deployment_id)
                if isinstance(deployment_id, str)
                else None
            )
            if deployment is not None:
                if result.state == "succeeded":
                    deployment.state = "validating"
                    deployment.progress_percent = max(deployment.progress_percent, 90)
                elif result.state in ("failed", "rolled_back"):
                    deployment.state = result.state
                    deployment.completed_at = datetime.now(UTC)
                deployment.evidence = {
                    **deployment.evidence,
                    "device_result_code": result.result_code,
                    "device_result_evidence": result.evidence,
                }
        attempt = await session.scalar(
            select(DeviceCommandAttempt).where(
                DeviceCommandAttempt.command_id == command.id,
                DeviceCommandAttempt.attempt == command.attempt,
            )
        )
        if attempt is not None:
            attempt.result_at = datetime.now(UTC)
            attempt.result_code = result.result_code
            attempt.evidence = result.evidence
