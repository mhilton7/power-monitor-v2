from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..errors import IntegrityConflict, InvalidRequest, NotFound
from ..models import (
    Alert,
    AlertEvent,
    AlertMaintenanceWindow,
    AuditEvent,
    Circuit,
    Device,
    DeviceCredential,
    Home,
    UtilityAccount,
    user_home_scopes,
)
from ..schemas.api import (
    AlertMaintenanceWindowRequest,
    AlertSilenceRequest,
    DeviceRevokeRequest,
    DeviceUpdateRequest,
    HomeScopesResponse,
    HomeUtilityUpdateRequest,
    VerifiedAggregateRequest,
)
from ..security.auth import CurrentUser, current_user, require_permission

router = APIRouter(prefix="/api/v1", tags=["settings"])


async def _home_ids(session: AsyncSession, user_id: str) -> tuple[str, ...]:
    return tuple(
        (
            await session.scalars(
                select(user_home_scopes.c.home_id)
                .where(user_home_scopes.c.user_id == user_id)
                .order_by(user_home_scopes.c.home_id)
            )
        ).all()
    )


async def _resolve_home_id(
    session: AsyncSession, user_id: str, requested_home_id: str | None
) -> str:
    home_ids = await _home_ids(session, user_id)
    if requested_home_id is not None:
        if requested_home_id not in home_ids:
            raise NotFound("home does not exist")
        return requested_home_id
    if not home_ids:
        raise NotFound("home does not exist")
    if len(home_ids) > 1:
        raise InvalidRequest("home_id is required when the actor can access multiple homes")
    return home_ids[0]


async def _validate_account_measurement_scope(
    session: AsyncSession,
    *,
    home_id: str,
    scope: str,
    override_device: tuple[str, str] | None = None,
) -> None:
    if scope == "energy_only":
        return
    devices = (
        await session.scalars(
            select(Device)
            .where(Device.home_id == home_id, Device.revoked_at.is_(None))
            .with_for_update()
        )
    ).all()
    matching = [
        device
        for device in devices
        if (
            override_device[1]
            if override_device is not None and device.id == override_device[0]
            else device.measurement_scope
        )
        == scope
    ]
    if not matching:
        raise IntegrityConflict(
            "account scope requires at least one explicitly verified sensor measurement scope"
        )
    if len(matching) == 1:
        return
    circuit_ids = {device.circuit_id for device in matching}
    circuit_id = next(iter(circuit_ids)) if len(circuit_ids) == 1 else None
    circuit = await session.get(Circuit, circuit_id) if circuit_id else None
    if circuit is None or circuit.aggregate_mode != "verified_sum":
        raise IntegrityConflict(
            "multiple account-scoped sensors require one verified non-overlapping aggregate"
        )


