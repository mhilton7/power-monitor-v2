from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from zoneinfo import ZoneInfo

import orjson
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import MAX_SENSOR_TIME_SKEW_SECONDS, PROTOCOL_ID, TELEMETRY_PROTOCOL_ID
from ..errors import IntegrityConflict, NotFound
from ..models import (
    Alert,
    BillingEstimateSelection,
    Device,
    DeviceTelemetryState,
    Home,
    HomeTelemetrySetting,
    IntervalCostSelection,
    NormalizedInterval,
    StatelessTelemetrySample,
    TelemetryCutover,
    TelemetryEnergyEvent,
    UtilityAccount,
    aware_utc,
)
from ..schemas.device import StatelessTelemetryRequest


@dataclass(frozen=True)
class StatelessIngestionResult:
    status: Literal["accepted", "duplicate"]
    received_at: datetime
    timestamp_source: Literal["sensor", "server"]
    config_version: int
    telemetry_interval_seconds: Literal[2, 5, 10, 15, 30, 60]
    advances_live_state: bool


def stateless_payload_hash(payload: StatelessTelemetryRequest) -> str:
    """Hash immutable measurement content, excluding retry-time command results."""

    body = orjson.dumps(
        payload.model_dump(mode="json", exclude={"command_results"}),
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(body).hexdigest()


async def telemetry_settings_for_home(session: AsyncSession, home_id: str) -> HomeTelemetrySetting:
    home = await session.scalar(select(Home).where(Home.id == home_id).with_for_update())
    if home is None:
        raise NotFound("home does not exist")
    settings = await session.get(HomeTelemetrySetting, home_id)
    if settings is None:
        settings = HomeTelemetrySetting(home_id=home_id)
        session.add(settings)
        await session.flush()
    return settings


def _history_bucket(instant: datetime, seconds: int) -> tuple[datetime, datetime]:
    epoch_seconds = math.floor(aware_utc(instant).timestamp())
    start_seconds = epoch_seconds - (epoch_seconds % seconds)
    start = datetime.fromtimestamp(start_seconds, tz=UTC)
    return start, start + timedelta(seconds=seconds)


def _scaled(value: Decimal | None, multiplier: int) -> int | None:
    if value is None:
        return None
    return int(value * multiplier)


def _incremental_average(prior: int | None, count: int, current: int | None) -> int | None:
    if current is None:
        return prior
    if prior is None or count <= 0:
        return current
    return ((prior * count) + current) // (count + 1)


def _cycle_key(instant: datetime, account: UtilityAccount) -> tuple[int, int]:
    local = aware_utc(instant).astimezone(ZoneInfo(account.timezone))
    year, month = local.year, local.month
    if local.day < account.billing_day:
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return year, month


async def _finalize_prior_buckets(
    session: AsyncSession, device_id: str, current_start: datetime
) -> None:
    rows = (
        await session.scalars(
            select(NormalizedInterval).where(
                NormalizedInterval.device_id == device_id,
                NormalizedInterval.source_kind == "stateless_v2",
                NormalizedInterval.finalized.is_(False),
                NormalizedInterval.start_utc < current_start,
            )
        )
    ).all()
    for row in rows:
        row.finalized = True
        row.completeness = min(
            Decimal("1"),
            Decimal(row.received_sample_count) / Decimal(row.expected_sample_count),
        )
        if row.gap_status != "connection_gap":
            row.gap_status = (
                "complete" if row.received_sample_count >= row.expected_sample_count else "partial"
            )


async def _invalidate_mutable_cost_selections(
    session: AsyncSession, device: Device, interval_id: str
) -> None:
    await session.execute(
        delete(IntervalCostSelection).where(
            IntervalCostSelection.normalized_interval_id == interval_id
        )
    )
    account_ids = select(UtilityAccount.id).where(UtilityAccount.home_id == device.home_id)
    await session.execute(
        delete(BillingEstimateSelection).where(
            BillingEstimateSelection.utility_account_id.in_(account_ids)
        )
    )


async def _record_energy_evidence(
    session: AsyncSession,
    *,
    device: Device,
    sample: StatelessTelemetrySample,
    previous_delivery: StatelessTelemetrySample | None,
    previous_energy: StatelessTelemetrySample | None,
    telemetry_interval_seconds: int,
) -> tuple[int | None, bool]:
    """Return allocatable bucket energy in mWh and whether a connection gap exists."""

    if previous_delivery is None:
        return None, False
    prior_total = previous_energy.pzem_energy_wh if previous_energy is not None else None
    current_total = sample.pzem_energy_wh if sample.pzem_status == "ok" else None
    threshold = timedelta(seconds=telemetry_interval_seconds * 2)
    delivery_elapsed = aware_utc(sample.effective_at) - aware_utc(previous_delivery.effective_at)
    energy_elapsed = (
        aware_utc(sample.effective_at) - aware_utc(previous_energy.effective_at)
        if previous_energy is not None and current_total is not None
        else None
    )
    connection_gap = delivery_elapsed > threshold or bool(
        energy_elapsed is not None and energy_elapsed > threshold
    )
    energy_baseline_at = (
        previous_energy.effective_at
        if previous_energy is not None
        else previous_delivery.effective_at
    )

    if prior_total is not None and current_total is not None and current_total < prior_total:
        session.add(
            TelemetryEnergyEvent(
                home_id=device.home_id,
                device_id=device.id,
                sample_id=sample.id,
                event_type="counter_reset",
                gap_start_utc=energy_baseline_at,
                gap_end_utc=sample.effective_at,
                prior_energy_wh=prior_total,
                current_energy_wh=current_total,
                recovered_energy_mwh=None,
                billing_status="excluded",
                evidence={"negative_delta_prevented": True, "new_baseline_established": True},
            )
        )
        session.add(
            Alert(
                home_id=device.home_id,
                device_id=device.id,
                alert_type="pzem_energy_counter_reset",
                severity="warning",
                state="open",
                evidence={
                    "prior_energy_wh": prior_total,
                    "current_energy_wh": current_total,
                    "sample_id": sample.id,
                },
            )
        )
        return None, connection_gap

    delta_mwh = (
        (current_total - prior_total) * 1000
        if prior_total is not None and current_total is not None
        else None
    )
    if not connection_gap:
        return delta_mwh, False

    # An invalid first reconnect establishes that the History interval is
    # incomplete, but cannot classify cumulative energy. Defer immutable gap
    # evidence until a later valid counter can resolve or explicitly leave it
    # unresolved against the last valid energy baseline.
    if current_total is None:
        return None, True

    account = await session.scalar(
        select(UtilityAccount).where(UtilityAccount.home_id == device.home_id)
    )
    crosses_cycle = bool(
        account is not None
        and _cycle_key(energy_baseline_at, account) != _cycle_key(sample.effective_at, account)
    )
    recovered = delta_mwh if delta_mwh is not None and not crosses_cycle else None
    session.add(
        TelemetryEnergyEvent(
            home_id=device.home_id,
            device_id=device.id,
            sample_id=sample.id,
            event_type=(
                "connection_gap_recovered" if recovered is not None else "connection_gap_unresolved"
            ),
            gap_start_utc=aware_utc(energy_baseline_at)
            + timedelta(seconds=telemetry_interval_seconds),
            gap_end_utc=sample.effective_at,
            prior_energy_wh=prior_total,
            current_energy_wh=current_total,
            recovered_energy_mwh=recovered,
            billing_status="included" if recovered is not None else "unresolved",
            evidence={
                "power_curve_fabricated": False,
                "crosses_billing_cycle": crosses_cycle,
                "counter_delta_available": delta_mwh is not None,
            },
        )
    )
    return None, True


async def _upsert_history_bucket(
    session: AsyncSession,
    *,
    device: Device,
    sample: StatelessTelemetrySample,
    settings: HomeTelemetrySetting,
    energy_mwh: int | None,
    connection_gap: bool,
) -> None:
    start, end = _history_bucket(sample.effective_at, settings.history_interval_seconds)
    await _finalize_prior_buckets(session, device.id, start)
    bucket = await session.scalar(
        select(NormalizedInterval)
        .where(
            NormalizedInterval.device_id == device.id,
            NormalizedInterval.start_utc == start,
            NormalizedInterval.source_kind == "stateless_v2",
        )
        .with_for_update()
    )
    expected = max(
        1,
        math.ceil(settings.history_interval_seconds / settings.telemetry_interval_seconds),
    )
    valid = sample.pzem_status == "ok" and sample.active_power_w is not None
    power_mw = _scaled(sample.active_power_w, 1000) if valid else None
    voltage_mv = _scaled(sample.voltage_v, 1000) if valid else None
    current_ma = _scaled(sample.current_a, 1000) if valid else None
    frequency_mhz = _scaled(sample.frequency_hz, 1000) if valid else None
    pf_milli = _scaled(sample.power_factor, 1000) if valid else None
    if bucket is None:
        received_count = 1 if valid else 0
        bucket = NormalizedInterval(
            device_id=device.id,
            raw_reading_id=None,
            start_utc=start,
            end_utc=end,
            energy_mwh=energy_mwh,
            average_power_mw=power_mw,
            completeness=Decimal(received_count) / Decimal(expected),
            energy_selection="pzem_delta" if energy_mwh is not None else "unavailable_initial",
            algorithm_version="server-bucket-v2",
            source_authenticated=True,
            source_kind="stateless_v2",
            minimum_power_mw=power_mw,
            maximum_power_mw=power_mw,
            ending_voltage_mv=voltage_mv,
            ending_current_ma=current_ma,
            average_frequency_mhz=frequency_mhz,
            average_power_factor_milli=pf_milli,
            received_sample_count=received_count,
            expected_sample_count=expected,
            gap_status="connection_gap" if connection_gap else "partial",
            finalized=False,
            last_received_at=sample.received_at,
        )
        session.add(bucket)
        return

    prior_count = bucket.received_sample_count
    if valid:
        assert power_mw is not None
        bucket.received_sample_count = min(expected, prior_count + 1)
        bucket.average_power_mw = _incremental_average(
            bucket.average_power_mw, prior_count, power_mw
        )
        bucket.minimum_power_mw = (
            power_mw if bucket.minimum_power_mw is None else min(bucket.minimum_power_mw, power_mw)
        )
        bucket.maximum_power_mw = (
            power_mw if bucket.maximum_power_mw is None else max(bucket.maximum_power_mw, power_mw)
        )
        bucket.ending_voltage_mv = voltage_mv
        bucket.ending_current_ma = current_ma
        bucket.average_frequency_mhz = _incremental_average(
            bucket.average_frequency_mhz, prior_count, frequency_mhz
        )
        bucket.average_power_factor_milli = _incremental_average(
            bucket.average_power_factor_milli, prior_count, pf_milli
        )
    if energy_mwh is not None:
        bucket.energy_mwh = (bucket.energy_mwh or 0) + energy_mwh
        bucket.energy_selection = "pzem_delta"
    bucket.completeness = min(
        Decimal("1"),
        Decimal(bucket.received_sample_count) / Decimal(bucket.expected_sample_count),
    )
    if connection_gap:
        bucket.gap_status = "connection_gap"
    elif bucket.gap_status != "connection_gap":
        bucket.gap_status = (
            "complete"
            if bucket.received_sample_count >= bucket.expected_sample_count
            else "partial"
        )
    bucket.last_received_at = sample.received_at
    if bucket.finalized:
        await _invalidate_mutable_cost_selections(session, device, bucket.id)


async def ingest_stateless_sample(
    session: AsyncSession,
    device_id: str,
    payload: StatelessTelemetryRequest,
    *,
    now: datetime | None = None,
) -> StatelessIngestionResult:
    received_at = aware_utc(now or datetime.now(UTC))
    device = await session.scalar(
        select(Device).where(Device.id == device_id, Device.revoked_at.is_(None)).with_for_update()
    )
    if device is None:
        raise NotFound("active device does not exist")
    if payload.sensor_id != device.id:
        raise IntegrityConflict("telemetry sensor identity does not match the authenticated device")
    settings = await telemetry_settings_for_home(session, device.home_id)
    payload_hash = stateless_payload_hash(payload)
    prior_identity = await session.scalar(
        select(StatelessTelemetrySample).where(
            StatelessTelemetrySample.device_id == device.id,
            StatelessTelemetrySample.boot_id == payload.boot_id,
            StatelessTelemetrySample.sample_sequence == payload.sample_sequence,
        )
    )
    if prior_identity is not None:
        if prior_identity.payload_sha256 != payload_hash:
            raise IntegrityConflict("telemetry sample identity was accepted with different content")
        return StatelessIngestionResult(
            status="duplicate",
            received_at=aware_utc(prior_identity.received_at),
            timestamp_source="sensor" if prior_identity.sensor_time_trusted else "server",
            config_version=settings.config_version,
            telemetry_interval_seconds=cast(
                Literal[2, 5, 10, 15, 30, 60], settings.telemetry_interval_seconds
            ),
            advances_live_state=False,
        )

    sampled_at = aware_utc(payload.sampled_at) if payload.sampled_at is not None else None
    sensor_time_trusted = bool(
        payload.time_status == "trusted"
        and sampled_at is not None
        and abs((sampled_at - received_at).total_seconds()) <= MAX_SENSOR_TIME_SKEW_SECONDS
    )
    effective_at = sampled_at if sensor_time_trusted and sampled_at is not None else received_at
    state = await session.get(DeviceTelemetryState, device.id)
    previous_delivery = (
        await session.get(StatelessTelemetrySample, state.latest_sample_id) if state else None
    )
    out_of_order = bool(
        previous_delivery is not None
        and aware_utc(effective_at) < aware_utc(previous_delivery.effective_at)
    )
    previous_energy = await session.scalar(
        select(StatelessTelemetrySample)
        .where(
            StatelessTelemetrySample.device_id == device.id,
            StatelessTelemetrySample.pzem_status == "ok",
            StatelessTelemetrySample.pzem_energy_wh.is_not(None),
            StatelessTelemetrySample.effective_at < effective_at,
        )
        .order_by(
            StatelessTelemetrySample.effective_at.desc(),
            StatelessTelemetrySample.received_at.desc(),
        )
        .limit(1)
    )
    sample = StatelessTelemetrySample(
        device_id=device.id,
        boot_id=payload.boot_id,
        sample_sequence=payload.sample_sequence,
        telemetry_protocol=TELEMETRY_PROTOCOL_ID,
        sampled_at=sampled_at,
        received_at=received_at,
        effective_at=effective_at,
        sensor_time_trusted=sensor_time_trusted,
        uptime_ms=payload.uptime_ms,
        voltage_v=payload.voltage_v,
        current_a=payload.current_a,
        active_power_w=payload.active_power_w,
        frequency_hz=payload.frequency_hz,
        power_factor=payload.power_factor,
        pzem_energy_wh=payload.pzem_energy_wh,
        pzem_status=payload.pzem_status,
        firmware_version=payload.firmware_version,
        firmware_build_id=payload.firmware_build_id,
        time_status=payload.time_status,
        wifi_rssi=payload.wifi_rssi,
        payload_sha256=payload_hash,
    )
    session.add(sample)
    await session.flush()

    if out_of_order:
        energy_mwh, connection_gap = None, False
    else:
        energy_mwh, connection_gap = await _record_energy_evidence(
            session,
            device=device,
            sample=sample,
            previous_delivery=previous_delivery,
            previous_energy=previous_energy,
            telemetry_interval_seconds=settings.telemetry_interval_seconds,
        )
    await _upsert_history_bucket(
        session,
        device=device,
        sample=sample,
        settings=settings,
        energy_mwh=energy_mwh,
        connection_gap=connection_gap,
    )
    if state is None:
        state = DeviceTelemetryState(
            device_id=device.id,
            latest_sample_id=sample.id,
            latest_server_received_at=received_at,
            latest_sensor_sampled_at=sampled_at,
            sensor_time_trusted=sensor_time_trusted,
            timestamp_source="sensor" if sensor_time_trusted else "server",
            telemetry_protocol=TELEMETRY_PROTOCOL_ID,
            firmware_version=payload.firmware_version,
            firmware_build_id=payload.firmware_build_id,
        )
        session.add(state)
    elif not out_of_order:
        state.latest_sample_id = sample.id
        state.latest_server_received_at = received_at
        state.latest_sensor_sampled_at = sampled_at
        state.sensor_time_trusted = sensor_time_trusted
        state.timestamp_source = "sensor" if sensor_time_trusted else "server"
        state.telemetry_protocol = TELEMETRY_PROTOCOL_ID
        state.firmware_version = payload.firmware_version
        state.firmware_build_id = payload.firmware_build_id
        state.updated_at = received_at
    else:
        # Accept and bucket older evidence, but never regress current values or
        # firmware identity when trusted sensor timestamps arrive out of order.
        pass

    cutover = await session.scalar(
        select(TelemetryCutover.id).where(TelemetryCutover.device_id == device.id)
    )
    if cutover is None:
        session.add(
            TelemetryCutover(
                device_id=device.id,
                old_protocol=PROTOCOL_ID,
                new_protocol=TELEMETRY_PROTOCOL_ID,
                cutover_at=received_at,
                first_sample_id=sample.id,
                firmware_version=payload.firmware_version,
                firmware_build_id=payload.firmware_build_id,
            )
        )
    if not out_of_order:
        device.last_heartbeat_at = received_at
        device.firmware_version = payload.firmware_version
    await session.flush()
    return StatelessIngestionResult(
        status="accepted",
        received_at=received_at,
        timestamp_source="sensor" if sensor_time_trusted else "server",
        config_version=settings.config_version,
        telemetry_interval_seconds=cast(
            Literal[2, 5, 10, 15, 30, 60], settings.telemetry_interval_seconds
        ),
        advances_live_state=not out_of_order,
    )


async def apply_stateless_history_retention(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """Delete expired derived History buckets while preserving immutable raw samples/costs."""

    instant = aware_utc(now or datetime.now(UTC))
    settings_rows = (
        await session.scalars(
            select(HomeTelemetrySetting).where(HomeTelemetrySetting.retention_days.is_not(None))
        )
    ).all()
    deleted = 0
    for settings in settings_rows:
        assert settings.retention_days is not None
        cutoff = instant - timedelta(days=settings.retention_days)
        candidates = (
            await session.scalars(
                select(NormalizedInterval.id)
                .join(Device, Device.id == NormalizedInterval.device_id)
                .where(
                    Device.home_id == settings.home_id,
                    NormalizedInterval.source_kind == "stateless_v2",
                    NormalizedInterval.finalized.is_(True),
                    NormalizedInterval.end_utc < cutoff,
                    ~select(IntervalCostSelection.normalized_interval_id)
                    .where(IntervalCostSelection.normalized_interval_id == NormalizedInterval.id)
                    .exists(),
                )
            )
        ).all()
        if candidates:
            result = await session.execute(
                delete(NormalizedInterval).where(NormalizedInterval.id.in_(tuple(candidates)))
            )
            deleted += int(result.rowcount or 0)
    return deleted


async def finalize_stateless_history(session: AsyncSession, *, now: datetime | None = None) -> int:
    instant = aware_utc(now or datetime.now(UTC))
    rows = (
        await session.scalars(
            select(NormalizedInterval).where(
                NormalizedInterval.source_kind == "stateless_v2",
                NormalizedInterval.finalized.is_(False),
                NormalizedInterval.end_utc <= instant,
            )
        )
    ).all()
    for row in rows:
        row.finalized = True
        row.completeness = min(
            Decimal("1"),
            Decimal(row.received_sample_count) / Decimal(row.expected_sample_count),
        )
        if row.gap_status != "connection_gap":
            row.gap_status = (
                "complete" if row.received_sample_count >= row.expected_sample_count else "partial"
            )
    return len(rows)
