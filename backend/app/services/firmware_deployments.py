from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    AuditEvent,
    DeviceCommand,
    FirmwareDeployment,
    FirmwareDeploymentBatch,
    FirmwareLifecycleSetting,
    FirmwareRelease,
    aware_utc,
)

logger = structlog.get_logger()

ACTIVE_FIRMWARE_DEPLOYMENT_STATES = frozenset(
    {"staged", "queued", "downloading", "rebooting", "validating"}
)
TERMINAL_FIRMWARE_DEPLOYMENT_STATES = frozenset(
    {"succeeded", "failed", "rolled_back", "timed_out", "cancelled"}
)
ACTIVE_OTA_COMMAND_STATES = frozenset(
    {"queued", "delivered", "accepted", "running", "awaiting_reboot", "awaiting_heartbeat"}
)
DEPLOYMENT_TIMEOUTS = {
    "staged": timedelta(hours=24),
    "queued": timedelta(minutes=30),
    "downloading": timedelta(minutes=20),
    "rebooting": timedelta(minutes=10),
    "validating": timedelta(minutes=10),
}
ARTIFACT_RECOVERY_GRACE = timedelta(minutes=5)
ESP_IMAGE_HEADER_SIZE = 24
ESP_SEGMENT_HEADER_SIZE = 8
ESP32S3_CHIP_ID = 9
ESP_APP_DESC_MAGIC = 0xABCD5432
ESP_APP_DESC_SIZE = 256
ESP_APP_DESC_VERSION_OFFSET = 16
ESP_APP_DESC_PROJECT_OFFSET = 48
ESP_APP_DESC_TEXT_SIZE = 32
ESP_APP_DESC_ELF_SHA256_OFFSET = 144


@dataclass(frozen=True, slots=True)
class ESP32S3AppIdentity:
    semantic_version: str
    project_name: str
    firmware_build_id: str


@dataclass(frozen=True, slots=True)
class FirmwareOTAGraphLocks:
    """Rows locked in the one authoritative OTA lifecycle order."""

    commands: tuple[DeviceCommand, ...]
    releases: tuple[FirmwareRelease, ...]
    batches: tuple[FirmwareDeploymentBatch, ...]
    deployments: tuple[FirmwareDeployment, ...]


def _nul_padded_ascii(field: bytes, *, label: str) -> str:
    terminator = field.find(b"\0")
    if terminator <= 0 or any(field[terminator + 1 :]):
        raise ValueError(f"ESP application {label} is not unambiguous NUL-padded text")
    try:
        return field[:terminator].decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError(f"ESP application {label} is not ASCII") from exc


def parse_esp32s3_app_identity(image: bytes) -> ESP32S3AppIdentity:
    """Extract the immutable ESP-IDF app descriptor identity from an OTA image."""

    descriptor_offset = ESP_IMAGE_HEADER_SIZE + ESP_SEGMENT_HEADER_SIZE
    minimum_size = descriptor_offset + ESP_APP_DESC_SIZE
    if len(image) < minimum_size or image[0] != 0xE9 or not 1 <= image[1] <= 16:
        raise ValueError("firmware is not a complete ESP application image")
    chip_id = int.from_bytes(image[12:14], "little")
    if chip_id != ESP32S3_CHIP_ID:
        raise ValueError("firmware image does not target ESP32-S3")
    first_segment_size = int.from_bytes(
        image[ESP_IMAGE_HEADER_SIZE + 4 : descriptor_offset], "little"
    )
    if first_segment_size < ESP_APP_DESC_SIZE or descriptor_offset + first_segment_size > len(
        image
    ):
        raise ValueError("firmware first application segment is truncated")
    descriptor = image[descriptor_offset : descriptor_offset + ESP_APP_DESC_SIZE]
    if int.from_bytes(descriptor[:4], "little") != ESP_APP_DESC_MAGIC:
        raise ValueError("firmware ESP application descriptor magic is invalid")
    semantic_version = _nul_padded_ascii(
        descriptor[
            ESP_APP_DESC_VERSION_OFFSET : ESP_APP_DESC_VERSION_OFFSET + ESP_APP_DESC_TEXT_SIZE
        ],
        label="version",
    )
    project_name = _nul_padded_ascii(
        descriptor[
            ESP_APP_DESC_PROJECT_OFFSET : ESP_APP_DESC_PROJECT_OFFSET + ESP_APP_DESC_TEXT_SIZE
        ],
        label="project name",
    )
    build_bytes = descriptor[ESP_APP_DESC_ELF_SHA256_OFFSET : ESP_APP_DESC_ELF_SHA256_OFFSET + 32]
    if len(build_bytes) != 32 or build_bytes in {b"\0" * 32, b"\xff" * 32}:
        raise ValueError("firmware ESP application build ID is unavailable")
    return ESP32S3AppIdentity(
        semantic_version=semantic_version,
        project_name=project_name,
        firmware_build_id=build_bytes.hex(),
    )


