from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import math
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..errors import IntegrityConflict, InvalidRequest, NotFound, PermissionDenied
from ..models import (
    Alert,
    BillingEstimate,
    BillingEstimateSelection,
    Circuit,
    Device,
    DeviceCommand,
    DeviceCredential,
    DeviceHeartbeat,
    Home,
    IntervalCost,
    IntervalCostSelection,
    NormalizedInterval,
    RateAssignment,
    RateHoliday,
    RatePeriod,
    RatePlan,
    RatePlanVersion,
    RawReading,
    UtilityAccount,
    aware_utc,
    user_home_scopes,
)
from ..schemas.api import CommandCreateRequest
from ..security.auth import CurrentUser, require_permission
from ..services.commands import (
    COMMAND_PERMISSIONS,
    COMMIT_CONFIRMATION_PHRASES,
    cancel_data_reset_prepare,
    create_command,
    validate_commit_token,
)
from ..services.cost_engine import current_cost_per_hour_microdollars

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

PUBLIC_COMMAND_EVIDENCE_FIELDS = {
    "format_storage_prepare": {
        "prepare_command_id",
        "acknowledged_records_lost",
        "unacknowledged_records_lost",
        "ready",
    },
    "format_storage_commit": {
        "prepare_command_id",
        "acknowledged_records_lost",
        "unacknowledged_records_lost",
        "formatted",
    },
    "data_reset_prepare": {
        "prepare_command_id",
        "reset_generation",
        "server_sequence_floor",
        "sequence_floor",
        "ready",
    },
    "data_reset_commit": {"prepare_command_id", "reset_generation", "sequence_floor"},
    "data_reset_cancel": {"prepare_command_id", "cancelled"},
    "rotate_device_credentials": {
        "rotation_id",
        "credential_fingerprint",
        "ready",
        "activated",
        "cancelled",
    },
}


def _public_command_evidence(command: DeviceCommand) -> dict[str, str | int | bool | None]:
    evidence = (command.last_result or {}).get("evidence")
    allowed = PUBLIC_COMMAND_EVIDENCE_FIELDS.get(command.command_type, set())
    if not isinstance(evidence, dict) or not allowed:
        return {}
    return {
        key: value
        for key, value in evidence.items()
        if key in allowed and (value is None or isinstance(value, str | int | bool))
    }


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


async def _scoped_device(session: AsyncSession, user_id: str, device_id: str) -> Device:
    homes = await _home_ids(session, user_id)
    device = await session.scalar(
        select(Device).where(Device.id == device_id, Device.home_id.in_(homes))
    )
    if device is None:
        raise NotFound("device does not exist")
    return device


