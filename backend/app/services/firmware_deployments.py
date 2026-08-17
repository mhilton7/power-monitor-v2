from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FirmwareDeployment, FirmwareRelease
from .commands import create_command

ACTIVE_FIRMWARE_DEPLOYMENT_STATES = frozenset({"staged", "queued", "downloading", "validating"})
TERMINAL_FIRMWARE_DEPLOYMENT_STATES = frozenset({"succeeded", "failed", "rolled_back", "cancelled"})


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
    manifest = _stored_manifest(deployment)
    actor = deployment.evidence["issued_by_user_id"]
    assert isinstance(actor, str)
    deployment.state = "queued"
    await create_command(
        session,
        device_id=deployment.device_id,
        command_type="ota_install",
        issued_by_user_id=actor,
        idempotency_key=f"ota:{deployment.id}",
        payload=manifest,
    )


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
            FirmwareDeployment.state.in_(("queued", "downloading", "validating")),
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
    for deployment, release in rows:
        if release.semantic_version != firmware_version:
            continue
        deployment.state = "succeeded"
        deployment.progress_percent = 100
        deployment.completed_at = completed_at
        deployment.evidence = {
            **deployment.evidence,
            "post_reboot_firmware_version": firmware_version,
            "post_reboot_confirmed_at": completed_at.isoformat(),
        }
        completed_release_ids.append(release.id)
    await session.flush()
    for release_id in dict.fromkeys(completed_release_ids):
        await advance_next_staged_firmware_deployment(session, release_id)
    return tuple(completed_release_ids)


__all__ = [
    "ACTIVE_FIRMWARE_DEPLOYMENT_STATES",
    "TERMINAL_FIRMWARE_DEPLOYMENT_STATES",
    "advance_next_staged_firmware_deployment",
    "queue_staged_firmware_deployment",
    "reconcile_firmware_version_heartbeat",
]
