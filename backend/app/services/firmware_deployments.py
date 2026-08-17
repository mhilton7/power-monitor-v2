from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DeviceCommand, FirmwareDeployment, FirmwareDeploymentBatch, FirmwareRelease

logger = structlog.get_logger()

ACTIVE_FIRMWARE_DEPLOYMENT_STATES = frozenset(
    {"staged", "queued", "downloading", "rebooting", "validating"}
)
TERMINAL_FIRMWARE_DEPLOYMENT_STATES = frozenset(
    {"succeeded", "failed", "rolled_back", "timed_out", "cancelled"}
)
DEPLOYMENT_TIMEOUTS = {
    "staged": timedelta(hours=24),
    "queued": timedelta(minutes=30),
    "downloading": timedelta(minutes=20),
    "rebooting": timedelta(minutes=10),
    "validating": timedelta(minutes=10),
}


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
    staged = await session.scalar(
        select(FirmwareDeployment)
        .where(
            FirmwareDeployment.firmware_release_id == release_id,
            FirmwareDeployment.state == "staged",
        )
        .order_by(FirmwareDeployment.created_at, FirmwareDeployment.id)
        .with_for_update(skip_locked=True)
        .limit(1)
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
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Turn post-reboot version evidence into terminal OTA success and advance staging."""

    completed_at = now or datetime.now(UTC)
    # Command results are applied earlier in the same authenticated heartbeat.
    # Flush their validating transition before selecting confirmation work.
    await session.flush()
    rows = (
        await session.execute(
            select(FirmwareDeployment, FirmwareRelease)
            .join(
                FirmwareRelease,
                FirmwareRelease.id == FirmwareDeployment.firmware_release_id,
            )
            .where(
                FirmwareDeployment.device_id == device_id,
                FirmwareDeployment.state == "validating",
            )
            .with_for_update()
        )
    ).all()
    completed_release_ids: list[str] = []
    touched_batch_ids: list[str | None] = []
    for deployment, release in rows:
        if release.semantic_version.removeprefix("v") != firmware_version.removeprefix("v"):
            deployment.state = "failed"
            deployment.completed_at = completed_at
            deployment.updated_at = completed_at
            deployment.error_code = "OTA_VERSION_NOT_CONFIRMED"
            deployment.error_message = (
                f"Sensor reconnected on {firmware_version} instead of {release.semantic_version}"
            )
            deployment.evidence = {
                **deployment.evidence,
                "expected_firmware_version": release.semantic_version,
                "post_reboot_firmware_version": firmware_version,
                "post_reboot_observed_at": completed_at.isoformat(),
                "server_result_code": "OTA_VERSION_NOT_CONFIRMED",
            }
            await halt_staged_siblings_after_failure(session, deployment, now=completed_at)
            logger.warning(
                "ota_version_not_confirmed",
                deployment_id=deployment.id,
                device_id=device_id,
                release_id=release.id,
                expected_version=release.semantic_version,
                reported_version=firmware_version,
                error_code="OTA_VERSION_NOT_CONFIRMED",
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
    candidates = list(
        (
            await session.scalars(
                select(FirmwareDeployment)
                .where(FirmwareDeployment.state.in_(ACTIVE_FIRMWARE_DEPLOYMENT_STATES))
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
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
        commands = list(
            (
                await session.scalars(
                    select(DeviceCommand)
                    .where(
                        DeviceCommand.device_id == deployment.device_id,
                        DeviceCommand.command_type == "ota_install",
                        DeviceCommand.state.in_(
                            (
                                "queued",
                                "delivered",
                                "accepted",
                                "running",
                                "awaiting_reboot",
                                "awaiting_heartbeat",
                            )
                        ),
                    )
                    .with_for_update()
                )
            ).all()
        )
        for command in commands:
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


__all__ = [
    "ACTIVE_FIRMWARE_DEPLOYMENT_STATES",
    "TERMINAL_FIRMWARE_DEPLOYMENT_STATES",
    "advance_next_staged_firmware_deployment",
    "halt_staged_siblings_after_failure",
    "queue_staged_firmware_deployment",
    "recalculate_firmware_batch",
    "reconcile_firmware_version_heartbeat",
    "reconcile_stale_firmware_deployments",
]