def _utc_bounds(timezone: str, now: datetime, scope: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    if scope == "today":
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif scope == "week":
        start_local = (local - timedelta(days=local.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif scope == "month":
        start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError("unsupported summary scope")
    return start_local.astimezone(UTC), now


def _billing_cycle_bounds(
    timezone: str, billing_day: int, now: datetime
) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    year, month = local.year, local.month
    if local.day < billing_day:
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    start = datetime(year, month, billing_day, tzinfo=zone)
    return start.astimezone(UTC), now


def _comparison_bounds(
    timezone: str, now: datetime
) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday = today - timedelta(days=1)
    last_week = week - timedelta(days=7)
    return (
        (yesterday.astimezone(UTC), today.astimezone(UTC)),
        (last_week.astimezone(UTC), week.astimezone(UTC)),
    )


async def _summary(
    session: AsyncSession, device_ids: tuple[str, ...], start: datetime, end: datetime
) -> dict[str, object]:
    if not device_ids:
        return {"energy_kwh": None, "cost": None, "completeness": 0, "missing_intervals": 0}
    energy, completeness_sum, count = (
        await session.execute(
            select(
                func.sum(NormalizedInterval.energy_mwh),
                func.sum(NormalizedInterval.completeness),
                func.count(NormalizedInterval.id),
            )
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(device_ids),
                RawReading.reset_generation == Device.reset_generation,
                NormalizedInterval.start_utc >= start,
                NormalizedInterval.end_utc <= end,
            )
        )
    ).one()
    cost, priced_count = (
        await session.execute(
            select(
                func.sum(IntervalCost.energy_cost_microdollars - IntervalCost.credit_microdollars),
                func.count(IntervalCost.id),
            )
            .join(
                IntervalCostSelection,
                IntervalCostSelection.interval_cost_id == IntervalCost.id,
            )
            .join(
                NormalizedInterval,
                NormalizedInterval.id == IntervalCostSelection.normalized_interval_id,
            )
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(device_ids),
                RawReading.reset_generation == Device.reset_generation,
                NormalizedInterval.start_utc >= start,
                NormalizedInterval.end_utc <= end,
            )
        )
    ).one()
    expected = max(1, int((end - start).total_seconds() // 60) * len(device_ids))
    return {
        "energy_kwh": Decimal(energy or 0) / Decimal(1_000_000) if energy is not None else None,
        "cost": (
            Decimal(cost) / Decimal(1_000_000)
            if cost is not None and int(priced_count or 0) == int(count or 0)
            else None
        ),
        "completeness": min(Decimal(1), Decimal(completeness_sum or 0) / Decimal(expected)),
        "missing_intervals": max(0, expected - int(count or 0)),
    }


async def _current_rate(
    session: AsyncSession,
    home_id: str,
    now: datetime,
    device_ids: tuple[str, ...],
    cycle_start: datetime,
) -> dict[str, object] | None:
    row = (
        await session.execute(
            select(RatePlanVersion, RatePlan, UtilityAccount)
            .join(RateAssignment, RateAssignment.rate_plan_version_id == RatePlanVersion.id)
            .join(UtilityAccount, UtilityAccount.id == RateAssignment.utility_account_id)
            .join(RatePlan, RatePlan.id == RatePlanVersion.rate_plan_id)
            .where(
                UtilityAccount.home_id == home_id,
                RateAssignment.effective_start <= now,
                (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > now)),
                RatePlanVersion.state == "published",
                RatePlanVersion.effective_start <= now,
                (RatePlanVersion.effective_end.is_(None) | (RatePlanVersion.effective_end > now)),
            )
            .order_by(RateAssignment.effective_start.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    version, plan, account = row
    local = now.astimezone(ZoneInfo(version.timezone))
    season = "summer" if local.month in (6, 7, 8, 9) else "winter"
    holiday = await session.scalar(
        select(RateHoliday.id).where(
            RateHoliday.rate_plan_version_id == version.id,
            RateHoliday.local_date == local.date(),
        )
    )
    day_type = "holiday" if holiday else "weekend" if local.weekday() >= 5 else "weekday"
    minute = local.hour * 60 + local.minute
    cumulative_mwh = int(
        await session.scalar(
            select(func.sum(NormalizedInterval.energy_mwh))
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(device_ids),
                RawReading.reset_generation == Device.reset_generation,
                NormalizedInterval.start_utc >= cycle_start,
                NormalizedInterval.end_utc <= now,
                NormalizedInterval.source_authenticated.is_(True),
            )
        )
        or 0
    )
    cumulative_kwh = Decimal(cumulative_mwh) / Decimal(1_000_000)
    effective_tier_threshold: Decimal | None = None
    if version.tier_threshold_kwh_per_day is not None and version.tier_threshold_season in (
        season,
        "all",
    ):
        cycle_local = cycle_start.astimezone(ZoneInfo(account.timezone))
        next_year = cycle_local.year + (1 if cycle_local.month == 12 else 0)
        next_month = 1 if cycle_local.month == 12 else cycle_local.month + 1
        cycle_days = (date(next_year, next_month, account.billing_day) - cycle_local.date()).days
        effective_tier_threshold = version.tier_threshold_kwh_per_day * cycle_days
    candidates = (
        await session.scalars(
            select(RatePeriod).where(
                RatePeriod.rate_plan_version_id == version.id,
                RatePeriod.season.in_((season, "all")),
                RatePeriod.day_type.in_((day_type, "all")),
                RatePeriod.start_minute <= minute,
                RatePeriod.end_minute > minute,
            )
        )
    ).all()
    tier_requires_usage = version.pricing_model in ("tiered", "seasonal_tiered") or any(
        period.tier_start_kwh > 0 or period.tier_end_kwh is not None for period in candidates
    )
    cycle_summary = await _summary(session, device_ids, cycle_start, now)
    reading_coverage = Decimal(str(cycle_summary["completeness"]))
    tier_confirmed = not tier_requires_usage or reading_coverage >= Decimal("1")
    candidates = [
        period
        for period in candidates
        if cumulative_kwh
        >= (
            effective_tier_threshold
            if effective_tier_threshold is not None
            and version.tier_threshold_source_kwh is not None
            and period.tier_start_kwh == version.tier_threshold_source_kwh
            else period.tier_start_kwh
        )
        and (
            period.tier_end_kwh is None
            or cumulative_kwh
            < (
                effective_tier_threshold
                if effective_tier_threshold is not None
                and version.tier_threshold_source_kwh is not None
                and period.tier_end_kwh == version.tier_threshold_source_kwh
                else period.tier_end_kwh
            )
        )
    ]
    if candidates:
        specificity = max(
            int(period.season == season) + int(period.day_type == day_type) for period in candidates
        )
        candidates = [
            period
            for period in candidates
            if int(period.season == season) + int(period.day_type == day_type) == specificity
        ]
    period = candidates[0] if len(candidates) == 1 else None
    effective_price = None
    next_change_at = None
    if period is not None and tier_confirmed:
        effective_price = period.price_per_kwh + version.cca_adjustment_per_kwh
        effective_price += effective_price * version.surcharge_percent / Decimal(100)
        baseline_applies = (
            account.cost_scope == "full_account"
            and account.baseline_allocation_kwh is not None
            and cumulative_kwh < account.baseline_allocation_kwh
        )
        if baseline_applies:
            effective_price -= version.baseline_credit_per_kwh
        effective_price = max(Decimal("0"), effective_price)
        local_midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        next_local = local_midnight + timedelta(minutes=period.end_minute)
        if next_local <= local:
            next_local += timedelta(days=1)
        next_change_at = next_local.astimezone(UTC)
    return {
        "plan_name": plan.name,
        "version_id": version.id,
        "effective_start": version.effective_start,
        "period": period.period_name if period and tier_confirmed else None,
        "tier_state": (
            period.period_name
            if period and tier_confirmed
            else "not_confirmed"
            if tier_requires_usage
            else None
        ),
        "tier_confirmed": tier_confirmed,
        "tier_confirmation_rule": (
            "requires_100_percent_reading_coverage" if tier_requires_usage else "not_applicable"
        ),
        "reading_coverage": reading_coverage,
        "tier_1_allowance_kwh": effective_tier_threshold,
        "price_per_kwh": effective_price,
        "base_price_per_kwh": period.price_per_kwh if period else None,
        "cca_adjustment_per_kwh": version.cca_adjustment_per_kwh,
        "surcharge_percent": version.surcharge_percent,
        "cumulative_cycle_kwh": cumulative_kwh,
        "period_start_minute": period.start_minute if period else None,
        "period_end_minute": period.end_minute if period else None,
        "next_change_at": next_change_at,
        "scope": account.cost_scope,
        "fixed_charges_included": account.cost_scope == "full_account",
        "baseline_credit_included": account.cost_scope == "full_account"
        and version.baseline_credit_per_kwh > 0,
        "cca_or_direct_access": account.cca_provider,
    }


@router.get("/home")
async def home_dashboard(
    home_id: str | None = None,
    device_id: str | None = None,
    aggregate_circuit_id: str | None = None,
    user: CurrentUser = Depends(require_permission("dashboard.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    if device_id and aggregate_circuit_id:
        raise PermissionDenied("select either one sensor or one verified aggregate")
    scoped_home_id = await _resolve_home_id(session, user.id, home_id)
    devices = (
        await session.scalars(
            select(Device)
            .where(Device.home_id == scoped_home_id, Device.revoked_at.is_(None))
            .order_by(Device.display_order, Device.id)
        )
    ).all()
    visible_devices = [device for device in devices if device.show_on_dashboard]
    now = datetime.now(UTC)
    output_devices: list[dict[str, object]] = []
    device_items: dict[str, dict[str, object]] = {}
    for device in devices:
        heartbeat = await session.scalar(
            select(DeviceHeartbeat)
            .where(DeviceHeartbeat.device_id == device.id)
            .order_by(DeviceHeartbeat.received_at.desc())
            .limit(1)
        )
        last_committed = await session.scalar(
            select(func.max(NormalizedInterval.end_utc))
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .where(
                NormalizedInterval.device_id == device.id,
                RawReading.reset_generation == device.reset_generation,
            )
        )
        age = (now - aware_utc(heartbeat.received_at)).total_seconds() if heartbeat else None
        state = (
            "monitoring_disabled"
            if not device.monitoring_enabled
            else "waiting"
            if heartbeat is None
            else "live"
            if age is not None and age <= 30 and heartbeat.pzem_status == "ok"
            else "needs_attention"
            if age is not None and age <= 30
            else "stale"
            if age is not None and age <= 120
            else "offline"
        )
        measurement = None
        if heartbeat is not None:
            measurement = {
                "voltage_v": heartbeat.voltage_v,
                "current_a": heartbeat.current_a,
                "active_power_w": heartbeat.active_power_w,
                "frequency_hz": heartbeat.frequency_hz,
                "power_factor": heartbeat.power_factor,
                "measured_at": heartbeat.measured_at,
                "pzem_status": heartbeat.pzem_status,
            }
        device_item: dict[str, object] = {
            "id": device.id,
            "home_id": device.home_id,
            "circuit_id": device.circuit_id,
            "friendly_name": device.friendly_name,
            "location": device.location,
            "state": state,
            "measurement_scope": device.measurement_scope,
            "measurement": measurement,
            "heartbeat_at": heartbeat.received_at if heartbeat else None,
            "last_committed_at": last_committed,
            "backlog": heartbeat.backlog if heartbeat else None,
            "storage_status": heartbeat.storage_status if heartbeat else "unavailable",
            "firmware_version": device.firmware_version,
        }
        device_items[device.id] = device_item
        if device.show_on_dashboard:
            output_devices.append(device_item)
    # Never infer a sum from unrelated sensors. When the operator has configured
    # exactly one verified, non-overlapping aggregate for this home, however, it
    # is the unambiguous default Home scope. Explicit sensor/aggregate query
    # parameters always take precedence.
    summary_kind = "selected_sensor"
    summary_home_id = scoped_home_id
    selected_aggregate_circuit_id = aggregate_circuit_id
    if device_id is None and selected_aggregate_circuit_id is None:
        designated = await session.scalar(
            select(Circuit).where(
                Circuit.home_id == scoped_home_id,
                Circuit.is_home_total.is_(True),
                Circuit.aggregate_mode == "verified_sum",
                Circuit.non_overlapping_confirmed.is_(True),
            )
        )
        if designated is not None:
            selected_aggregate_circuit_id = designated.id
    if device_id is None and selected_aggregate_circuit_id is None:
        candidate_members: dict[str, tuple[str, ...]] = {}
        for candidate_id in {
            device.circuit_id
            for device in devices
            if device.circuit_id is not None and device.include_in_aggregate
        }:
            candidate = await session.scalar(
                select(Circuit).where(
                    Circuit.id == candidate_id,
                    Circuit.home_id == scoped_home_id,
                    Circuit.aggregate_mode == "verified_sum",
                )
            )
            if candidate is None:
                continue
            members = tuple(
                device.id
                for device in devices
                if device.circuit_id == candidate.id and device.include_in_aggregate
            )
            if len(members) >= 2:
                candidate_members[candidate.id] = members
        if len(candidate_members) == 1:
            selected_aggregate_circuit_id = next(iter(candidate_members))
    if selected_aggregate_circuit_id:
        circuit = await session.scalar(
            select(Circuit).where(
                Circuit.id == selected_aggregate_circuit_id,
                Circuit.home_id == scoped_home_id,
                Circuit.aggregate_mode == "verified_sum",
            )
        )
        if circuit is None:
            raise NotFound("verified aggregate does not exist")
        ids = tuple(
            (
                await session.scalars(
                    select(Device.id)
                    .where(
                        Device.circuit_id == circuit.id,
                        Device.include_in_aggregate.is_(True),
                    )
                    .order_by(Device.display_order, Device.id)
                )
            ).all()
        )
        if not ids:
            raise NotFound("verified aggregate has no sensors")
        summary_kind = "verified_aggregate"
    elif device_id:
        selected_device = next((device for device in devices if device.id == device_id), None)
        if selected_device is None:
            raise NotFound("device does not exist")
        ids = (device_id,)
    else:
        # Preserve the one-sensor default scope, but do not pin the dashboard to
        # a failed first sensor when another visible sensor has a fresh,
        # authenticated meter reading. Multi-device totals still require an
        # an operator-configured verified_sum circuit.
        default_item: dict[str, object] | None = None
        for output_item in output_devices:
            item_measurement = output_item.get("measurement")
            if (
                output_item["state"] == "live"
                and isinstance(item_measurement, dict)
                and item_measurement.get("active_power_w") is not None
            ):
                default_item = output_item
                break
        if default_item is None and output_devices:
            default_item = output_devices[0]
        ids = (str(default_item["id"]),) if default_item is not None else ()
    home = await session.get(Home, summary_home_id)
    account = await session.scalar(
        select(UtilityAccount).where(UtilityAccount.home_id == summary_home_id)
    )
    timezone = account.timezone if account is not None else home.timezone if home else "UTC"
    today_bounds = _utc_bounds(timezone, now, "today")
    week_bounds = _utc_bounds(timezone, now, "week")
    month_bounds = _utc_bounds(timezone, now, "month")
    yesterday_bounds, last_week_bounds = _comparison_bounds(timezone, now)
    billing_bounds = _billing_cycle_bounds(timezone, account.billing_day if account else 1, now)
    current_rate = await _current_rate(session, summary_home_id, now, ids, billing_bounds[0])
    homes_by_id = {
        home_id: await session.get(Home, home_id)
        for home_id in {device.home_id for device in devices}
    }
    accounts_by_home_id = {
        home_id: await session.scalar(
            select(UtilityAccount).where(UtilityAccount.home_id == home_id)
        )
        for home_id in homes_by_id
    }
    devices_by_id = {device.id: device for device in visible_devices}
    for output_item in output_devices:
        device = devices_by_id[str(output_item["id"])]
        card_account = accounts_by_home_id[device.home_id]
        card_home = homes_by_id[device.home_id]
        card_timezone = (
            card_account.timezone
            if card_account is not None
            else card_home.timezone
            if card_home is not None
            else "UTC"
        )
        card_cycle_start = _billing_cycle_bounds(
            card_timezone, card_account.billing_day if card_account is not None else 1, now
        )[0]
        card_rate = await _current_rate(
            session, device.home_id, now, (device.id,), card_cycle_start
        )
        if card_rate is None or card_rate.get("price_per_kwh") is None:
            continue
        price = Decimal(str(card_rate["price_per_kwh"]))
        item_measurement = output_item.get("measurement")
        power = (
            item_measurement.get("active_power_w") if isinstance(item_measurement, dict) else None
        )
        output_item["estimated_cost_per_hour"] = (
            Decimal(current_cost_per_hour_microdollars(Decimal(str(power)), price))
            / Decimal(1_000_000)
            if power is not None
            else None
        )
    aggregate_measurement: dict[str, object] | None = None
    if summary_kind == "verified_aggregate":
        aggregate_members = [device_items.get(member_id) for member_id in ids]
        powers: list[Decimal] = []
        for aggregate_item in aggregate_members:
            if aggregate_item is None:
                continue
            item_measurement = aggregate_item.get("measurement")
            if aggregate_item["state"] != "live" or not isinstance(item_measurement, dict):
                continue
            power = item_measurement.get("active_power_w")
            if power is not None:
                powers.append(Decimal(str(power)))
        aggregate_measurement = {
            "state": "live" if len(powers) == len(aggregate_members) else "unavailable",
            "active_power_w": sum(powers) if len(powers) == len(aggregate_members) else None,
            "member_device_ids": ids,
            "voltage_v": None,
            "frequency_hz": None,
            "power_factor": None,
        }
    summaries = {
        "today": await _summary(session, ids, *today_bounds),
        "yesterday": await _summary(session, ids, *yesterday_bounds),
        "week": await _summary(session, ids, *week_bounds),
        "last_week": await _summary(session, ids, *last_week_bounds),
        "month": await _summary(session, ids, *month_bounds),
        "billing_cycle": await _summary(session, ids, *billing_bounds),
    }
    estimate_scope_kind = "energy_only"
    estimate_scope_id = ids[0] if len(ids) == 1 else selected_aggregate_circuit_id
    if account is not None and account.cost_scope != "energy_only":
        configured = {
            device.id
            for device in devices
            if device.home_id == summary_home_id and device.measurement_scope == account.cost_scope
        }
        if configured and configured == set(ids):
            estimate_scope_kind = account.cost_scope
    billing_estimate = None
    if account is not None and estimate_scope_id is not None:
        billing_estimate = await session.scalar(
            select(BillingEstimate)
            .join(
                BillingEstimateSelection,
                BillingEstimateSelection.billing_estimate_id == BillingEstimate.id,
            )
            .where(
                BillingEstimateSelection.utility_account_id == account.id,
                BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                BillingEstimateSelection.scope_kind == estimate_scope_kind,
                BillingEstimateSelection.scope_id == estimate_scope_id,
            )
        )
    if billing_estimate is not None:
        summaries["billing_cycle"] = {
            **summaries["billing_cycle"],
            "cost": Decimal(billing_estimate.total_microdollars) / Decimal(1_000_000),
            "energy_cost": Decimal(billing_estimate.energy_cost_microdollars) / Decimal(1_000_000),
            "fixed_charge": Decimal(billing_estimate.fixed_charge_microdollars)
            / Decimal(1_000_000),
            "credits": Decimal(billing_estimate.credit_microdollars) / Decimal(1_000_000),
            "rate_plan_version_id": billing_estimate.rate_plan_version_id,
            "estimate_scope": billing_estimate.scope_kind,
        }
    return {
        "generated_at": now,
        "devices": output_devices,
        "summaries": summaries,
        "current_rate": current_rate,
        "aggregate_measurement": aggregate_measurement,
        "summary_scope": {
            "kind": summary_kind if ids else "unavailable",
            "device_id": ids[0] if len(ids) == 1 else None,
            "device_ids": ids,
            "aggregate": summary_kind == "verified_aggregate",
            "circuit_id": selected_aggregate_circuit_id,
        },
        "disclosure": {
            "usage_source": "authenticated PZEM-004T sensor intervals only",
            "estimated_not_utility_bill": True,
        },
    }


def _bucket_seconds(start: datetime, end: datetime, requested: int | None) -> int:
    if requested:
        if requested not in (60, 300, 900, 3600, 86400):
            raise InvalidRequest("unsupported History resolution")
        return requested
    seconds = (end - start).total_seconds()
    if seconds <= 86_400:
        return 300
    if seconds <= 7 * 86_400:
        return 900
    if seconds <= 31 * 86_400:
        return 3600
    return 86_400


@router.get("/history")
async def history(
    from_utc: datetime = Query(alias="from"),
    to_utc: datetime = Query(alias="to"),
    home_id: str | None = None,
    metric: Literal[
        "power", "voltage", "current", "frequency", "power_factor", "energy", "cost"
    ] = "power",
    device_id: str | None = None,
    aggregate_circuit_id: str | None = None,
    resolution_seconds: int | None = None,
    user: CurrentUser = Depends(require_permission("history.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    if from_utc.utcoffset() is None or to_utc.utcoffset() is None:
        raise InvalidRequest("History timestamps must include a UTC offset")
    if device_id and aggregate_circuit_id:
        raise InvalidRequest("select either one sensor or one verified aggregate")
    start = from_utc.astimezone(UTC)
    end = to_utc.astimezone(UTC)
    if end <= start or end - start > timedelta(days=366):
        raise InvalidRequest("History range must be ordered and no longer than 366 days")
    scoped_home_id = await _resolve_home_id(session, user.id, home_id)
    device_ids = tuple(
        (
            await session.scalars(
                select(Device.id)
                .where(Device.home_id == scoped_home_id, Device.revoked_at.is_(None))
                .order_by(Device.id)
            )
        ).all()
    )
    aggregate = False
    selected_circuit: Circuit | None = None
    if aggregate_circuit_id:
        selected_circuit = await session.scalar(
            select(Circuit).where(
                Circuit.id == aggregate_circuit_id,
                Circuit.home_id == scoped_home_id,
                Circuit.aggregate_mode == "verified_sum",
            )
        )
        if selected_circuit is None:
            raise NotFound("verified aggregate does not exist")
        device_ids = tuple(
            device.id
            for device in (
                await session.scalars(
                    select(Device).where(
                        Device.circuit_id == selected_circuit.id,
                        Device.home_id == scoped_home_id,
                        Device.include_in_aggregate.is_(True),
                    )
                )
            ).all()
        )
        if not device_ids:
            raise NotFound("verified aggregate has no sensors")
        aggregate = True
    elif device_id:
        if device_id not in device_ids:
            raise NotFound("device does not exist")
        device_ids = (device_id,)
    else:
        selected_circuit = await session.scalar(
            select(Circuit).where(
                Circuit.home_id == scoped_home_id,
                Circuit.is_home_total.is_(True),
                Circuit.aggregate_mode == "verified_sum",
                Circuit.non_overlapping_confirmed.is_(True),
            )
        )
        if selected_circuit is not None:
            aggregate_circuit_id = selected_circuit.id
            device_ids = tuple(
                (
                    await session.scalars(
                        select(Device.id)
                        .where(
                            Device.circuit_id == selected_circuit.id,
                            Device.home_id == scoped_home_id,
                            Device.include_in_aggregate.is_(True),
                        )
                        .order_by(Device.display_order, Device.id)
                    )
                ).all()
            )
            if not device_ids:
                raise NotFound("home-total service branch has no sensors")
            aggregate = True
        elif len(device_ids) > 1:
            device_ids = (device_ids[0],)
    bucket = _bucket_seconds(start, end, resolution_seconds)
    rows = (
        await session.execute(
            select(NormalizedInterval, RawReading)
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(device_ids),
                RawReading.reset_generation == Device.reset_generation,
                NormalizedInterval.start_utc >= start,
                NormalizedInterval.end_utc <= end,
            )
            .order_by(NormalizedInterval.start_utc)
        )
    ).all()
    cost_rows = (
        await session.execute(
            select(
                IntervalCostSelection.normalized_interval_id,
                func.sum(IntervalCost.energy_cost_microdollars - IntervalCost.credit_microdollars),
            )
            .join(
                IntervalCostSelection,
                IntervalCostSelection.interval_cost_id == IntervalCost.id,
            )
            .join(
                NormalizedInterval,
                NormalizedInterval.id == IntervalCostSelection.normalized_interval_id,
            )
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(device_ids),
                RawReading.reset_generation == Device.reset_generation,
                NormalizedInterval.start_utc >= start,
                NormalizedInterval.end_utc <= end,
            )
            .group_by(IntervalCostSelection.normalized_interval_id)
        )
    ).all()
    cost_by_interval: dict[str, int] = {
        interval_id: int(total or 0) for interval_id, total in cost_rows
    }
    bucket_origin = datetime.fromtimestamp(int(start.timestamp()) // bucket * bucket, UTC)
    bucket_count = math.ceil((end - bucket_origin).total_seconds() / bucket)
    if bucket_count > 20_000:
        raise InvalidRequest("History resolution would exceed the 20,000-point response limit")
    buckets: dict[int, dict[str, list[tuple[NormalizedInterval, RawReading]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for interval, raw in rows:
        offset = max(0, int((aware_utc(interval.start_utc) - bucket_origin).total_seconds()))
        buckets[offset // bucket][interval.device_id].append((interval, raw))
    points: list[dict[str, object]] = []
    field_name = {
        "voltage": "voltage_mv",
        "current": "current_ma",
        "frequency": "frequency_mhz",
        "power_factor": "power_factor_milli",
    }.get(metric)
    divisors = {"voltage": 1000, "current": 1000, "frequency": 1000, "power_factor": 1000}
    all_membership_complete = True
    total_quality_seconds = Decimal(0)
    expected_quality_seconds = Decimal(str((end - start).total_seconds() * len(device_ids)))
    for bucket_index in range(bucket_count):
        bucket_start = bucket_origin + timedelta(seconds=bucket_index * bucket)
        bucket_end = bucket_start + timedelta(seconds=bucket)
        visible_start = max(start, bucket_start)
        visible_end = min(end, bucket_end)
        visible_seconds = max(0.0, (visible_end - visible_start).total_seconds())
        expected_intervals = max(1, math.ceil(visible_seconds / 60))
        expected_seconds = Decimal(expected_intervals * 60)
        by_device = buckets.get(bucket_index, {})
        device_coverage: dict[str, Decimal] = {}
        device_quality: dict[str, Decimal] = {}
        for selected_device_id in device_ids:
            intervals = by_device.get(selected_device_id, [])
            coverage = Decimal(0)
            quality_seconds = Decimal(0)
            for interval, _raw in sorted(intervals, key=lambda item: item[0].start_utc):
                seconds = Decimal(
                    str(
                        max(
                            0.0,
                            (
                                aware_utc(interval.end_utc) - aware_utc(interval.start_utc)
                            ).total_seconds(),
                        )
                    )
                )
                coverage += seconds
                quality_seconds += seconds * interval.completeness
            device_coverage[selected_device_id] = coverage
            device_quality[selected_device_id] = quality_seconds
            total_quality_seconds += quality_seconds
        membership_complete = all(
            device_coverage.get(selected_device_id, Decimal(0)) >= expected_seconds
            for selected_device_id in device_ids
        )
        all_membership_complete = all_membership_complete and membership_complete
        values = [item for intervals in by_device.values() for item in intervals]
        energy_mwh = sum(interval.energy_mwh for interval, _raw in values)
        costs_complete = (
            (membership_complete or not aggregate)
            and bool(values)
            and all(interval.id in cost_by_interval for interval, _raw in values)
        )
        cost_micro = (
            sum(cost_by_interval[interval.id] for interval, _raw in values) if costs_complete else 0
        )
        value: Decimal | None = None
        if values and (membership_complete or not aggregate):
            if metric == "power":
                device_means: list[Decimal] = []
                for selected_device_id in device_ids:
                    intervals = by_device[selected_device_id]
                    if any(interval.average_power_mw is None for interval, _raw in intervals):
                        device_means = []
                        break
                    weighted = Decimal(0)
                    duration = Decimal(0)
                    for interval, _raw in intervals:
                        seconds = Decimal(
                            str(
                                (
                                    aware_utc(interval.end_utc) - aware_utc(interval.start_utc)
                                ).total_seconds()
                            )
                        )
                        weighted += Decimal(interval.average_power_mw or 0) * seconds
                        duration += seconds
                    if duration <= 0:
                        device_means = []
                        break
                    device_means.append(weighted / duration)
                if len(device_means) == len(device_ids):
                    value = sum(device_means, Decimal(0)) / Decimal(1_000_000)
            elif metric == "energy":
                value = Decimal(energy_mwh) / Decimal(1_000_000)
            elif metric == "cost":
                value = Decimal(cost_micro) / Decimal(1_000_000) if costs_complete else None
            elif not aggregate:
                assert field_name is not None
                device_means = []
                for selected_device_id in device_ids:
                    intervals = by_device[selected_device_id]
                    if any(getattr(raw, field_name) is None for _interval, raw in intervals):
                        device_means = []
                        break
                    weighted = Decimal(0)
                    duration = Decimal(0)
                    for interval, raw in intervals:
                        seconds = Decimal(
                            str(
                                (
                                    aware_utc(interval.end_utc) - aware_utc(interval.start_utc)
                                ).total_seconds()
                            )
                        )
                        weighted += Decimal(getattr(raw, field_name)) * seconds
                        duration += seconds
                    if duration <= 0:
                        device_means = []
                        break
                    device_means.append(weighted / duration)
                if len(device_means) == len(device_ids):
                    combined = sum(device_means, Decimal(0)) / Decimal(len(device_means))
                    value = combined / Decimal(divisors[metric])
        quality = (
            sum(device_quality.values(), Decimal(0)) / (expected_seconds * Decimal(len(device_ids)))
            if expected_seconds > 0 and device_ids
            else Decimal(0)
        )
        points.append(
            {
                "timestamp": bucket_start,
                "value": value,
                "cost": Decimal(cost_micro) / Decimal(1_000_000) if costs_complete else None,
                "quality": min(Decimal(1), quality),
            }
        )
    missing_ranges: list[dict[str, datetime | str]] = []
    if not device_ids:
        missing_ranges.append({"start": start, "end": end})
    for selected_device_id in device_ids:
        prior = start
        device_rows = sorted(
            (item for item in rows if item[0].device_id == selected_device_id),
            key=lambda item: item[0].start_utc,
        )
        for interval, _raw in device_rows:
            interval_start = aware_utc(interval.start_utc)
            interval_end = aware_utc(interval.end_utc)
            if interval_start > prior:
                missing_ranges.append(
                    {"device_id": selected_device_id, "start": prior, "end": interval_start}
                )
            prior = max(prior, interval_end)
        if end > prior:
            missing_ranges.append({"device_id": selected_device_id, "start": prior, "end": end})
    energy_total = sum(interval.energy_mwh for interval, _raw in rows)
    all_costs_complete = bool(rows) and all(
        interval.id in cost_by_interval for interval, _raw in rows
    )
    cost_total = (
        sum(cost_by_interval[interval.id] for interval, _raw in rows) if all_costs_complete else 0
    )
    overall_complete = all_membership_complete or not aggregate
    return {
        "points": points,
        "energy_kwh": Decimal(energy_total) / Decimal(1_000_000) if overall_complete else None,
        "cost": (
            Decimal(cost_total) / Decimal(1_000_000)
            if all_costs_complete and overall_complete
            else None
        ),
        "completeness": (
            min(Decimal(1), total_quality_seconds / expected_quality_seconds)
            if expected_quality_seconds > 0
            else Decimal(0)
        ),
        "missing_ranges": missing_ranges,
        "resolution_seconds": bucket,
        "timezone": "UTC",
        "usage_source": "authenticated PZEM-004T sensor intervals only",
        "scope": {
            "device_ids": device_ids,
            "aggregate": aggregate,
            "circuit_id": aggregate_circuit_id,
        },
        "aggregation": {
            "power": "sum_of_per_device_time_weighted_means",
            "energy": "sum_of_authenticated_intervals",
            "cost": "sum_only_when_every_visible_interval_is_priced",
            "missing_policy": "explicit_null_bucket_if_any_selected_device_lacks_time_coverage",
        },
    }


@router.get("/history/export.csv")
async def history_csv(
    from_utc: datetime = Query(alias="from"),
    to_utc: datetime = Query(alias="to"),
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("history.export")),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if from_utc.utcoffset() is None or to_utc.utcoffset() is None:
        raise InvalidRequest("CSV timestamps must include a UTC offset")
    if to_utc <= from_utc or to_utc - from_utc > timedelta(days=366):
        raise InvalidRequest("CSV range must be ordered and no longer than 366 days")
    scoped_home_id = await _resolve_home_id(session, user.id, home_id)
    rows = (
        await session.execute(
            select(NormalizedInterval, RawReading)
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                Device.home_id == scoped_home_id,
                RawReading.reset_generation == Device.reset_generation,
                NormalizedInterval.start_utc >= from_utc.astimezone(UTC),
                NormalizedInterval.end_utc <= to_utc.astimezone(UTC),
            )
            .order_by(NormalizedInterval.start_utc)
        )
    ).all()
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        ("device_id", "sequence", "start_utc", "end_utc", "energy_kwh", "completeness", "source")
    )
    for interval, raw in rows:
        writer.writerow(
            (
                interval.device_id,
                raw.sequence,
                interval.start_utc.isoformat(),
                interval.end_utc.isoformat(),
                str(Decimal(interval.energy_mwh) / Decimal(1_000_000)),
                str(interval.completeness),
                "authenticated_pzem",
            )
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=powermeter-history.csv"},
    )


@router.get("/devices")
async def list_devices(
    home_id: str | None = None,
    user: CurrentUser = Depends(require_permission("sensors.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _home_ids(session, user.id)
    scoped_home_id = await _resolve_home_id(session, user.id, home_id)
    home_scopes = (
        await session.execute(
            select(Home.id, Home.name).where(Home.id.in_(homes)).order_by(Home.name, Home.id)
        )
    ).all()
    devices = (
        await session.scalars(
            select(Device)
            .where(Device.home_id == scoped_home_id)
            .order_by(Device.display_order, Device.id)
        )
    ).all()
    result: list[dict[str, object]] = []
    for device in devices:
        heartbeats = (
            await session.scalars(
                select(DeviceHeartbeat)
                .where(DeviceHeartbeat.device_id == device.id)
                .order_by(DeviceHeartbeat.received_at.desc())
                .limit(2)
            )
        ).all()
        heartbeat = heartbeats[0] if heartbeats else None
        prior_heartbeat = heartbeats[1] if len(heartbeats) > 1 else None
        last_command = await session.scalar(
            select(DeviceCommand)
            .where(DeviceCommand.device_id == device.id)
            .order_by(DeviceCommand.issued_at.desc())
            .limit(1)
        )
        active_credential = await session.scalar(
            select(DeviceCredential)
            .where(
                DeviceCredential.device_id == device.id,
                DeviceCredential.state == "active",
                DeviceCredential.revoked_at.is_(None),
            )
            .order_by(DeviceCredential.key_version.desc())
        )
        rotation = await session.scalar(
            select(DeviceCredential)
            .where(
                DeviceCredential.device_id == device.id,
                DeviceCredential.state.in_(("pending", "prepared")),
                DeviceCredential.revoked_at.is_(None),
            )
            .order_by(DeviceCredential.key_version.desc())
        )
        queue_drain_rate = None
        if heartbeat is not None and prior_heartbeat is not None:
            elapsed_minutes = (
                aware_utc(heartbeat.received_at) - aware_utc(prior_heartbeat.received_at)
            ).total_seconds() / 60
            if elapsed_minutes > 0:
                queue_drain_rate = Decimal(prior_heartbeat.backlog - heartbeat.backlog) / Decimal(
                    str(elapsed_minutes)
                )
        missing_prefix_status = "unavailable"
        if heartbeat is not None and heartbeat.oldest_sequence is not None:
            missing_prefix_status = (
                "detected" if device.contiguous_ack + 1 < heartbeat.oldest_sequence else "none"
            )
        synchronization_errors = (
            [
                flag
                for flag in heartbeat.health_flags
                if flag.startswith(("BACKLOG_", "MISSING_PREFIX_", "SYNC_"))
            ]
            if heartbeat is not None
            else []
        )
        result.append(
            {
                "id": device.id,
                "home_id": device.home_id,
                "circuit_id": device.circuit_id,
                "friendly_name": device.friendly_name,
                "location": device.location,
                "notes": device.notes,
                "display_order": device.display_order,
                "include_in_aggregate": device.include_in_aggregate,
                "show_on_dashboard": device.show_on_dashboard,
                "monitoring_enabled": device.monitoring_enabled,
                "device_fingerprint": hashlib.sha256(device.id.encode()).hexdigest()[:12],
                "credential_fingerprint": active_credential.fingerprint
                if active_credential
                else None,
                "credential_key_version": active_credential.key_version
                if active_credential
                else None,
                "credential_rotation": {
                    "rotation_id": rotation.rotation_id,
                    "credential_fingerprint": rotation.fingerprint,
                    "state": rotation.state,
                    "overlap_expires_at": rotation.overlap_expires_at,
                    "prepare_command_id": rotation.prepare_command_id,
                    "commit_command_id": rotation.commit_command_id,
                    "cancel_command_id": rotation.cancel_command_id,
                }
                if rotation
                else None,
                "firmware_version": device.firmware_version,
                "protocol": device.protocol_id,
                "pzem_variant": device.pzem_variant,
                "ct_rating_a": device.ct_rating_a,
                "measurement_scope": device.measurement_scope,
                "heartbeat_at": heartbeat.received_at if heartbeat else None,
                "wifi_rssi": heartbeat.wifi_rssi if heartbeat else None,
                "ip_address": heartbeat.ip_address if heartbeat else None,
                "pzem_status": (
                    "monitoring_disabled"
                    if not device.monitoring_enabled
                    else heartbeat.pzem_status
                    if heartbeat
                    else "unavailable"
                ),
                "storage_status": heartbeat.storage_status if heartbeat else "unavailable",
                "storage_bytes_total": heartbeat.storage_bytes_total if heartbeat else None,
                "storage_bytes_free": heartbeat.storage_bytes_free if heartbeat else None,
                "oldest_sequence": heartbeat.oldest_sequence if heartbeat else None,
                "newest_sequence": heartbeat.newest_sequence if heartbeat else None,
                "acknowledgement": device.contiguous_ack,
                "backlog": heartbeat.backlog if heartbeat else None,
                "synchronization": {
                    "server_contiguous_acknowledgement": device.contiguous_ack,
                    "earliest_sd_sequence": heartbeat.oldest_sequence if heartbeat else None,
                    "latest_sd_sequence": heartbeat.newest_sequence if heartbeat else None,
                    "queued_records": heartbeat.backlog if heartbeat else None,
                    "last_attempted_batch_start": None,
                    "last_attempted_batch_end": None,
                    "last_batch_start": None,
                    "last_batch_end": None,
                    "selected_record_count": None,
                    "measured_serialized_bytes": None,
                    "serialized_bytes": None,
                    "http_result": None,
                    "last_accepted_sequence": device.contiguous_ack,
                    "missing_prefix_status": missing_prefix_status,
                    "last_synchronization_error": synchronization_errors[0]
                    if synchronization_errors
                    else None,
                    "last_error": synchronization_errors[0] if synchronization_errors else None,
                    "queue_drain_rate_per_minute": queue_drain_rate,
                    "queue_drain_rate": queue_drain_rate,
                    "unavailable_fields_reason": (
                        "not reported by pm-protocol/1.0.0"
                        if heartbeat is not None
                        else "no authenticated heartbeat"
                    ),
                },
                "free_internal_heap": heartbeat.free_internal_heap if heartbeat else None,
                "largest_internal_block": heartbeat.largest_internal_block if heartbeat else None,
                "last_reboot_reason": heartbeat.reboot_reason if heartbeat else None,
                "last_command": {
                    "id": last_command.id,
                    "type": last_command.command_type,
                    "state": last_command.state,
                    "progress_percent": last_command.progress_percent,
                    "expires_at": last_command.expires_at,
                    "result_code": (last_command.last_result or {}).get("result_code"),
                    "result_evidence": _public_command_evidence(last_command),
                }
                if last_command
                else None,
            }
        )
    return {
        "home_scopes": [{"id": home_id, "name": name} for home_id, name in home_scopes],
        "devices": result,
    }


@router.post("/devices/{device_id}/commands", status_code=202)
async def queue_command(
    device_id: str,
    payload: CommandCreateRequest,
    user: CurrentUser = Depends(require_permission("sensors.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    device = await _scoped_device(session, user.id, device_id)
    required = COMMAND_PERMISSIONS[payload.command_type]
    if required not in user.permissions:
        raise PermissionDenied(f"permission required: {required}")
    if payload.command_type.endswith("_commit"):
        expected_phrase = COMMIT_CONFIRMATION_PHRASES[payload.command_type]
        if payload.typed_confirmation != expected_phrase:
            raise InvalidRequest(f"type {expected_phrase} exactly to authorize this command")
    command_payload: dict[str, Any] = dict(payload.payload)
    existing = await session.scalar(
        select(DeviceCommand).where(
            DeviceCommand.device_id == device_id,
            DeviceCommand.idempotency_key == payload.idempotency_key,
        )
    )
    if existing is not None and (
        payload.command_type.endswith("_commit") or payload.command_type == "data_reset_cancel"
    ):
        expected_payload = dict(command_payload)
        if payload.command_type.endswith("_commit") and payload.prepare_command_id:
            expected_payload["prepare_command_id"] = payload.prepare_command_id
            expected_payload["confirmation_token"] = payload.confirmation_token
            if existing.command_type == "data_reset_commit":
                expected_payload["reset_generation"] = existing.payload.get("reset_generation")
                expected_payload["sequence_floor"] = existing.payload.get("sequence_floor")
        if existing.command_type != payload.command_type or existing.payload != expected_payload:
            raise IntegrityConflict("idempotency key was already used for a different command")
        return {
            "command": {
                "id": existing.id,
                "type": existing.command_type,
                "state": existing.state,
                "expires_at": existing.expires_at,
            },
            "confirmation_token": None,
        }
    if payload.command_type == "maintenance_sleep":
        seconds = command_payload.get("seconds")
        if not isinstance(seconds, int) or not 30 <= seconds <= 3600:
            raise InvalidRequest(
                "maintenance sleep must be a bounded duration from 30 to 3600 seconds"
            )
    if payload.command_type == "data_reset_prepare":
        if command_payload:
            raise InvalidRequest("data reset boundary is assigned only by the server")
        command_payload = {
            "reset_generation": device.reset_generation + 1,
            "server_sequence_floor": device.maximum_sequence,
        }
    if payload.command_type == "format_storage_prepare" and command_payload:
        raise InvalidRequest("storage format prepare payload is assigned only by the server")
    linked_prepare_expires_at: datetime | None = None
    if payload.command_type == "data_reset_cancel":
        if set(command_payload) != {"prepare_command_id"} or not isinstance(
            command_payload.get("prepare_command_id"), str
        ):
            raise InvalidRequest("data reset cancel requires exactly one prepare command ID")
        cancelled_prepare = await cancel_data_reset_prepare(
            session,
            device_id=device_id,
            prepare_command_id=str(command_payload["prepare_command_id"]),
        )
        linked_prepare_expires_at = cancelled_prepare.expires_at
    if payload.command_type.endswith("_commit"):
        assert payload.prepare_command_id and payload.confirmation_token
        prepare = await validate_commit_token(
            session,
            prepare_command_id=payload.prepare_command_id,
            confirmation_token=payload.confirmation_token,
            commit_command_type=payload.command_type,
        )
        if prepare.device_id != device_id:
            raise NotFound("prepare command does not belong to this device")
        linked_prepare_expires_at = prepare.expires_at
        command_payload["prepare_command_id"] = prepare.id
        command_payload["confirmation_token"] = payload.confirmation_token
        if payload.command_type == "data_reset_commit":
            generation = prepare.payload.get("reset_generation")
            server_floor = prepare.payload.get("server_sequence_floor")
            evidence = (prepare.last_result or {}).get("evidence")
            if (
                not isinstance(generation, int)
                or not isinstance(server_floor, int)
                or not isinstance(evidence, dict)
                or evidence.get("prepare_command_id") != prepare.id
                or evidence.get("reset_generation") != generation
                or evidence.get("server_sequence_floor") != server_floor
                or isinstance(evidence.get("sequence_floor"), bool)
                or not isinstance(evidence.get("sequence_floor"), int)
                or int(evidence["sequence_floor"]) < server_floor
                or evidence.get("ready") is not True
            ):
                raise IntegrityConflict("authenticated data reset prepare boundary is invalid")
            floor = int(evidence["sequence_floor"])
            command_payload["reset_generation"] = generation
            command_payload["sequence_floor"] = floor
    command, confirmation_token = await create_command(
        session,
        device_id=device_id,
        command_type=payload.command_type,
        issued_by_user_id=user.id,
        idempotency_key=payload.idempotency_key,
        payload=command_payload,
        expires_at=linked_prepare_expires_at,
    )
    await session.commit()
    return {
        "command": {
            "id": command.id,
            "type": command.command_type,
            "state": command.state,
            "expires_at": command.expires_at,
        },
        "confirmation_token": confirmation_token,
    }


@router.get("/alerts")
async def alerts(
    user: CurrentUser = Depends(require_permission("dashboard.view")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    homes = await _home_ids(session, user.id)
    rows = (
        await session.scalars(
            select(Alert)
            .where(Alert.home_id.in_(homes), Alert.state.in_(("open", "acknowledged")))
            .order_by(Alert.opened_at.desc())
            .limit(100)
        )
    ).all()
    return {
        "alerts": [
            {
                "id": row.id,
                "type": row.alert_type,
                "severity": row.severity,
                "state": row.state,
                "opened_at": row.opened_at,
                "acknowledged_at": row.acknowledged_at,
                "silenced_until": row.silenced_until,
                "notification_suppressed": bool(
                    row.silenced_until and aware_utc(row.silenced_until) > datetime.now(UTC)
                ),
                "evidence": row.evidence,
            }
            for row in rows
        ]
    }


@router.get("/events")
async def server_events(
    request: Request,
    _user: CurrentUser = Depends(require_permission("dashboard.view")),
) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        event_id = 0
        while not await request.is_disconnected():
            event_id += 1
            payload = f'{{"generated_at":"{datetime.now(UTC).isoformat()}"}}'
            yield f"id: {event_id}\nevent: refresh\ndata: {payload}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
