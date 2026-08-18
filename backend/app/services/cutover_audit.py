from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import NotFound
from ..models import (
    Circuit,
    Device,
    FirmwareDeployment,
    Home,
    NormalizedInterval,
    RateAssignment,
    RatePlanVersion,
    User,
    UtilityAccount,
    user_home_scopes,
)


async def redacted_cutover_snapshot(
    session: AsyncSession, home_id: str, *, now: datetime | None = None
) -> dict[str, object]:
    """Return nonsecret IDs/counts used to prove a cutover preserved durable state."""

    instant = (now or datetime.now(UTC)).astimezone(UTC)
    home = await session.get(Home, home_id)
    if home is None:
        raise NotFound("home does not exist")
    devices = (
        await session.scalars(select(Device).where(Device.home_id == home.id).order_by(Device.id))
    ).all()
    device_ids = tuple(item.id for item in devices)
    history_count, history_earliest, history_latest = (
        await session.execute(
            select(
                func.count(NormalizedInterval.id),
                func.min(NormalizedInterval.start_utc),
                func.max(NormalizedInterval.end_utc),
            ).where(
                NormalizedInterval.device_id.in_(device_ids),
                NormalizedInterval.source_authenticated.is_(True),
            )
        )
    ).one()
    active_assignments = (
        select(RateAssignment)
        .join(UtilityAccount, UtilityAccount.id == RateAssignment.utility_account_id)
        .where(
            UtilityAccount.home_id == home.id,
            RateAssignment.effective_start <= instant,
            (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > instant)),
        )
    )
    active_assignment_count = int(
        await session.scalar(select(func.count()).select_from(active_assignments.subquery())) or 0
    )
    active_rate_version_count = int(
        await session.scalar(
            select(func.count(distinct(RateAssignment.rate_plan_version_id)))
            .join(UtilityAccount, UtilityAccount.id == RateAssignment.utility_account_id)
            .join(RatePlanVersion, RatePlanVersion.id == RateAssignment.rate_plan_version_id)
            .where(
                UtilityAccount.home_id == home.id,
                RateAssignment.effective_start <= instant,
                (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > instant)),
                RatePlanVersion.state == "published",
            )
        )
        or 0
    )
    user_count = int(
        await session.scalar(
            select(func.count(distinct(User.id)))
            .join(user_home_scopes, user_home_scopes.c.user_id == User.id)
            .where(user_home_scopes.c.home_id == home.id)
        )
        or 0
    )
    deployment_count = int(
        await session.scalar(
            select(func.count(FirmwareDeployment.id))
            .join(Device, Device.id == FirmwareDeployment.device_id)
            .where(Device.home_id == home.id)
        )
        or 0
    )
    release_count = int(
        await session.scalar(
            select(func.count(distinct(FirmwareDeployment.firmware_release_id)))
            .join(Device, Device.id == FirmwareDeployment.device_id)
            .where(Device.home_id == home.id)
        )
        or 0
    )
    main = await session.scalar(
        select(Circuit).where(
            Circuit.home_id == home.id,
            Circuit.is_home_total.is_(True),
            Circuit.is_billing_source.is_(True),
        )
    )
    main_members = (
        (
            await session.scalars(
                select(Device.id).where(Device.circuit_id == main.id).order_by(Device.id)
            )
        ).all()
        if main is not None
        else []
    )
    return {
        "snapshot_at": instant,
        "home": {"id": home.id, "name": home.name},
        "sensors": [
            {
                "id": item.id,
                "name": item.friendly_name,
                "revoked": item.revoked_at is not None,
            }
            for item in devices
        ],
        "accepted_history": {
            "count": int(history_count or 0),
            "earliest_utc": history_earliest,
            "latest_utc": history_latest,
        },
        "rates": {
            "active_assignment_count": active_assignment_count,
            "active_version_count": active_rate_version_count,
        },
        "user_count": user_count,
        "ota": {
            "release_count": release_count,
            "deployment_count": deployment_count,
        },
        "main_service": {
            "id": main.id if main is not None else None,
            "name": main.name if main is not None else None,
            "member_device_ids": list(main_members),
        },
    }