def _fsync_directory(directory: Path) -> None:
    """Persist a directory-entry change where the host exposes directory fsync."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(descriptor)


def durable_write_bytes(path: Path, data: bytes) -> None:
    """Write and sync a unique pending artifact before metadata may reference it."""

    with path.open("xb") as artifact:
        artifact.write(data)
        artifact.flush()
        os.fsync(artifact.fileno())
    _fsync_directory(path.parent)


def durable_replace(source: Path, target: Path) -> None:
    """Same-volume replace followed by content and directory durability barriers."""

    os.replace(source, target)
    with target.open("r+b") as artifact:
        os.fsync(artifact.fileno())
    _fsync_directory(target.parent)


def durable_unlink(path: Path, *, missing_ok: bool = False) -> None:
    """Remove one exact artifact and persist the parent directory entry change."""

    try:
        path.unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise
        return
    _fsync_directory(path.parent)


def firmware_artifact_matches(path: Path, expected_size: int, expected_sha256: str) -> bool:
    """Verify one regular artifact without trusting only its directory entry."""

    if path.is_symlink():
        return False
    try:
        digest = hashlib.sha256()
        with path.open("rb") as artifact:
            initial = os.fstat(artifact.fileno())
            if initial.st_size != expected_size:
                return False
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
            final = os.fstat(artifact.fileno())
    except (FileNotFoundError, IsADirectoryError, OSError):
        return False
    return (
        initial.st_size == final.st_size
        and initial.st_mtime_ns == final.st_mtime_ns
        and digest.hexdigest() == expected_sha256
    )


def _stored_manifest(deployment: FirmwareDeployment) -> dict[str, Any]:
    manifest = deployment.evidence.get("manifest")
    actor = deployment.evidence.get("issued_by_user_id")
    if not isinstance(manifest, dict) or not isinstance(actor, str) or not actor:
        raise ValueError("staged firmware deployment evidence is incomplete")
    return manifest


async def queue_staged_firmware_deployment(
    session: AsyncSession, deployment: FirmwareDeployment
) -> None:
    """Release one held deployment without changing its signed manifest."""

    if deployment.state != "staged":
        return
    from .commands import create_command

    manifest = _stored_manifest(deployment)
    actor = deployment.evidence["issued_by_user_id"]
    assert isinstance(actor, str)
    deployment.state = "queued"
    deployment.updated_at = datetime.now(UTC)
    await create_command(
        session,
        device_id=deployment.device_id,
        command_type="ota_install",
        issued_by_user_id=actor,
        idempotency_key=f"ota:{deployment.id}",
        payload=manifest,
    )


async def recalculate_firmware_batch(
    session: AsyncSession, batch_id: str | None, *, now: datetime | None = None
) -> FirmwareDeploymentBatch | None:
    if batch_id is None:
        return None
    # Application sessions intentionally disable autoflush. Make every state
    # transition visible to the aggregate query before deriving the batch.
    await session.flush()
    batch = await session.scalar(
        select(FirmwareDeploymentBatch)
        .where(FirmwareDeploymentBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        return None
    states = list(
        (
            await session.scalars(
                select(FirmwareDeployment.state).where(FirmwareDeployment.batch_id == batch_id)
            )
        ).all()
    )
    if not states:
        return batch
    if any(state in ACTIVE_FIRMWARE_DEPLOYMENT_STATES for state in states):
        batch.state = "in_progress"
        batch.completed_at = None
    elif all(state == "succeeded" for state in states):
        batch.state = "succeeded"
        batch.completed_at = now or datetime.now(UTC)
    elif any(state == "succeeded" for state in states):
        batch.state = "partial"
        batch.completed_at = now or datetime.now(UTC)
    elif all(state == "cancelled" for state in states):
        batch.state = "cancelled"
        batch.completed_at = now or datetime.now(UTC)
    else:
        batch.state = "failed"
        batch.completed_at = now or datetime.now(UTC)
    batch.updated_at = now or datetime.now(UTC)
    return batch


async def lock_active_ota_commands_for_deployments(
    session: AsyncSession,
    deployments: list[FirmwareDeployment] | tuple[FirmwareDeployment, ...],
) -> tuple[DeviceCommand, ...]:
    """Lock active OTA commands before callers lock their linked deployments."""

    deployment_ids = {deployment.id for deployment in deployments}
    device_ids = {deployment.device_id for deployment in deployments}
    if not deployment_ids or not device_ids:
        return ()
    commands = list(
        (
            await session.scalars(
                select(DeviceCommand)
                .where(
                    DeviceCommand.device_id.in_(device_ids),
                    DeviceCommand.command_type == "ota_install",
                    DeviceCommand.state.in_(ACTIVE_OTA_COMMAND_STATES),
                )
                .order_by(DeviceCommand.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    return tuple(
        command for command in commands if command.payload.get("deployment_id") in deployment_ids
    )


async def lock_firmware_ota_graph(
    session: AsyncSession,
    deployments: Sequence[FirmwareDeployment],
    *,
    lock_commands: bool,
    additional_release_ids: Sequence[str] = (),
) -> FirmwareOTAGraphLocks:
    """Lock an OTA graph as command -> release -> batch -> deployment.

    Every lifecycle mutator uses this order. Callers that already hold the
    relevant command row (device result/expiry processing) pass
    ``lock_commands=False``; no caller may first lock a deployment and then use
    this helper.
    """

    expected_links = {
        deployment.id: (
            deployment.firmware_release_id,
            deployment.batch_id,
            deployment.device_id,
        )
        for deployment in deployments
    }
    deployment_ids = sorted(expected_links)
    extra_release_ids = set(additional_release_ids)
    if not deployment_ids and not extra_release_ids:
        return FirmwareOTAGraphLocks((), (), (), ())
    commands = (
        await lock_active_ota_commands_for_deployments(session, tuple(deployments))
        if lock_commands and deployment_ids
        else ()
    )
    release_ids = sorted(
        {deployment.firmware_release_id for deployment in deployments} | extra_release_ids
    )
    releases = tuple(
        (
            await session.scalars(
                select(FirmwareRelease)
                .where(FirmwareRelease.id.in_(release_ids))
                .order_by(FirmwareRelease.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if {release.id for release in releases} != set(release_ids):
        raise RuntimeError("firmware deployment release identity is missing")
    batch_ids = sorted(
        {deployment.batch_id for deployment in deployments if deployment.batch_id is not None}
    )
    batches = (
        tuple(
            (
                await session.scalars(
                    select(FirmwareDeploymentBatch)
                    .where(FirmwareDeploymentBatch.id.in_(batch_ids))
                    .order_by(FirmwareDeploymentBatch.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if batch_ids
        else ()
    )
    if {batch.id for batch in batches} != set(batch_ids):
        raise RuntimeError("firmware deployment batch identity is missing")
    locked_deployments = (
        tuple(
            (
                await session.scalars(
                    select(FirmwareDeployment)
                    .where(FirmwareDeployment.id.in_(deployment_ids))
                    .order_by(FirmwareDeployment.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if deployment_ids
        else ()
    )
    if {deployment.id for deployment in locked_deployments} != set(deployment_ids):
        raise RuntimeError("firmware deployment identity changed while acquiring locks")
    if any(
        expected_links[deployment.id]
        != (
            deployment.firmware_release_id,
            deployment.batch_id,
            deployment.device_id,
        )
        for deployment in locked_deployments
    ):
        raise RuntimeError("firmware deployment references changed while acquiring locks")
    if lock_commands and locked_deployments:
        # A staged promotion can insert its first OTA command while this caller
        # is waiting for the release lock. Re-read without acquiring a late
        # command lock and fail closed if that race won; every command present
        # at the original lock boundary remains protected by its row lock.
        current_commands = list(
            (
                await session.scalars(
                    select(DeviceCommand).where(
                        DeviceCommand.device_id.in_(
                            {deployment.device_id for deployment in locked_deployments}
                        ),
                        DeviceCommand.command_type == "ota_install",
                        DeviceCommand.state.in_(ACTIVE_OTA_COMMAND_STATES),
                    )
                )
            ).all()
        )
        locked_deployment_ids = {deployment.id for deployment in locked_deployments}
        current_command_ids = {
            command.id
            for command in current_commands
            if command.payload.get("deployment_id") in locked_deployment_ids
        }
        if current_command_ids != {command.id for command in commands}:
            raise RuntimeError("active OTA command references changed while acquiring locks")
    return FirmwareOTAGraphLocks(commands, releases, batches, locked_deployments)


async def halt_staged_siblings_after_failure(
    session: AsyncSession, deployment: FirmwareDeployment, *, now: datetime
) -> None:
    if deployment.batch_id is None:
        return
    siblings = list(
        (
            await session.scalars(
                select(FirmwareDeployment)
                .where(
                    FirmwareDeployment.batch_id == deployment.batch_id,
                    FirmwareDeployment.id != deployment.id,
                    FirmwareDeployment.state == "staged",
                )
                .order_by(FirmwareDeployment.id)
                .with_for_update()
            )
        ).all()
    )
    for sibling in siblings:
        sibling.state = "cancelled"
        sibling.completed_at = now
        sibling.updated_at = now
        sibling.error_code = "OTA_STAGED_HALTED"
        sibling.error_message = "Staged rollout stopped after another sensor failed"
        sibling.evidence = {
            **sibling.evidence,
            "server_result_code": "OTA_STAGED_HALTED",
            "blocked_by_deployment_id": deployment.id,
        }


async def advance_next_staged_firmware_deployment(
    session: AsyncSession, release_id: str
) -> FirmwareDeployment | None:
    """Advance exactly one staged target after the prior target proves success."""

    # Serialize promotion for a release. Concurrent post-reboot heartbeats must
    # never release two staged sensors at once.
    await session.scalar(
        select(FirmwareRelease.id).where(FirmwareRelease.id == release_id).with_for_update()
    )
    active = await session.scalar(
        select(FirmwareDeployment.id)
        .where(
            FirmwareDeployment.firmware_release_id == release_id,
            FirmwareDeployment.state.in_(tuple(ACTIVE_FIRMWARE_DEPLOYMENT_STATES - {"staged"})),
        )
        .limit(1)
    )
    if active is not None:
        return None
    preflight_staged = await session.scalar(
        select(FirmwareDeployment)
        .where(
            FirmwareDeployment.firmware_release_id == release_id,
            FirmwareDeployment.state == "staged",
        )
        .order_by(FirmwareDeployment.created_at, FirmwareDeployment.id)
        .limit(1)
    )
    if preflight_staged is None:
        return None
    if preflight_staged.batch_id is not None:
        locked_batch_id = await session.scalar(
            select(FirmwareDeploymentBatch.id)
            .where(FirmwareDeploymentBatch.id == preflight_staged.batch_id)
            .with_for_update()
        )
        if locked_batch_id is None:
            raise RuntimeError("staged firmware deployment batch identity is missing")
    staged = await session.scalar(
        select(FirmwareDeployment)
        .where(
            FirmwareDeployment.id == preflight_staged.id,
            FirmwareDeployment.state == "staged",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if staged is None:
        return None
    await queue_staged_firmware_deployment(session, staged)
    return staged


async def reconcile_firmware_version_heartbeat(
    session: AsyncSession,
    *,
    device_id: str,
    firmware_version: str,
    firmware_build_id: str | None,
    now: datetime | None = None,
    locked_graph: FirmwareOTAGraphLocks | None = None,
) -> tuple[str, ...]:
    """Confirm OTA only from the exact post-reboot version and ELF build identity."""

    completed_at = now or datetime.now(UTC)
    # Command results are applied earlier in the same authenticated heartbeat.
    # Flush their validating transition before selecting confirmation work.
    await session.flush()
    # A joined FOR UPDATE lets PostgreSQL choose a plan-dependent tuple-lock
    # order. Preflight without locks, then use the shared command -> release ->
    # batch -> deployment order. Device result processing already holds its
    # command row and takes the same remaining order before reaching here.
    if locked_graph is None:
        preflight = list(
            (
                await session.scalars(
                    select(FirmwareDeployment)
                    .where(
                        FirmwareDeployment.device_id == device_id,
                        FirmwareDeployment.state == "validating",
                    )
                    .order_by(FirmwareDeployment.id)
                )
            ).all()
        )
        graph = await lock_firmware_ota_graph(session, preflight, lock_commands=True)
    else:
        graph = locked_graph
    deployments = [
        deployment
        for deployment in graph.deployments
        if deployment.device_id == device_id and deployment.state == "validating"
    ]
    releases = {release.id: release for release in graph.releases}
    completed_release_ids: list[str] = []
    touched_batch_ids: list[str | None] = []
    for deployment in deployments:
        release = releases.get(deployment.firmware_release_id)
        if release is None:
            raise RuntimeError("firmware deployment release identity is missing")
        version_matches = release.semantic_version.removeprefix(
            "v"
        ) == firmware_version.removeprefix("v")
        expected_build_id = release.firmware_build_id
        build_matches = (
            expected_build_id is not None
            and firmware_build_id is not None
            and re.fullmatch(r"[0-9a-f]{64}", expected_build_id) is not None
            and re.fullmatch(r"[0-9a-f]{64}", firmware_build_id) is not None
            and hmac.compare_digest(expected_build_id, firmware_build_id)
        )
        if not version_matches or not build_matches:
            error_code = (
                "OTA_VERSION_NOT_CONFIRMED" if not version_matches else "OTA_BUILD_ID_NOT_CONFIRMED"
            )
            deployment.state = "failed"
            deployment.completed_at = completed_at
            deployment.updated_at = completed_at
            deployment.error_code = error_code
            deployment.error_message = (
                "The sensor did not report the exact target firmware identity after reboot"
            )
            deployment.evidence = {
                **deployment.evidence,
                "expected_firmware_version": release.semantic_version,
                "post_reboot_firmware_version": firmware_version,
                "expected_firmware_build_id": expected_build_id,
                "post_reboot_firmware_build_id": firmware_build_id,
                "post_reboot_observed_at": completed_at.isoformat(),
                "server_result_code": error_code,
            }
            await halt_staged_siblings_after_failure(session, deployment, now=completed_at)
            logger.warning(
                "ota_identity_not_confirmed",
                deployment_id=deployment.id,
                device_id=device_id,
                release_id=release.id,
                expected_version=release.semantic_version,
                reported_version=firmware_version,
                expected_build_id=expected_build_id,
                reported_build_id=firmware_build_id,
                error_code=error_code,
            )
            touched_batch_ids.append(deployment.batch_id)
            continue
        deployment.state = "succeeded"
        deployment.progress_percent = 100
        deployment.completed_at = completed_at
        deployment.updated_at = completed_at
        deployment.error_code = None
        deployment.error_message = None
        deployment.evidence = {
            **deployment.evidence,
            "post_reboot_firmware_version": firmware_version,
            "expected_firmware_build_id": expected_build_id,
            "post_reboot_firmware_build_id": firmware_build_id,
            "post_reboot_confirmed_at": completed_at.isoformat(),
        }
        completed_release_ids.append(release.id)
        touched_batch_ids.append(deployment.batch_id)
        logger.info(
            "ota_version_confirmed",
            deployment_id=deployment.id,
            device_id=device_id,
            release_id=release.id,
            expected_version=release.semantic_version,
            reported_version=firmware_version,
            expected_build_id=expected_build_id,
            reported_build_id=firmware_build_id,
        )
    await session.flush()
    for release_id in dict.fromkeys(completed_release_ids):
        await advance_next_staged_firmware_deployment(session, release_id)
    for batch_id in dict.fromkeys(touched_batch_ids):
        await recalculate_firmware_batch(session, batch_id, now=completed_at)
    return tuple(completed_release_ids)


async def reconcile_stale_firmware_deployments(
    session: AsyncSession, *, now: datetime | None = None
) -> tuple[str, ...]:
    """Fail closed on bounded OTA deadlines; safe to call at startup or before API reads."""

    effective_now = now or datetime.now(UTC)
    preflight_candidates = list(
        (
            await session.scalars(
                select(FirmwareDeployment)
                .where(FirmwareDeployment.state.in_(ACTIVE_FIRMWARE_DEPLOYMENT_STATES))
                .order_by(FirmwareDeployment.id)
            )
        ).all()
    )
    graph = await lock_firmware_ota_graph(session, preflight_candidates, lock_commands=True)
    locked_commands = graph.commands
    candidates = [
        deployment
        for deployment in graph.deployments
        if deployment.state in ACTIVE_FIRMWARE_DEPLOYMENT_STATES
    ]
    expired: list[FirmwareDeployment] = []
    for deployment in candidates:
        timeout = DEPLOYMENT_TIMEOUTS[deployment.state]
        updated_at = deployment.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if effective_now - updated_at <= timeout:
            continue
        prior_state = deployment.state
        deployment.state = "timed_out"
        deployment.completed_at = effective_now
        deployment.updated_at = effective_now
        deployment.error_code = "OTA_JOB_TIMED_OUT"
        deployment.error_message = f"OTA {prior_state} stage exceeded its bounded deadline"
        deployment.evidence = {
            **deployment.evidence,
            "last_confirmed_stage": prior_state,
            "timed_out_at": effective_now.isoformat(),
            "server_result_code": "OTA_JOB_TIMED_OUT",
        }
        for command in locked_commands:
            if command.payload.get("deployment_id") != deployment.id:
                continue
            command.state = "expired"
            command.last_result = {
                "result_code": "OTA_JOB_TIMED_OUT",
                "evidence": {"last_confirmed_stage": prior_state},
            }
        logger.warning(
            "ota_job_timed_out",
            deployment_id=deployment.id,
            device_id=deployment.device_id,
            release_id=deployment.firmware_release_id,
            last_confirmed_stage=prior_state,
            error_code="OTA_JOB_TIMED_OUT",
        )
        expired.append(deployment)
    await session.flush()
    for deployment in expired:
        await halt_staged_siblings_after_failure(session, deployment, now=effective_now)
    for release_id in dict.fromkeys(item.firmware_release_id for item in expired):
        await advance_next_staged_firmware_deployment(session, release_id)
    for batch_id in dict.fromkeys(item.batch_id for item in expired):
        await recalculate_firmware_batch(session, batch_id, now=effective_now)
    return tuple(item.id for item in expired)


async def apply_firmware_deployment_retention(
    session: AsyncSession, *, now: datetime | None = None
) -> tuple[str, ...]:
    """Compact only archived terminal deployment history beyond configured retention.

    Release rows and firmware artifacts are deliberately outside this cleanup.
    The deployment and per-sensor rows remain as compact audit tombstones so
    historical foreign-key and security evidence is never orphaned.
    """

    lifecycle = await session.get(FirmwareLifecycleSetting, "global")
    if lifecycle is None or lifecycle.deployment_retention_days is None:
        return ()
    effective_now = now or datetime.now(UTC)
    cutoff = effective_now - timedelta(days=lifecycle.deployment_retention_days)
    candidates = list(
        (
            await session.scalars(
                select(FirmwareDeploymentBatch)
                .where(
                    FirmwareDeploymentBatch.archived_at.is_not(None),
                    FirmwareDeploymentBatch.archived_at < cutoff,
                    FirmwareDeploymentBatch.deleted_at.is_(None),
                    FirmwareDeploymentBatch.troubleshooting_hold.is_(False),
                )
                .order_by(FirmwareDeploymentBatch.id)
            )
        ).all()
    )
    removed: list[str] = []
    for candidate in candidates:
        preflight_deployments = list(
            (
                await session.scalars(
                    select(FirmwareDeployment)
                    .where(FirmwareDeployment.batch_id == candidate.id)
                    .order_by(FirmwareDeployment.id)
                )
            ).all()
        )
        if not preflight_deployments or any(
            deployment.state not in TERMINAL_FIRMWARE_DEPLOYMENT_STATES
            for deployment in preflight_deployments
        ):
            continue
        graph = await lock_firmware_ota_graph(session, preflight_deployments, lock_commands=True)
        locked_commands = graph.commands
        batch = next((row for row in graph.batches if row.id == candidate.id), None)
        if (
            batch is None
            or batch.archived_at is None
            or aware_utc(batch.archived_at) >= cutoff
            or batch.deleted_at is not None
            or batch.troubleshooting_hold
        ):
            continue
        deployments = [
            deployment for deployment in graph.deployments if deployment.batch_id == batch.id
        ]
        if {deployment.id for deployment in deployments} != {
            deployment.id for deployment in preflight_deployments
        } or any(
            deployment.state not in TERMINAL_FIRMWARE_DEPLOYMENT_STATES
            for deployment in deployments
        ):
            continue
        deployment_ids = {deployment.id for deployment in deployments}
        if any(
            command.payload.get("deployment_id") in deployment_ids for command in locked_commands
        ):
            continue
        final_states = {deployment.id: deployment.state for deployment in deployments}
        for deployment in deployments:
            retained_evidence = {
                key: deployment.evidence[key]
                for key in (
                    "previous_firmware_version",
                    "post_reboot_firmware_version",
                    "expected_firmware_build_id",
                    "post_reboot_firmware_build_id",
                    "post_reboot_confirmed_at",
                    "server_result_code",
                )
                if key in deployment.evidence
            }
            deployment.evidence = {
                **retained_evidence,
                "audit_tombstone": True,
                "details_deleted_at": effective_now.isoformat(),
                "retention_cleanup": True,
            }
            deployment.error_message = None
        batch.deleted_at = effective_now
        batch.deleted_by_user_id = None
        batch.updated_at = effective_now
        session.add(
            AuditEvent(
                actor_user_id=None,
                event_code="FIRMWARE_DEPLOYMENT_RETENTION_APPLIED",
                target_type="firmware_deployment_batch_tombstone",
                target_id=batch.id,
                details={
                    "retention_days": lifecycle.deployment_retention_days,
                    "release_id": batch.firmware_release_id,
                    "final_state": batch.state,
                    "per_sensor_final_states": final_states,
                    "artifact_preserved": True,
                },
            )
        )
        removed.append(batch.id)
    return tuple(removed)


async def reconcile_firmware_artifact_quarantines(
    session: AsyncSession,
    *,
    firmware_dir: Path,
    apply: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    """Recover exact, parseable two-phase artifact deletion remnants.

    Unknown final images, symlinks, and collisions are reported and never
    deleted. Mature unowned pending uploads are purged after the shared
    lifecycle lock proves no upload transaction is still committing them. A
    pre-commit delete crash is restored from DB metadata; a post-commit delete
    crash is purged only when the DB lifecycle already records deletion.
    """

    directory = firmware_dir.resolve()
    effective_now = now or datetime.now(UTC)
    recovery_cutoff = effective_now - ARTIFACT_RECOVERY_GRACE
    delete_pattern = re.compile(
        r"^\.(?P<release>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12})\.(?P<token>[0-9a-f]{16})\.pending-delete$"
    )
    upload_pattern = re.compile(
        r"^\.(?P<release>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12})\.(?P<token>[0-9a-f]{16})\.pending-upload$"
    )
    final_pattern = re.compile(
        r"^(?P<release>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12})\.bin$"
    )
    temp_pattern = re.compile(
        r"^(?P<release>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12})\.tmp$"
    )
    delete_candidates: dict[str, list[Path]] = {}
    upload_candidates: dict[str, list[Path]] = {}
    final_candidates: dict[str, Path] = {}
    temp_candidates: dict[str, Path] = {}
    unsafe_entries: list[str] = []
    if directory.is_dir():
        for path in directory.iterdir():
            matches = {
                "delete": delete_pattern.fullmatch(path.name),
                "upload": upload_pattern.fullmatch(path.name),
                "final": final_pattern.fullmatch(path.name),
                "temp": temp_pattern.fullmatch(path.name),
            }
            matched = next(((kind, value) for kind, value in matches.items() if value), None)
            if matched is None:
                continue
            if path.is_symlink() or not path.is_file():
                unsafe_entries.append(path.name)
                continue
            kind, match = matched
            release_id = match.group("release")
            if kind == "delete":
                delete_candidates.setdefault(release_id, []).append(path)
            elif kind == "upload":
                upload_candidates.setdefault(release_id, []).append(path)
            elif kind == "final":
                final_candidates[release_id] = path
            else:
                temp_candidates[release_id] = path
    if apply:
        # Upload and release deletion hold this singleton before changing any
        # pending artifact. Waiting here makes the following ownership query see
        # the transaction that created a legitimate pending upload.
        await session.scalar(
            select(FirmwareLifecycleSetting)
            .where(FirmwareLifecycleSetting.id == "global")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    candidate_ids = (
        set(delete_candidates)
        | set(upload_candidates)
        | set(final_candidates)
        | set(temp_candidates)
    )
    releases = (
        {
            release.id: release
            for release in (
                await session.scalars(
                    select(FirmwareRelease).where(FirmwareRelease.id.in_(candidate_ids))
                )
            ).all()
        }
        if candidate_ids
        else {}
    )

    def mature(path: Path) -> bool:
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return False
        return modified_at <= recovery_cutoff

    mature_delete_ids = {
        release_id
        for release_id, paths in delete_candidates.items()
        if paths and all(mature(path) for path in paths)
    }
    mature_upload_ids = {
        release_id
        for release_id, paths in upload_candidates.items()
        if paths and all(mature(path) for path in paths)
    }
    mature_ids = mature_delete_ids | mature_upload_ids
    locked_releases: dict[str, FirmwareRelease] = {}
    if apply and mature_ids:
        locked_releases = {
            release.id: release
            for release in (
                await session.scalars(
                    select(FirmwareRelease)
                    .where(FirmwareRelease.id.in_(mature_ids))
                    .order_by(FirmwareRelease.id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            ).all()
        }
    skipped_locked_ids = (
        sorted((mature_ids & set(releases)) - set(locked_releases)) if apply else []
    )
    deferred_recovery_ids = sorted(
        (set(delete_candidates) - mature_delete_ids)
        | (set(upload_candidates) - mature_upload_ids)
        | set(skipped_locked_ids)
    )

    def release_for_mutation(release_id: str) -> FirmwareRelease | None:
        if apply:
            return locked_releases.get(release_id)
        return releases.get(release_id)

    restored: list[str] = []
    purged: list[str] = []
    purged_unknown_uploads: list[str] = []
    promoted_uploads: list[str] = []
    collisions: list[str] = []
    corrupt_artifacts: list[str] = []
    unknown_release_ids: list[str] = []
    for release_id, paths in delete_candidates.items():
        if release_id not in mature_delete_ids or release_id in skipped_locked_ids:
            continue
        paths = [path for path in paths if path.is_file() and not path.is_symlink()]
        if not paths:
            continue
        release = release_for_mutation(release_id)
        if release is None:
            unknown_release_ids.append(release_id)
            continue
        if len(paths) != 1:
            collisions.append(release_id)
            continue
        quarantine = paths[0]
        expected = directory / f"{release.id}.bin"
        if release.lifecycle_state in {"deleted", "rejected"} and release.image_path == "":
            if apply:
                durable_unlink(quarantine)
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_code="FIRMWARE_ARTIFACT_QUARANTINE_PURGED",
                        target_type="firmware_release",
                        target_id=release.id,
                        details={"lifecycle_state": release.lifecycle_state},
                    )
                )
            purged.append(release.id)
            continue
        configured = Path(release.image_path) if release.image_path else None
        if (
            configured is not None
            and configured.resolve() == expected
            and not expected.exists()
            and release.lifecycle_state not in {"deleted", "rejected"}
        ):
            if not firmware_artifact_matches(
                quarantine,
                expected_size=release.image_size,
                expected_sha256=release.sha256,
            ):
                corrupt_artifacts.append(release.id)
                continue
            if apply:
                durable_replace(quarantine, expected)
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_code="FIRMWARE_ARTIFACT_QUARANTINE_RESTORED",
                        target_type="firmware_release",
                        target_id=release.id,
                        details={"lifecycle_state": release.lifecycle_state},
                    )
                )
            restored.append(release.id)
            continue
        collisions.append(release.id)
    for release_id, paths in upload_candidates.items():
        if release_id not in mature_upload_ids or release_id in skipped_locked_ids:
            continue
        paths = [path for path in paths if path.is_file() and not path.is_symlink()]
        if not paths:
            continue
        release = release_for_mutation(release_id)
        if release is None:
            if len(paths) != 1:
                collisions.append(release_id)
                continue
            if apply:
                durable_unlink(paths[0], missing_ok=True)
                session.add(
                    AuditEvent(
                        actor_user_id=None,
                        event_code="FIRMWARE_ORPHAN_UPLOAD_PURGED",
                        target_type="firmware_artifact_orphan",
                        target_id=release_id,
                        details={
                            "filename": paths[0].name,
                            "minimum_age_seconds": int(ARTIFACT_RECOVERY_GRACE.total_seconds()),
                        },
                    )
                )
                purged_unknown_uploads.append(release_id)
            else:
                unknown_release_ids.append(release_id)
            continue
        if len(paths) != 1:
            collisions.append(release_id)
            continue
        pending = paths[0]
        expected = directory / f"{release.id}.bin"
        configured = Path(release.image_path) if release.image_path else None
        if (
            configured is None
            or configured.resolve() != expected
            or expected.exists()
            or release.lifecycle_state in {"deleted", "rejected"}
        ):
            collisions.append(release.id)
            continue
        if not firmware_artifact_matches(
            pending,
            expected_size=release.image_size,
            expected_sha256=release.sha256,
        ):
            corrupt_artifacts.append(release.id)
            continue
        if apply:
            durable_replace(pending, expected)
            session.add(
                AuditEvent(
                    actor_user_id=None,
                    event_code="FIRMWARE_ARTIFACT_UPLOAD_PROMOTED",
                    target_type="firmware_release",
                    target_id=release.id,
                    details={"lifecycle_state": release.lifecycle_state},
                )
            )
        promoted_uploads.append(release.id)

    orphan_final_release_ids: list[str] = []
    for release_id, path in final_candidates.items():
        if not path.is_file() or path.is_symlink():
            continue
        release = releases.get(release_id)
        if release is None:
            orphan_final_release_ids.append(release_id)
            continue
        configured = Path(release.image_path) if release.image_path else None
        if (
            configured is None
            or configured.resolve() != path
            or release.lifecycle_state in {"deleted", "rejected"}
        ):
            orphan_final_release_ids.append(release_id)
        elif not firmware_artifact_matches(
            path,
            expected_size=release.image_size,
            expected_sha256=release.sha256,
        ):
            corrupt_artifacts.append(release_id)

    orphan_temp_release_ids = sorted(temp_candidates)
    unknown_release_ids = sorted(set(unknown_release_ids))
    collisions = sorted(set(collisions))
    corrupt_artifacts = sorted(set(corrupt_artifacts))
    return {
        "apply": apply,
        "restored_release_ids": restored,
        "purged_release_ids": purged,
        "purged_unknown_upload_release_ids": purged_unknown_uploads,
        "promoted_upload_release_ids": promoted_uploads,
        "collision_release_ids": collisions,
        "corrupt_artifact_release_ids": corrupt_artifacts,
        "unknown_release_ids": unknown_release_ids,
        "orphan_final_release_ids": orphan_final_release_ids,
        "orphan_temp_release_ids": orphan_temp_release_ids,
        "deferred_recovery_release_ids": deferred_recovery_ids,
        "recovery_grace_seconds": int(ARTIFACT_RECOVERY_GRACE.total_seconds()),
        "unsafe_entries": unsafe_entries,
        "attention_required": bool(
            collisions
            or corrupt_artifacts
            or unknown_release_ids
            or orphan_final_release_ids
            or orphan_temp_release_ids
            or unsafe_entries
        ),
    }


__all__ = [
    "ACTIVE_FIRMWARE_DEPLOYMENT_STATES",
    "ACTIVE_OTA_COMMAND_STATES",
    "ARTIFACT_RECOVERY_GRACE",
    "TERMINAL_FIRMWARE_DEPLOYMENT_STATES",
    "ESP32S3AppIdentity",
    "FirmwareOTAGraphLocks",
    "advance_next_staged_firmware_deployment",
    "apply_firmware_deployment_retention",
    "durable_replace",
    "durable_unlink",
    "durable_write_bytes",
    "firmware_artifact_matches",
    "halt_staged_siblings_after_failure",
    "lock_active_ota_commands_for_deployments",
    "lock_firmware_ota_graph",
    "parse_esp32s3_app_identity",
    "queue_staged_firmware_deployment",
    "recalculate_firmware_batch",
    "reconcile_firmware_artifact_quarantines",
    "reconcile_firmware_version_heartbeat",
    "reconcile_stale_firmware_deployments",
]