@router.get("/home-scopes", response_model=HomeScopesResponse)
async def list_home_scopes(
    user: CurrentUser = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    rows = (
        await session.execute(
            select(Home.id, Home.name)
            .join(user_home_scopes, user_home_scopes.c.home_id == Home.id)
            .where(user_home_scopes.c.user_id == user.id)
            .distinct()
            .order_by(Home.name, Home.id)
        )
    ).all()
    return {"home_scopes": [{"id": row.id, "name": row.name} for row in rows]}


@router.get("/settings/home-utility")
async def home_utility(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("billing.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_home_id(session, user.id, home_id)
    home = await session.get(Home, scoped_home_id)
    if home is None:
        raise NotFound("home does not exist")
    account = await session.scalar(select(UtilityAccount).where(UtilityAccount.home_id == home.id))
    if account is None:
        raise NotFound("utility account does not exist")
    return {
        "home": {"id": home.id, "name": home.name, "timezone": home.timezone},
        "utility": {
            "id": account.id,
            "utility_name": account.utility_name,
            "timezone": account.timezone,
            "billing_day": account.billing_day,
            "cost_scope": account.cost_scope,
            "baseline_allocation_kwh": account.baseline_allocation_kwh,
            "cca_provider": account.cca_provider,
        },
        "usage_source": "authenticated PZEM-004T sensor intervals only",
    }


@router.patch("/settings/home-utility")
async def update_home_utility(
    payload: HomeUtilityUpdateRequest,
    request: Request,
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("system.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_home_id(session, user.id, home_id)
    home = await session.scalar(select(Home).where(Home.id == scoped_home_id).with_for_update())
    if home is None:
        raise NotFound("home does not exist")
    account = await session.scalar(
        select(UtilityAccount).where(UtilityAccount.home_id == home.id).with_for_update()
    )
    if account is None:
        raise NotFound("utility account does not exist")
    if payload.timezone is not None:
        try:
            ZoneInfo(payload.timezone)
        except ZoneInfoNotFoundError as exc:
            raise IntegrityConflict("timezone is not recognized") from exc
        home.timezone = payload.timezone
        account.timezone = payload.timezone
    if payload.home_name is not None:
        home.name = payload.home_name
    if payload.billing_day is not None:
        account.billing_day = payload.billing_day
    if payload.cost_scope is not None:
        await _validate_account_measurement_scope(
            session, home_id=home.id, scope=payload.cost_scope
        )
        account.cost_scope = payload.cost_scope
    if "baseline_allocation_kwh" in payload.model_fields_set:
        account.baseline_allocation_kwh = payload.baseline_allocation_kwh
    if "cca_provider" in payload.model_fields_set:
        account.cca_provider = payload.cca_provider
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="HOME_UTILITY_SETTINGS_UPDATED",
            target_type="utility_account",
            target_id=account.id,
            correlation_id=request.state.correlation_id,
            details={
                "cost_scope": payload.cost_scope,
                "billing_day": payload.billing_day,
                "timezone": payload.timezone,
            },
        )
    )
    await session.commit()
    return await home_utility(home_id=home.id, user=user, session=session)


@router.patch("/devices/{device_id}")
async def update_device(
    device_id: str,
    payload: DeviceUpdateRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("sensors.configure")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _home_ids(session, user.id)
    device = await session.scalar(
        select(Device)
        .where(Device.id == device_id, Device.home_id.in_(homes), Device.revoked_at.is_(None))
        .with_for_update()
    )
    if device is None:
        raise NotFound("device does not exist")
    if payload.friendly_name is not None:
        device.friendly_name = payload.friendly_name
    if payload.measurement_scope is not None:
        await _validate_account_measurement_scope(
            session,
            home_id=device.home_id,
            scope=payload.measurement_scope,
            override_device=(device.id, payload.measurement_scope),
        )
        account = await session.scalar(
            select(UtilityAccount).where(UtilityAccount.home_id == device.home_id)
        )
        if account is not None and account.cost_scope != "energy_only":
            await _validate_account_measurement_scope(
                session,
                home_id=device.home_id,
                scope=account.cost_scope,
                override_device=(device.id, payload.measurement_scope),
            )
        device.measurement_scope = payload.measurement_scope
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="DEVICE_SETTINGS_UPDATED",
            target_type="device",
            target_id=device.id,
            correlation_id=request.state.correlation_id,
            details={"measurement_scope": payload.measurement_scope},
        )
    )
    await session.commit()
    return {
        "id": device.id,
        "friendly_name": device.friendly_name,
        "measurement_scope": device.measurement_scope,
    }


@router.get("/circuits")
async def list_circuits(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("sensors.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    scoped_home_id = await _resolve_home_id(session, user.id, home_id)
    rows = (
        await session.scalars(
            select(Circuit).where(Circuit.home_id == scoped_home_id).order_by(Circuit.id)
        )
    ).all()
    return {
        "circuits": [
            {
                "id": row.id,
                "home_id": row.home_id,
                "name": row.name,
                "aggregate_mode": row.aggregate_mode,
            }
            for row in rows
        ]
    }


@router.post("/circuits/verified-aggregates", status_code=201)
async def create_verified_aggregate(
    payload: VerifiedAggregateRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("sensors.configure")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _home_ids(session, user.id)
    if payload.home_id not in homes:
        raise NotFound("home does not exist")
    devices = (
        await session.scalars(
            select(Device)
            .where(
                Device.id.in_(payload.device_ids),
                Device.home_id == payload.home_id,
                Device.revoked_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if {item.id for item in devices} != set(payload.device_ids):
        raise NotFound("one or more aggregate sensors do not exist")
    if any(item.circuit_id is not None for item in devices):
        raise IntegrityConflict("a sensor already belongs to a circuit")
    circuit = Circuit(
        home_id=payload.home_id,
        name=payload.name,
        aggregate_mode="verified_sum",
    )
    session.add(circuit)
    await session.flush()
    for device in devices:
        device.circuit_id = circuit.id
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="VERIFIED_AGGREGATE_CREATED",
            target_type="circuit",
            target_id=circuit.id,
            correlation_id=request.state.correlation_id,
            details={"device_ids": sorted(payload.device_ids), "operator_verified": True},
        )
    )
    await session.commit()
    return {"id": circuit.id, "name": circuit.name, "device_ids": payload.device_ids}


@router.post("/devices/{device_id}/revoke", status_code=204)
async def revoke_device(
    device_id: str,
    _payload: DeviceRevokeRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("sensors.configure")),
    session: AsyncSession = Depends(get_session),
) -> None:
    homes = await _home_ids(session, user.id)
    device = await session.scalar(
        select(Device)
        .where(Device.id == device_id, Device.home_id.in_(homes), Device.revoked_at.is_(None))
        .with_for_update()
    )
    if device is None:
        raise NotFound("device does not exist")
    now = datetime.now(UTC)
    device.revoked_at = now
    credentials = (
        await session.scalars(
            select(DeviceCredential).where(
                DeviceCredential.device_id == device.id,
                DeviceCredential.revoked_at.is_(None),
            )
        )
    ).all()
    for credential in credentials:
        credential.revoked_at = now
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="DEVICE_REVOKED",
            target_type="device",
            target_id=device.id,
            correlation_id=request.state.correlation_id,
            details={"history_preserved": True},
        )
    )
    await session.commit()


async def _scoped_alert(session: AsyncSession, user_id: str, alert_id: str) -> Alert:
    homes = await _home_ids(session, user_id)
    alert = await session.scalar(
        select(Alert).where(Alert.id == alert_id, Alert.home_id.in_(homes)).with_for_update()
    )
    if alert is None:
        raise NotFound("alert does not exist")
    return alert


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(
    alert_id: str,
    request: Request,
    user: CurrentUser = Depends(require_permission("dashboard.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    alert = await _scoped_alert(session, user.id, alert_id)
    if alert.state == "resolved":
        raise IntegrityConflict("resolved alert cannot be acknowledged")
    alert.state = "acknowledged"
    alert.acknowledged_at = datetime.now(UTC)
    session.add(
        AlertEvent(
            alert_id=alert.id,
            event_code="ACKNOWLEDGED",
            evidence={"actor_user_id": user.id},
        )
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="ALERT_ACKNOWLEDGED",
            target_type="alert",
            target_id=alert.id,
            correlation_id=request.state.correlation_id,
            details={"alert_type": alert.alert_type},
        )
    )
    await session.commit()
    return {"id": alert.id, "state": alert.state}


@router.post("/alerts/{alert_id}/silence")
async def silence_alert(
    alert_id: str,
    payload: AlertSilenceRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("system.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    now = datetime.now(UTC)
    until = payload.until.astimezone(UTC)
    if not now < until <= now + timedelta(days=30):
        raise IntegrityConflict("alert silence must end within 30 days")
    alert = await _scoped_alert(session, user.id, alert_id)
    alert.silenced_until = until
    session.add(
        AlertEvent(
            alert_id=alert.id,
            event_code="SILENCED",
            evidence={"actor_user_id": user.id, "until": until.isoformat()},
        )
    )
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="ALERT_SILENCED",
            target_type="alert",
            target_id=alert.id,
            correlation_id=request.state.correlation_id,
            details={"until": until.isoformat()},
        )
    )
    await session.commit()
    return {"id": alert.id, "silenced_until": alert.silenced_until}


@router.get("/alert-maintenance-windows")
async def alert_maintenance_windows(
    user: CurrentUser = Depends(require_permission("system.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _home_ids(session, user.id)
    rows = (
        await session.scalars(
            select(AlertMaintenanceWindow)
            .where(
                AlertMaintenanceWindow.home_id.in_(homes),
                AlertMaintenanceWindow.cancelled_at.is_(None),
                AlertMaintenanceWindow.ends_at >= datetime.now(UTC),
            )
            .order_by(AlertMaintenanceWindow.starts_at)
        )
    ).all()
    return {
        "maintenance_windows": [
            {
                "id": row.id,
                "home_id": row.home_id,
                "device_id": row.device_id,
                "alert_type": row.alert_type,
                "starts_at": row.starts_at,
                "ends_at": row.ends_at,
                "reason": row.reason,
            }
            for row in rows
        ]
    }


@router.post("/alert-maintenance-windows", status_code=201)
async def create_alert_maintenance_window(
    payload: AlertMaintenanceWindowRequest,
    request: Request,
    user: CurrentUser = Depends(require_permission("system.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _home_ids(session, user.id)
    if payload.home_id not in homes:
        raise NotFound("home does not exist")
    if payload.device_id is not None:
        device = await session.scalar(
            select(Device).where(
                Device.id == payload.device_id,
                Device.home_id == payload.home_id,
                Device.revoked_at.is_(None),
            )
        )
        if device is None:
            raise NotFound("device does not exist")
    now = datetime.now(UTC)
    starts_at = payload.starts_at.astimezone(UTC)
    ends_at = payload.ends_at.astimezone(UTC)
    if ends_at <= now or starts_at > now + timedelta(days=365):
        raise IntegrityConflict("maintenance window must be current or within one year")
    window = AlertMaintenanceWindow(
        home_id=payload.home_id,
        device_id=payload.device_id,
        alert_type=payload.alert_type,
        starts_at=starts_at,
        ends_at=ends_at,
        reason=payload.reason,
        created_by_user_id=user.id,
    )
    session.add(window)
    await session.flush()
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="ALERT_MAINTENANCE_WINDOW_CREATED",
            target_type="alert_maintenance_window",
            target_id=window.id,
            correlation_id=request.state.correlation_id,
            details={
                "home_id": window.home_id,
                "device_id": window.device_id,
                "alert_type": window.alert_type,
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
            },
        )
    )
    await session.commit()
    return {"id": window.id, "state": "scheduled"}


@router.post("/alert-maintenance-windows/{window_id}/cancel")
async def cancel_alert_maintenance_window(
    window_id: str,
    request: Request,
    user: CurrentUser = Depends(require_permission("system.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _home_ids(session, user.id)
    window = await session.scalar(
        select(AlertMaintenanceWindow)
        .where(
            AlertMaintenanceWindow.id == window_id,
            AlertMaintenanceWindow.home_id.in_(homes),
            AlertMaintenanceWindow.cancelled_at.is_(None),
        )
        .with_for_update()
    )
    if window is None:
        raise NotFound("maintenance window does not exist")
    window.cancelled_at = datetime.now(UTC)
    window.cancelled_by_user_id = user.id
    session.add(
        AuditEvent(
            actor_user_id=user.id,
            event_code="ALERT_MAINTENANCE_WINDOW_CANCELLED",
            target_type="alert_maintenance_window",
            target_id=window.id,
            correlation_id=request.state.correlation_id,
            details={},
        )
    )
    await session.commit()
    return {"id": window.id, "state": "cancelled"}
