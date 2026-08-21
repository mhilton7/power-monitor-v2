from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from backend.app.config import Settings, get_settings
from backend.app.errors import RateWorkflowConflict
from backend.app.models import (
    Alert,
    AlertConditionState,
    AlertEvent,
    AlertMaintenanceWindow,
    ApplicationLog,
    BillingCycleAdjustment,
    BillingEstimate,
    BillingEstimateSelection,
    CalculationRun,
    Circuit,
    CostRun,
    Device,
    DeviceEvent,
    DeviceHeartbeat,
    DeviceNonce,
    DeviceTelemetryState,
    FirmwareDeployment,
    FirmwareRelease,
    Home,
    IntervalCost,
    IntervalCostSelection,
    NormalizedInterval,
    RateAssignment,
    RateCandidate,
    RateCandidateReview,
    RateDatedPrice,
    RateHoliday,
    RatePeriod,
    RatePlanVersion,
    RateSourceRevision,
    RateSyncRun,
    RawReading,
    Rollup,
    StatelessTelemetrySample,
    TelemetryEnergyEvent,
    UtilityAccount,
    aware_utc,
)
from backend.app.services.commands import expire_prepare_tokens
from backend.app.services.cost_engine import (
    CostContext,
    DatedPrice,
    PricePeriod,
    RateEvaluationError,
    RateVersion,
    event_calendar_from_evidence,
    fixed_charge_microdollars,
    fixed_charges_from_storage,
    holiday_calendar_from_evidence,
    price_sensor_interval,
    season_definitions_from_storage,
    season_for_local,
    validate_rate_evidence,
)
from backend.app.services.firmware_deployments import (
    advance_next_staged_firmware_deployment as advance_next_staged_firmware_deployment,
)
from backend.app.services.firmware_deployments import (
    apply_firmware_deployment_retention,
    reconcile_firmware_artifact_quarantines,
    reconcile_firmware_version_heartbeat,
)
from backend.app.services.rate_sync import sync_due_rate_sources
from backend.app.services.rate_workflow import (
    resolve_assigned_utility_account_cycle_tier_threshold,
)
from backend.app.services.stateless_telemetry import (
    apply_stateless_history_retention,
    finalize_stateless_history,
)
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

WORKER_LOCK_ID = 0x504D5632
PENDING_COST_CURSOR_FILENAME = "worker-cost-scan-cursor.json"
PENDING_COST_CURSOR_SCHEMA = "pm-worker-cost-scan-cursor/1.0.0"
PENDING_COST_CURSOR_MAX_BYTES = 1024


def _load_pending_cost_scan_cursor(directory: Path) -> tuple[datetime, str] | None:
    path = directory / PENDING_COST_CURSOR_FILENAME
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_size < 2 or metadata.st_size > PENDING_COST_CURSOR_MAX_BYTES:
                return None
            raw = handle.read(PENDING_COST_CURSOR_MAX_BYTES + 1)
            if handle.read(1) or len(raw.encode("utf-8")) > PENDING_COST_CURSOR_MAX_BYTES:
                return None
        value = json.loads(raw)
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"schema", "start_utc", "interval_id"}:
        return None
    if value["schema"] != PENDING_COST_CURSOR_SCHEMA:
        return None
    start_text = value["start_utc"]
    interval_id = value["interval_id"]
    if (
        not isinstance(start_text, str)
        or len(start_text) > 40
        or not isinstance(interval_id, str)
        or len(interval_id) != 36
    ):
        return None
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        parsed_id = UUID(interval_id)
    except ValueError:
        return None
    if start.tzinfo is None or start.utcoffset() != timedelta(0) or str(parsed_id) != interval_id:
        return None
    return start.astimezone(UTC), interval_id


def _write_pending_cost_scan_cursor(
    directory: Path,
    cursor: tuple[datetime, str],
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    resolved_directory = directory.resolve(strict=True)
    destination = resolved_directory / PENDING_COST_CURSOR_FILENAME
    start, interval_id = cursor
    payload = (
        json.dumps(
            {
                "schema": PENDING_COST_CURSOR_SCHEMA,
                "start_utc": aware_utc(start).isoformat().replace("+00:00", "Z"),
                "interval_id": interval_id,
            },
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    if len(payload.encode("utf-8")) > PENDING_COST_CURSOR_MAX_BYTES:
        raise RuntimeError("pending-cost scan cursor exceeded its fixed size limit")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=resolved_directory,
        prefix=f".{PENDING_COST_CURSOR_FILENAME}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        if os.name == "posix":
            directory_descriptor = os.open(
                resolved_directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


DEVICE_ALERT_TYPES = frozenset(
    {
        "sensor_offline",
        "heartbeat_delayed",
        "reading_backlog",
        "pzem_unavailable",
        "microsd_missing",
        "microsd_read_only",
        "microsd_nearly_full",
        "microsd_corrupt_segment",
        "time_untrusted",
        "tls_validation_failure",
        "wifi_repeated_failure",
        "ota_failed_or_rolled_back",
    }
)
OPERATIONAL_ALERT_TYPES = frozenset(
    {"rate_source_changed", "rate_sync_failed", "backup_failed", "restore_test_failed"}
)
REQUIRED_ALERT_TYPES = DEVICE_ALERT_TYPES | OPERATIONAL_ALERT_TYPES


@dataclass(frozen=True)
class AlertObservation:
    active: bool
    severity: str
    evidence: dict[str, Any]
    debounce_seconds: int
    minimum_observations: int = 1
    observation_key: str | None = None


async def acquire_worker_lease(session: AsyncSession) -> bool:
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        # A transaction-scoped lease is released by PostgreSQL on both commit and
        # rollback.  A session-scoped advisory lock needs an explicit unlock,
        # which cannot run while the transaction is aborted and can therefore
        # mask the job's original database exception.
        return bool(await session.scalar(select(func.pg_try_advisory_xact_lock(WORKER_LOCK_ID))))
    return True


async def cleanup_nonces(session: AsyncSession) -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    result = await session.execute(delete(DeviceNonce).where(DeviceNonce.seen_at < cutoff))
    return int(result.rowcount or 0)


async def _rate_for_interval(
    session: AsyncSession, interval: NormalizedInterval
) -> tuple[RateVersion, UtilityAccount, str, tuple[str, ...]] | None:
    rows = (
        await session.execute(
            select(RatePlanVersion, UtilityAccount, Device)
            .join(RateAssignment, RateAssignment.rate_plan_version_id == RatePlanVersion.id)
            .join(UtilityAccount, UtilityAccount.id == RateAssignment.utility_account_id)
            .join(Device, Device.home_id == UtilityAccount.home_id)
            .where(
                Device.id == interval.device_id,
                RatePlanVersion.state == "published",
                RateAssignment.effective_start <= interval.start_utc,
                (
                    RateAssignment.effective_end.is_(None)
                    | (RateAssignment.effective_end >= interval.end_utc)
                ),
                RatePlanVersion.effective_start <= interval.start_utc,
                (
                    RatePlanVersion.effective_end.is_(None)
                    | (RatePlanVersion.effective_end >= interval.end_utc)
                ),
            )
            .order_by(RateAssignment.effective_start.desc())
            .limit(2)
        )
    ).all()
    if len(rows) != 1:
        return None
    version, account, device = rows[0]
    periods = (
        await session.scalars(
            select(RatePeriod).where(RatePeriod.rate_plan_version_id == version.id)
        )
    ).all()
    dated_prices = (
        await session.scalars(
            select(RateDatedPrice)
            .where(RateDatedPrice.rate_plan_version_id == version.id)
            .order_by(RateDatedPrice.start_utc)
        )
    ).all()
    holidays = frozenset(
        (
            await session.scalars(
                select(RateHoliday.local_date).where(RateHoliday.rate_plan_version_id == version.id)
            )
        ).all()
    )
    holiday_calendar = holiday_calendar_from_evidence(version.eligibility_evidence)
    if holiday_calendar is not None and holiday_calendar.local_dates != holidays:
        raise RateEvaluationError(
            "stored holiday calendar does not match its persisted holiday rows"
        )
    domain = RateVersion(
        id=version.id,
        rate_plan_id=version.rate_plan_id,
        timezone=version.timezone,
        effective_start=aware_utc(version.effective_start),
        effective_end=aware_utc(version.effective_end) if version.effective_end else None,
        periods=tuple(
            PricePeriod(
                season=period.season,
                day_type=period.day_type,
                name=period.period_name,
                start_minute=period.start_minute,
                end_minute=period.end_minute,
                price_per_kwh=period.price_per_kwh,
                tier_start_kwh=period.tier_start_kwh,
                tier_end_kwh=period.tier_end_kwh,
                boundary_inclusive=period.boundary_inclusive,
                threshold_basis=period.threshold_basis,
            )
            for period in periods
        ),
        dated_prices=tuple(
            DatedPrice(
                start_utc=aware_utc(item.start_utc),
                end_utc=aware_utc(item.end_utc),
                name=item.source_label,
                price_per_kwh=item.price_per_kwh,
            )
            for item in dated_prices
        ),
        season_definitions=season_definitions_from_storage(version.season_definitions),
        holiday_treatment=version.holiday_treatment,
        holiday_calendar=holiday_calendar,
        event_calendar=event_calendar_from_evidence(version.eligibility_evidence),
        baseline_credit_per_kwh=version.baseline_credit_per_kwh,
        tier_threshold_kwh_per_day=version.tier_threshold_kwh_per_day,
        tier_threshold_season=version.tier_threshold_season,
        tier_threshold_source_kwh=version.tier_threshold_source_kwh,
        tier1_boundary_inclusive=version.tier1_boundary_inclusive,
        daily_fixed_charge=version.daily_fixed_charge,
        monthly_fixed_charge=version.monthly_fixed_charge,
        minimum_charge=version.minimum_charge,
        meter_charge=version.meter_charge,
        other_fixed_charge=version.other_fixed_charge,
        fixed_charges=fixed_charges_from_storage(version.fixed_charges),
        cca_adjustment_per_kwh=version.cca_adjustment_per_kwh,
        surcharge_percent=version.surcharge_percent,
        algorithm_version=version.algorithm_version,
        holidays=holidays,
    )
    validate_rate_evidence(domain)
    effective_scope = "energy_only"
    member_ids: tuple[str, ...] = (device.id,)
    if account.cost_scope == "full_account":
        home_total = await session.scalar(
            select(Circuit).where(
                Circuit.home_id == account.home_id,
                Circuit.is_home_total.is_(True),
                Circuit.is_billing_source.is_(True),
                Circuit.aggregate_mode == "verified_sum",
                Circuit.non_overlapping_confirmed.is_(True),
            )
        )
        if home_total is not None:
            designated_members = tuple(
                sorted(
                    (
                        await session.scalars(
                            select(Device.id).where(
                                Device.circuit_id == home_total.id,
                                Device.include_in_aggregate.is_(True),
                            )
                        )
                    ).all()
                )
            )
            if device.id in designated_members:
                effective_scope = "full_account"
                member_ids = designated_members
        elif device.measurement_scope == account.cost_scope:
            # Compatibility for an unambiguous legacy verified aggregate while
            # a populated installation is still crossing revision 0016.
            scoped_devices = (
                await session.scalars(
                    select(Device).where(
                        Device.home_id == account.home_id,
                        Device.revoked_at.is_(None),
                        Device.measurement_scope == account.cost_scope,
                    )
                )
            ).all()
            if len(scoped_devices) == 1:
                effective_scope = account.cost_scope
            else:
                circuit_ids = {item.circuit_id for item in scoped_devices}
                circuit_id = next(iter(circuit_ids)) if len(circuit_ids) == 1 else None
                circuit = await session.get(Circuit, circuit_id) if circuit_id else None
                if circuit is not None and circuit.aggregate_mode == "verified_sum":
                    effective_scope = account.cost_scope
                    member_ids = tuple(sorted(item.id for item in scoped_devices))
    elif account.cost_scope == "allocated_account" and (
        device.measurement_scope == account.cost_scope
    ):
        scoped_devices = (
            await session.scalars(
                select(Device).where(
                    Device.home_id == account.home_id,
                    Device.revoked_at.is_(None),
                    Device.measurement_scope == account.cost_scope,
                )
            )
        ).all()
        if len(scoped_devices) == 1:
            effective_scope = account.cost_scope
        else:
            circuit_ids = {item.circuit_id for item in scoped_devices}
            circuit_id = next(iter(circuit_ids)) if len(circuit_ids) == 1 else None
            circuit = await session.get(Circuit, circuit_id) if circuit_id else None
            if circuit is None or circuit.aggregate_mode != "verified_sum":
                # Multiple account-scoped meters may only be combined after an
                # operator has verified a non-overlapping aggregate.  Pricing
                # stops rather than silently applying account credits per CT.
                return None
            effective_scope = account.cost_scope
            member_ids = tuple(sorted(item.id for item in scoped_devices))
    return domain, account, effective_scope, member_ids


async def _billing_scope_groups(
    session: AsyncSession, account: UtilityAccount
) -> list[tuple[str, str, tuple[str, ...]]]:
    devices = (
        await session.scalars(
            select(Device).where(
                Device.home_id == account.home_id,
                Device.revoked_at.is_(None),
            )
        )
    ).all()
    if account.cost_scope == "energy_only":
        return [("energy_only", device.id, (device.id,)) for device in devices]

    if account.cost_scope == "full_account":
        home_total = await session.scalar(
            select(Circuit).where(
                Circuit.home_id == account.home_id,
                Circuit.is_home_total.is_(True),
                Circuit.is_billing_source.is_(True),
                Circuit.aggregate_mode == "verified_sum",
                Circuit.non_overlapping_confirmed.is_(True),
            )
        )
        member_ids = (
            tuple(
                sorted(
                    (
                        await session.scalars(
                            select(Device.id).where(
                                Device.circuit_id == home_total.id,
                                Device.include_in_aggregate.is_(True),
                            )
                        )
                    ).all()
                )
            )
            if home_total is not None
            else ()
        )
        if home_total is not None and member_ids:
            groups: list[tuple[str, str, tuple[str, ...]]] = [
                ("energy_only", device.id, (device.id,))
                for device in devices
                if device.id not in member_ids
            ]
            groups.append(("full_account", home_total.id, member_ids))
            return groups

    matching = [device for device in devices if device.measurement_scope == account.cost_scope]
    allocated_groups: list[tuple[str, str, tuple[str, ...]]] = [
        ("energy_only", device.id, (device.id,))
        for device in devices
        if device.measurement_scope != account.cost_scope
    ]
    if len(matching) == 1:
        allocated_groups.append((account.cost_scope, matching[0].id, (matching[0].id,)))
    elif len(matching) > 1:
        circuit_ids = {device.circuit_id for device in matching}
        circuit_id = next(iter(circuit_ids)) if len(circuit_ids) == 1 else None
        circuit = await session.get(Circuit, circuit_id) if circuit_id else None
        if circuit is not None and circuit.aggregate_mode == "verified_sum":
            allocated_groups.append(
                (
                    account.cost_scope,
                    circuit.id,
                    tuple(sorted(device.id for device in matching)),
                )
            )
    return allocated_groups


def _billing_cycle_start(interval_start: datetime, account: UtilityAccount) -> datetime:
    zone = ZoneInfo(account.timezone)
    local = aware_utc(interval_start).astimezone(zone)
    year = local.year
    month = local.month
    if local.day < account.billing_day:
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return datetime(year, month, account.billing_day, tzinfo=zone).astimezone(UTC)


def _billing_cycle_days(cycle_start: datetime, account: UtilityAccount) -> int:
    zone = ZoneInfo(account.timezone)
    local_start = aware_utc(cycle_start).astimezone(zone)
    year = local_start.year + (1 if local_start.month == 12 else 0)
    month = 1 if local_start.month == 12 else local_start.month + 1
    local_end = datetime(year, month, account.billing_day, tzinfo=zone)
    return (local_end.date() - local_start.date()).days


def _billing_cycle_end(cycle_start: datetime, account: UtilityAccount) -> datetime:
    zone = ZoneInfo(account.timezone)
    local_start = aware_utc(cycle_start).astimezone(zone)
    year = local_start.year + (1 if local_start.month == 12 else 0)
    month = 1 if local_start.month == 12 else local_start.month + 1
    return datetime(year, month, account.billing_day, tzinfo=zone).astimezone(UTC)


async def _cost_context(
    session: AsyncSession,
    interval: NormalizedInterval,
    account: UtilityAccount,
    rate: RateVersion,
    member_ids: tuple[str, ...],
    effective_scope: str,
) -> CostContext | None:
    cycle_start = _billing_cycle_start(interval.start_utc, account)
    cycle_end = _billing_cycle_end(cycle_start, account)
    interval_start = aware_utc(interval.start_utc)
    automatic_start = cycle_start
    seed_mwh = 0
    seed = await session.scalar(
        select(BillingCycleAdjustment).where(
            BillingCycleAdjustment.utility_account_id == account.id,
            BillingCycleAdjustment.cycle_start_utc == cycle_start,
            BillingCycleAdjustment.reason == "verified_cycle_to_date_seed",
        )
    )
    if seed is not None:
        through_value = seed.evidence.get("through_utc")
        if not isinstance(through_value, str):
            return None
        try:
            through = datetime.fromisoformat(through_value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if through.utcoffset() is None:
            return None
        automatic_start = through.astimezone(UTC)
        if not cycle_start <= automatic_start <= interval_start:
            return None
        seed_mwh = seed.energy_mwh
        crossing_seed = await session.scalar(
            select(func.count(NormalizedInterval.id)).where(
                NormalizedInterval.device_id.in_(member_ids),
                NormalizedInterval.start_utc < automatic_start,
                NormalizedInterval.end_utc > automatic_start,
                NormalizedInterval.source_authenticated.is_(True),
            )
        )
        if int(crossing_seed or 0):
            return None
    prior_mwh = int(
        await session.scalar(
            select(func.sum(NormalizedInterval.energy_mwh))
            .outerjoin(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(member_ids),
                or_(
                    NormalizedInterval.source_kind == "stateless_v2",
                    RawReading.reset_generation == Device.reset_generation,
                ),
                NormalizedInterval.start_utc >= automatic_start,
                NormalizedInterval.end_utc <= interval_start,
                NormalizedInterval.source_authenticated.is_(True),
            )
        )
        or 0
    )
    recovered_mwh = int(
        await session.scalar(
            select(func.sum(TelemetryEnergyEvent.recovered_energy_mwh)).where(
                TelemetryEnergyEvent.device_id.in_(member_ids),
                TelemetryEnergyEvent.event_type == "connection_gap_recovered",
                TelemetryEnergyEvent.billing_status == "included",
                TelemetryEnergyEvent.gap_end_utc > automatic_start,
                TelemetryEnergyEvent.gap_end_utc <= interval_start,
            )
        )
        or 0
    )
    unresolved_events = await session.scalar(
        select(func.count(TelemetryEnergyEvent.id)).where(
            TelemetryEnergyEvent.device_id.in_(member_ids),
            TelemetryEnergyEvent.billing_status == "unresolved",
            or_(
                TelemetryEnergyEvent.gap_end_utc.is_(None),
                TelemetryEnergyEvent.gap_end_utc > automatic_start,
            ),
            or_(
                TelemetryEnergyEvent.gap_start_utc.is_(None),
                TelemetryEnergyEvent.gap_start_utc < interval_start,
            ),
        )
    )
    if int(unresolved_events or 0):
        return None
    cumulative = Decimal(seed_mwh + prior_mwh + recovered_mwh) / Decimal(1_000_000)
    allocation = account.baseline_allocation_kwh or Decimal("0")
    try:
        threshold = await resolve_assigned_utility_account_cycle_tier_threshold(
            session,
            utility_account_id=account.id,
            timezone=account.timezone,
            cycle_start=cycle_start,
            cycle_end=cycle_end,
        )
    except (RateWorkflowConflict, ValueError):
        return None
    if threshold is None and any(
        period.threshold_basis == "account_daily_baseline" for period in rate.periods
    ):
        return None
    local = aware_utc(interval.start_utc).astimezone(ZoneInfo(rate.timezone))
    return CostContext(
        cumulative_cycle_kwh_before=cumulative,
        baseline_remaining_kwh=max(Decimal("0"), allocation - cumulative),
        billing_cycle_days=_billing_cycle_days(cycle_start, account),
        tier_threshold_cycle_kwh=threshold.total_kwh if threshold else None,
        tier_threshold_season=season_for_local(rate, local) if threshold else None,
        tier1_boundary_inclusive=(threshold.tier1_boundary_inclusive if threshold else True),
        scope=effective_scope,  # type: ignore[arg-type]
        holidays=rate.holidays,
    )


def _allocate_integer_total(total: int, weights: list[int]) -> list[int]:
    if total < 0 or any(weight < 0 for weight in weights):
        raise ValueError("cost allocation inputs must be nonnegative")
    weight_sum = sum(weights)
    if not weights:
        return []
    if weight_sum == 0:
        return [total, *(0 for _ in weights[1:])]
    bases = [total * weight // weight_sum for weight in weights]
    remainders = [total * weight % weight_sum for weight in weights]
    for index in sorted(range(len(weights)), key=lambda item: (-remainders[item], item))[
        : total - sum(bases)
    ]:
        bases[index] += 1
    return bases


async def calculate_pending_costs(
    session: AsyncSession,
    limit: int = 1000,
    *,
    metrics: dict[str, int] | None = None,
    cursor_directory: Path | None = None,
) -> int:
    if limit <= 0:
        if metrics is not None:
            metrics["unpriceable"] = 0
        return 0

    pending = (
        select(NormalizedInterval)
        .outerjoin(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
        .join(Device, Device.id == NormalizedInterval.device_id)
        .where(
            or_(
                NormalizedInterval.source_kind == "stateless_v2",
                RawReading.reset_generation == Device.reset_generation,
            ),
            NormalizedInterval.finalized.is_(True),
            NormalizedInterval.energy_mwh.is_not(None),
            ~select(IntervalCostSelection.normalized_interval_id)
            .where(IntervalCostSelection.normalized_interval_id == NormalizedInterval.id)
            .exists(),
        )
        .order_by(NormalizedInterval.start_utc, NormalizedInterval.id)
    )
    cursor = (
        _load_pending_cost_scan_cursor(cursor_directory) if cursor_directory is not None else None
    )
    if cursor is None:
        intervals = list((await session.scalars(pending.limit(limit))).all())
    else:
        after_cursor = or_(
            NormalizedInterval.start_utc > cursor[0],
            and_(
                NormalizedInterval.start_utc == cursor[0],
                NormalizedInterval.id > cursor[1],
            ),
        )
        intervals = list((await session.scalars(pending.where(after_cursor).limit(limit))).all())
        if len(intervals) < limit:
            through_cursor = or_(
                NormalizedInterval.start_utc < cursor[0],
                and_(
                    NormalizedInterval.start_utc == cursor[0],
                    NormalizedInterval.id <= cursor[1],
                ),
            )
            intervals.extend(
                (
                    await session.scalars(
                        pending.where(through_cursor).limit(limit - len(intervals))
                    )
                ).all()
            )
    if intervals:
        last_scanned = intervals[-1]
        if cursor_directory is not None:
            _write_pending_cost_scan_cursor(
                cursor_directory,
                (last_scanned.start_utc, last_scanned.id),
            )
    created = 0
    unpriceable = 0
    processed: set[str] = set()
    for interval in intervals:
        if interval.id in processed:
            continue
        try:
            resolved = await _rate_for_interval(session, interval)
        except RateEvaluationError:
            # Published evidence is immutable, so malformed or unresolved
            # schedule evidence cannot be repaired inside this worker cycle.
            # Fail closed for only this interval; one bad rate must not roll
            # back independent authenticated intervals for other accounts.
            unpriceable += 1
            continue
        if resolved is None:
            continue
        rate, account, effective_scope, member_ids = resolved
        group = [interval]
        if len(member_ids) > 1:
            group = list(
                (
                    await session.scalars(
                        select(NormalizedInterval)
                        .outerjoin(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
                        .join(Device, Device.id == NormalizedInterval.device_id)
                        .where(
                            NormalizedInterval.device_id.in_(member_ids),
                            or_(
                                NormalizedInterval.source_kind == "stateless_v2",
                                RawReading.reset_generation == Device.reset_generation,
                            ),
                            NormalizedInterval.finalized.is_(True),
                            NormalizedInterval.energy_mwh.is_not(None),
                            NormalizedInterval.start_utc == interval.start_utc,
                            NormalizedInterval.end_utc == interval.end_utc,
                            NormalizedInterval.source_authenticated.is_(True),
                        )
                        .order_by(NormalizedInterval.device_id)
                    )
                ).all()
            )
            if {item.device_id for item in group} != set(member_ids):
                continue
            # A prior worker cycle may have selected one member but crashed
            # before the transaction committed all members.  Never mix runs.
            already_selected = int(
                await session.scalar(
                    select(func.count(IntervalCostSelection.normalized_interval_id)).where(
                        IntervalCostSelection.normalized_interval_id.in_(
                            tuple(item.id for item in group)
                        )
                    )
                )
                or 0
            )
            if already_selected:
                continue
        try:
            context = await _cost_context(
                session,
                interval,
                account,
                rate,
                member_ids,
                effective_scope,
            )
            if context is None:
                continue
            combined_energy = sum(int(item.energy_mwh or 0) for item in group)
            result = price_sensor_interval(
                start_utc=aware_utc(interval.start_utc),
                end_utc=aware_utc(interval.end_utc),
                energy_mwh=combined_energy,
                rate=rate,
                context=context,
            )
        except RateEvaluationError:
            # Cost-engine validation is deliberately fail closed. Keep the
            # interval unpriced while allowing unrelated work to commit.
            unpriceable += len(group)
            processed.update(item.id for item in group)
            continue
        run = CostRun(
            rate_plan_version_id=rate.id,
            algorithm_version=rate.algorithm_version,
            interval_start_utc=interval.start_utc,
            interval_end_utc=interval.end_utc,
            cost_scope=effective_scope,
            state="succeeded",
        )
        session.add(run)
        await session.flush()
        weights = [int(item.energy_mwh or 0) for item in group]
        costs = _allocate_integer_total(result.energy_cost_microdollars, weights)
        credits = _allocate_integer_total(result.credit_microdollars, weights)
        period_name = ",".join(dict.fromkeys(item.period_name for item in result.slices))
        for index, member in enumerate(group):
            cost = IntervalCost(
                normalized_interval_id=member.id,
                cost_run_id=run.id,
                rate_plan_version_id=rate.id,
                energy_mwh=int(member.energy_mwh or 0),
                energy_cost_microdollars=costs[index],
                credit_microdollars=credits[index],
                period_name=period_name,
            )
            session.add(cost)
            await session.flush()
            session.add(
                IntervalCostSelection(
                    normalized_interval_id=member.id,
                    interval_cost_id=cost.id,
                    selection_reason="effective_rate_assignment",
                )
            )
            processed.add(member.id)
            created += 1
    await session.flush()
    if metrics is not None:
        metrics["unpriceable"] = unpriceable
    return created


async def calculate_billing_estimates(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    metrics: dict[str, int] | None = None,
) -> int:
    """Select immutable cycle-to-date estimates from authenticated sensor costs."""

    instant = now or datetime.now(UTC)
    scope_end = instant.replace(second=0, microsecond=0)
    accounts = (await session.scalars(select(UtilityAccount))).all()
    created = 0
    unpriceable = 0
    for account in accounts:
        assignments = (
            await session.scalars(
                select(RateAssignment)
                .join(
                    RatePlanVersion,
                    RatePlanVersion.id == RateAssignment.rate_plan_version_id,
                )
                .where(
                    RateAssignment.utility_account_id == account.id,
                    RateAssignment.effective_start <= instant,
                    (
                        RateAssignment.effective_end.is_(None)
                        | (RateAssignment.effective_end > instant)
                    ),
                    RatePlanVersion.state == "published",
                    RatePlanVersion.effective_start <= instant,
                    (
                        RatePlanVersion.effective_end.is_(None)
                        | (RatePlanVersion.effective_end > instant)
                    ),
                )
                .order_by(RateAssignment.effective_start.desc())
                .limit(2)
            )
        ).all()
        if len(assignments) != 1:
            await session.execute(
                delete(BillingEstimateSelection).where(
                    BillingEstimateSelection.utility_account_id == account.id,
                    BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                )
            )
            continue
        assignment = assignments[0]
        version = await session.get(RatePlanVersion, assignment.rate_plan_version_id)
        if version is None:
            continue
        periods = (
            await session.scalars(
                select(RatePeriod).where(RatePeriod.rate_plan_version_id == version.id)
            )
        ).all()
        dated_prices = (
            await session.scalars(
                select(RateDatedPrice)
                .where(RateDatedPrice.rate_plan_version_id == version.id)
                .order_by(RateDatedPrice.start_utc)
            )
        ).all()
        holidays = frozenset(
            (
                await session.scalars(
                    select(RateHoliday.local_date).where(
                        RateHoliday.rate_plan_version_id == version.id
                    )
                )
            ).all()
        )
        try:
            holiday_calendar = holiday_calendar_from_evidence(version.eligibility_evidence)
            if holiday_calendar is not None and holiday_calendar.local_dates != holidays:
                raise RateEvaluationError(
                    "stored holiday calendar does not match its persisted holiday rows"
                )
            season_definitions = season_definitions_from_storage(version.season_definitions)
            event_calendar = event_calendar_from_evidence(version.eligibility_evidence)
            fixed_charges = fixed_charges_from_storage(version.fixed_charges)
        except RateEvaluationError:
            await session.execute(
                delete(BillingEstimateSelection).where(
                    BillingEstimateSelection.utility_account_id == account.id,
                    BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                )
            )
            unpriceable += 1
            continue
        rate = RateVersion(
            id=version.id,
            rate_plan_id=version.rate_plan_id,
            timezone=version.timezone,
            effective_start=aware_utc(version.effective_start),
            effective_end=aware_utc(version.effective_end) if version.effective_end else None,
            periods=tuple(
                PricePeriod(
                    season=period.season,
                    day_type=period.day_type,
                    name=period.period_name,
                    start_minute=period.start_minute,
                    end_minute=period.end_minute,
                    price_per_kwh=period.price_per_kwh,
                    tier_start_kwh=period.tier_start_kwh,
                    tier_end_kwh=period.tier_end_kwh,
                    boundary_inclusive=period.boundary_inclusive,
                    threshold_basis=period.threshold_basis,
                )
                for period in periods
            ),
            dated_prices=tuple(
                DatedPrice(
                    start_utc=aware_utc(item.start_utc),
                    end_utc=aware_utc(item.end_utc),
                    name=item.source_label,
                    price_per_kwh=item.price_per_kwh,
                )
                for item in dated_prices
            ),
            season_definitions=season_definitions,
            holiday_treatment=version.holiday_treatment,
            holiday_calendar=holiday_calendar,
            event_calendar=event_calendar,
            baseline_credit_per_kwh=version.baseline_credit_per_kwh,
            tier_threshold_kwh_per_day=version.tier_threshold_kwh_per_day,
            tier_threshold_season=version.tier_threshold_season,
            tier_threshold_source_kwh=version.tier_threshold_source_kwh,
            tier1_boundary_inclusive=version.tier1_boundary_inclusive,
            daily_fixed_charge=version.daily_fixed_charge,
            monthly_fixed_charge=version.monthly_fixed_charge,
            minimum_charge=version.minimum_charge,
            meter_charge=version.meter_charge,
            other_fixed_charge=version.other_fixed_charge,
            fixed_charges=fixed_charges,
            cca_adjustment_per_kwh=version.cca_adjustment_per_kwh,
            surcharge_percent=version.surcharge_percent,
            algorithm_version=version.algorithm_version,
            holidays=holidays,
        )
        try:
            validate_rate_evidence(rate)
        except RateEvaluationError:
            await session.execute(
                delete(BillingEstimateSelection).where(
                    BillingEstimateSelection.utility_account_id == account.id,
                    BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                )
            )
            unpriceable += 1
            continue
        cycle_start = _billing_cycle_start(instant, account)
        if (
            aware_utc(assignment.effective_start) > cycle_start
            or aware_utc(version.effective_start) > cycle_start
        ):
            # A single-version estimate cannot retroactively apply current fixed
            # charges to the earlier segment of a split billing cycle. Clear a
            # formerly selected estimate so readers fail closed instead of
            # continuing to display stale pre-transition billing.
            await session.execute(
                delete(BillingEstimateSelection).where(
                    BillingEstimateSelection.utility_account_id == account.id,
                    BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                )
            )
            continue
        for scope_kind, scope_id, member_ids in await _billing_scope_groups(session, account):
            recovered_energy_mwh = int(
                await session.scalar(
                    select(func.sum(TelemetryEnergyEvent.recovered_energy_mwh)).where(
                        TelemetryEnergyEvent.device_id.in_(member_ids),
                        TelemetryEnergyEvent.billing_status == "included",
                        TelemetryEnergyEvent.gap_end_utc > cycle_start,
                        TelemetryEnergyEvent.gap_end_utc <= scope_end,
                    )
                )
                or 0
            )
            verified_seed_mwh = int(
                await session.scalar(
                    select(func.sum(BillingCycleAdjustment.energy_mwh)).where(
                        BillingCycleAdjustment.utility_account_id == account.id,
                        BillingCycleAdjustment.cycle_start_utc == cycle_start,
                        BillingCycleAdjustment.reason == "verified_cycle_to_date_seed",
                    )
                )
                or 0
            )
            if recovered_energy_mwh or verified_seed_mwh:
                # These authoritative totals participate in cumulative tier
                # usage, but have no immutable interval price selection. Do not
                # understate a money estimate by silently omitting them.
                await session.execute(
                    delete(BillingEstimateSelection).where(
                        BillingEstimateSelection.utility_account_id == account.id,
                        BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                        BillingEstimateSelection.scope_kind == scope_kind,
                        BillingEstimateSelection.scope_id == scope_id,
                    )
                )
                continue
            cost_rows = (
                await session.execute(
                    select(NormalizedInterval, IntervalCost)
                    .outerjoin(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
                    .join(Device, Device.id == NormalizedInterval.device_id)
                    .join(
                        IntervalCostSelection,
                        IntervalCostSelection.normalized_interval_id == NormalizedInterval.id,
                    )
                    .join(
                        IntervalCost,
                        IntervalCost.id == IntervalCostSelection.interval_cost_id,
                    )
                    .where(
                        NormalizedInterval.device_id.in_(member_ids),
                        or_(
                            NormalizedInterval.source_kind == "stateless_v2",
                            RawReading.reset_generation == Device.reset_generation,
                        ),
                        NormalizedInterval.finalized.is_(True),
                        NormalizedInterval.energy_mwh.is_not(None),
                        NormalizedInterval.start_utc >= cycle_start,
                        NormalizedInterval.end_utc <= scope_end,
                    )
                    .order_by(NormalizedInterval.start_utc, NormalizedInterval.device_id)
                )
            ).all()
            if any(cost.rate_plan_version_id != version.id for _interval, cost in cost_rows):
                # Preserve already-priced immutable interval costs, but do not
                # combine their variable totals with one version's fixed charges
                # or leave a formerly selected estimate visible.
                await session.execute(
                    delete(BillingEstimateSelection).where(
                        BillingEstimateSelection.utility_account_id == account.id,
                        BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                        BillingEstimateSelection.scope_kind == scope_kind,
                        BillingEstimateSelection.scope_id == scope_id,
                    )
                )
                continue
            energy_mwh = sum(int(interval.energy_mwh or 0) for interval, _cost in cost_rows)
            energy_cost = sum(cost.energy_cost_microdollars for _interval, cost in cost_rows)
            credits = sum(cost.credit_microdollars for _interval, cost in cost_rows)
            expected = max(
                1,
                int((scope_end - cycle_start).total_seconds() // 60) * len(member_ids),
            )
            completeness_sum = sum(
                (interval.completeness for interval, _cost in cost_rows), Decimal("0")
            )
            completeness = min(Decimal("1"), completeness_sum / Decimal(expected))
            missing = max(0, expected - len(cost_rows))
            zone = ZoneInfo(account.timezone)
            local_start = cycle_start.astimezone(zone).date()
            local_end_exclusive = scope_end.astimezone(zone).date() + timedelta(days=1)
            try:
                fixed = fixed_charge_microdollars(
                    rate,
                    local_start,
                    local_end_exclusive,
                    scope=scope_kind,
                    # One UtilityAccount is unique per home and its verified Main-service
                    # billing source represents one utility billing meter.  Sensor/CT
                    # membership is deliberately never used as the meter multiplier.
                    meter_count=1,
                    variable_charge_microdollars=max(0, energy_cost - credits),
                )
            except RateEvaluationError:
                await session.execute(
                    delete(BillingEstimateSelection).where(
                        BillingEstimateSelection.utility_account_id == account.id,
                        BillingEstimateSelection.estimate_kind == "billing_cycle_to_date",
                        BillingEstimateSelection.scope_kind == scope_kind,
                        BillingEstimateSelection.scope_id == scope_id,
                    )
                )
                unpriceable += 1
                continue
            input_document = {
                "schema": "pm-billing-estimate-input/1.0.0",
                "account_id": account.id,
                "rate_version_id": version.id,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "member_device_ids": list(member_ids),
                "scope_start_utc": cycle_start.isoformat(),
                "scope_end_utc": scope_end.isoformat(),
                "interval_cost_ids": [cost.id for _interval, cost in cost_rows],
                "fixed_charge_microdollars": fixed,
            }
            input_sha256 = hashlib.sha256(
                json.dumps(input_document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            estimate = await session.scalar(
                select(BillingEstimate).where(BillingEstimate.input_sha256 == input_sha256)
            )
            if estimate is None:
                run = CostRun(
                    rate_plan_version_id=version.id,
                    algorithm_version=version.algorithm_version,
                    interval_start_utc=cycle_start,
                    interval_end_utc=scope_end,
                    cost_scope=scope_kind,
                    state="succeeded",
                )
                session.add(run)
                await session.flush()
                estimate = BillingEstimate(
                    utility_account_id=account.id,
                    cost_run_id=run.id,
                    rate_plan_version_id=version.id,
                    estimate_kind="billing_cycle_to_date",
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    member_device_ids=list(member_ids),
                    scope_start_utc=cycle_start,
                    scope_end_utc=scope_end,
                    sensor_energy_mwh=energy_mwh,
                    energy_cost_microdollars=energy_cost,
                    fixed_charge_microdollars=fixed,
                    credit_microdollars=credits,
                    total_microdollars=energy_cost - credits + fixed,
                    completeness=completeness,
                    missing_intervals=missing,
                    input_sha256=input_sha256,
                )
                session.add(estimate)
                await session.flush()
                created += 1
            selection = await session.get(
                BillingEstimateSelection,
                (account.id, "billing_cycle_to_date", scope_kind, scope_id),
            )
            if selection is None:
                session.add(
                    BillingEstimateSelection(
                        utility_account_id=account.id,
                        estimate_kind="billing_cycle_to_date",
                        scope_kind=scope_kind,
                        scope_id=scope_id,
                        billing_estimate_id=estimate.id,
                    )
                )
            else:
                selection.billing_estimate_id = estimate.id
                selection.selected_at = scope_end
    if metrics is not None:
        metrics["unpriceable"] = unpriceable
    return created


async def update_rollups(session: AsyncSession) -> int:
    # Recomputable hourly rollups. Immutable raw/normalized evidence remains untouched.
    since = datetime.now(UTC) - timedelta(days=40)
    rows = (
        await session.scalars(
            select(NormalizedInterval)
            .outerjoin(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.start_utc >= since,
                or_(
                    NormalizedInterval.source_kind == "stateless_v2",
                    RawReading.reset_generation == Device.reset_generation,
                ),
                NormalizedInterval.finalized.is_(True),
                NormalizedInterval.energy_mwh.is_not(None),
            )
        )
    ).all()
    grouped: dict[tuple[str, datetime], list[NormalizedInterval]] = {}
    for row in rows:
        bucket = row.start_utc.replace(minute=0, second=0, microsecond=0)
        grouped.setdefault((row.device_id, bucket), []).append(row)
    updated = 0
    for (device_id, start), values in grouped.items():
        run = CalculationRun(
            algorithm_version="rollup-v1",
            input_first_sequence=None,
            input_last_sequence=None,
            state="succeeded",
            completed_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        existing = await session.scalar(
            select(Rollup).where(
                Rollup.device_id == device_id,
                Rollup.bucket == "hour",
                Rollup.start_utc == start,
            )
        )
        energy = sum(int(value.energy_mwh or 0) for value in values)
        completeness = sum((value.completeness for value in values), Decimal("0")) / Decimal(60)
        if existing is None:
            session.add(
                Rollup(
                    device_id=device_id,
                    bucket="hour",
                    start_utc=start,
                    end_utc=start + timedelta(hours=1),
                    energy_mwh=energy,
                    completeness=min(Decimal("1"), completeness),
                    interval_count=len(values),
                    calculation_run_id=run.id,
                )
            )
        else:
            existing.energy_mwh = energy
            existing.completeness = min(Decimal("1"), completeness)
            existing.interval_count = len(values)
            existing.calculation_run_id = run.id
        updated += 1
    return updated


async def _active_maintenance_window(
    session: AsyncSession,
    *,
    home_id: str,
    device_id: str | None,
    alert_type: str,
    now: datetime,
) -> AlertMaintenanceWindow | None:
    query = select(AlertMaintenanceWindow).where(
        AlertMaintenanceWindow.home_id == home_id,
        AlertMaintenanceWindow.cancelled_at.is_(None),
        AlertMaintenanceWindow.starts_at <= now,
        AlertMaintenanceWindow.ends_at > now,
        or_(
            AlertMaintenanceWindow.alert_type.is_(None),
            AlertMaintenanceWindow.alert_type == alert_type,
        ),
    )
    if device_id is not None:
        query = query.where(
            or_(
                AlertMaintenanceWindow.device_id.is_(None),
                AlertMaintenanceWindow.device_id == device_id,
            )
        )
    else:
        query = query.where(AlertMaintenanceWindow.device_id.is_(None))
    window: AlertMaintenanceWindow | None = await session.scalar(
        query.order_by(AlertMaintenanceWindow.ends_at.desc()).limit(1)
    )
    return window


async def _apply_alert_observation(
    session: AsyncSession,
    *,
    scope_key: str,
    home_id: str,
    device_id: str | None,
    alert_type: str,
    observation: AlertObservation,
    now: datetime,
) -> int:
    condition = await session.scalar(
        select(AlertConditionState)
        .where(AlertConditionState.scope_key == scope_key)
        .with_for_update()
    )
    existing = await session.scalar(
        select(Alert).where(
            Alert.home_id == home_id,
            Alert.device_id == device_id if device_id is not None else Alert.device_id.is_(None),
            Alert.alert_type == alert_type,
            Alert.state.in_(("open", "acknowledged")),
        )
    )

    if not observation.active:
        if condition is not None:
            condition.active = False
            condition.first_seen_at = None
            condition.last_seen_at = now
            condition.observation_count = 0
            condition.last_observation_key = None
            condition.evidence = observation.evidence
        if existing is None:
            return 0
        existing.state = "resolved"
        existing.resolved_at = now
        existing.evidence = {**existing.evidence, "resolution": observation.evidence}
        session.add(
            AlertEvent(
                alert_id=existing.id,
                event_code="RESOLVED",
                evidence=observation.evidence,
            )
        )
        return 1

    if condition is None:
        condition = AlertConditionState(
            scope_key=scope_key,
            home_id=home_id,
            device_id=device_id,
            alert_type=alert_type,
            active=True,
            first_seen_at=now,
            last_seen_at=now,
            observation_count=1,
            last_observation_key=observation.observation_key,
            evidence=observation.evidence,
        )
        session.add(condition)
    else:
        if not condition.active:
            condition.active = True
            condition.first_seen_at = now
            condition.observation_count = 0
            condition.last_observation_key = None
        if (
            observation.observation_key is None
            or observation.observation_key != condition.last_observation_key
        ):
            condition.observation_count += 1
            condition.last_observation_key = observation.observation_key
        condition.last_seen_at = now
        condition.evidence = observation.evidence

    first_seen = aware_utc(condition.first_seen_at or now)
    ready = (
        now - first_seen >= timedelta(seconds=observation.debounce_seconds)
        and condition.observation_count >= observation.minimum_observations
    )
    if not ready:
        return 0

    window = await _active_maintenance_window(
        session,
        home_id=home_id,
        device_id=device_id,
        alert_type=alert_type,
        now=now,
    )
    evidence = {
        **observation.evidence,
        "debounce": {
            "first_seen_at": first_seen.isoformat(),
            "observation_count": condition.observation_count,
            "minimum_seconds": observation.debounce_seconds,
        },
    }
    if window is not None:
        condition.evidence = {
            **evidence,
            "maintenance_window_id": window.id,
            "suppressed_until": aware_utc(window.ends_at).isoformat(),
        }
        return 0
    if existing is not None:
        existing.evidence = evidence
        return 0

    alert = Alert(
        home_id=home_id,
        device_id=device_id,
        alert_type=alert_type,
        severity=observation.severity,
        state="open",
        evidence=evidence,
    )
    session.add(alert)
    await session.flush()
    session.add(AlertEvent(alert_id=alert.id, event_code="OPENED", evidence=evidence))
    return 1


def _heartbeat_observations(
    device: Device,
    heartbeat: DeviceHeartbeat | None,
    telemetry_state: DeviceTelemetryState | None,
    telemetry_sample: StatelessTelemetrySample | None,
    deployment: FirmwareDeployment | None,
    recent_event_codes: set[str],
    now: datetime,
) -> dict[str, AlertObservation]:
    stateless = telemetry_state is not None and telemetry_sample is not None
    age_seconds = (
        (now - aware_utc(telemetry_state.latest_server_received_at)).total_seconds()
        if stateless and telemetry_state is not None
        else (now - aware_utc(heartbeat.received_at)).total_seconds()
        if heartbeat is not None
        else (now - aware_utc(device.created_at)).total_seconds()
    )
    fresh = (stateless or heartbeat is not None) and age_seconds <= 30
    heartbeat_key = (
        telemetry_sample.id
        if stateless and telemetry_sample is not None
        else heartbeat.id
        if heartbeat is not None
        else f"missing:{int(now.timestamp()) // 15}"
    )
    flags = {
        str(flag).strip().lower()
        for flag in (heartbeat.health_flags if heartbeat and not stateless else [])
    }
    heartbeat_evidence: dict[str, Any] = {
        "heartbeat_id": heartbeat.id if heartbeat and not stateless else None,
        "telemetry_sample_id": telemetry_sample.id if stateless and telemetry_sample else None,
        "heartbeat_at": (
            aware_utc(telemetry_state.latest_server_received_at).isoformat()
            if stateless and telemetry_state is not None
            else aware_utc(heartbeat.received_at).isoformat()
            if heartbeat
            else None
        ),
        "heartbeat_age_seconds": round(age_seconds, 3),
        "telemetry_protocol": "pm-telemetry/2.0.0" if stateless else "legacy",
    }

    tls_active = bool(
        fresh
        and (
            flags
            & {
                "tls_validation_failure",
                "tls_ca_failure",
                "tls_hostname_failure",
                "tls_certificate_failure",
            }
            or recent_event_codes
            & {"TLS_VALIDATION_FAILED", "TLS_CA_FAILED", "TLS_HOSTNAME_FAILED"}
        )
    )
    wifi_active = bool(
        fresh
        and (
            flags & {"wifi_repeated_failure", "wifi_association_repeated_failure"}
            or recent_event_codes & {"WIFI_REPEATED_FAILURE", "WIFI_ASSOCIATION_FAILED"}
        )
    )
    storage = heartbeat.storage_status if heartbeat is not None and not stateless else None
    pzem_status = (
        telemetry_sample.pzem_status
        if stateless and telemetry_sample is not None
        else heartbeat.pzem_status
        if heartbeat is not None
        else None
    )
    time_status = (
        telemetry_sample.time_status
        if stateless and telemetry_sample is not None
        else heartbeat.time_status
        if heartbeat is not None
        else None
    )

    def fresh_observation(*, active: bool, evidence: dict[str, Any]) -> AlertObservation:
        return AlertObservation(
            active=active,
            severity="warning",
            evidence=evidence,
            debounce_seconds=30,
            minimum_observations=2,
            observation_key=heartbeat_key,
        )

    observations = {
        "sensor_offline": AlertObservation(
            active=age_seconds > 120,
            severity="critical",
            evidence=heartbeat_evidence,
            debounce_seconds=15,
            observation_key=f"age:{int(now.timestamp()) // 15}",
        ),
        "heartbeat_delayed": AlertObservation(
            active=30 < age_seconds <= 120,
            severity="warning",
            evidence=heartbeat_evidence,
            debounce_seconds=15,
            observation_key=f"age:{int(now.timestamp()) // 15}",
        ),
        "reading_backlog": fresh_observation(
            active=bool(not stateless and fresh and heartbeat and heartbeat.backlog >= 10),
            evidence={
                **heartbeat_evidence,
                "backlog": heartbeat.backlog if heartbeat and not stateless else None,
                "legacy_only": True,
            },
        ),
        "pzem_unavailable": fresh_observation(
            active=bool(fresh and pzem_status != "ok"),
            evidence={
                **heartbeat_evidence,
                "pzem_status": pzem_status,
            },
        ),
        "microsd_missing": fresh_observation(
            active=bool(not stateless and fresh and storage == "missing"),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "microsd_read_only": fresh_observation(
            active=bool(not stateless and fresh and storage == "read_only"),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "microsd_nearly_full": fresh_observation(
            active=bool(
                not stateless
                and fresh
                and (storage == "full" or flags & {"microsd_nearly_full", "storage_nearly_full"})
            ),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "microsd_corrupt_segment": fresh_observation(
            active=bool(
                not stateless
                and fresh
                and (
                    storage == "corrupt"
                    or flags & {"microsd_corrupt_segment", "storage_segment_corrupt"}
                    or "STORAGE_SEGMENT_CORRUPT" in recent_event_codes
                )
            ),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "time_untrusted": fresh_observation(
            active=bool(fresh and time_status != "trusted"),
            evidence={
                **heartbeat_evidence,
                "time_status": time_status,
            },
        ),
        "tls_validation_failure": fresh_observation(
            active=tls_active,
            evidence={**heartbeat_evidence, "health_flags": sorted(flags)},
        ),
        "wifi_repeated_failure": fresh_observation(
            active=wifi_active,
            evidence={**heartbeat_evidence, "health_flags": sorted(flags)},
        ),
        "ota_failed_or_rolled_back": AlertObservation(
            active=bool(deployment and deployment.state in {"failed", "rolled_back"}),
            severity="critical" if deployment and deployment.state == "rolled_back" else "warning",
            evidence={
                "deployment_id": deployment.id if deployment else None,
                "state": deployment.state if deployment else None,
                "deployment_evidence": deployment.evidence if deployment else {},
            },
            debounce_seconds=0,
            observation_key=deployment.id if deployment else None,
        ),
    }
    if set(observations) != DEVICE_ALERT_TYPES:
        raise RuntimeError("device alert implementation does not cover the required type set")
    return observations


async def evaluate_sensor_alerts(session: AsyncSession, now: datetime | None = None) -> int:
    evaluated_at = now or datetime.now(UTC)
    devices = (await session.scalars(select(Device).where(Device.revoked_at.is_(None)))).all()
    changed = 0
    for device in devices:
        telemetry_state = await session.get(DeviceTelemetryState, device.id)
        telemetry_sample = (
            await session.get(StatelessTelemetrySample, telemetry_state.latest_sample_id)
            if telemetry_state is not None
            else None
        )
        heartbeat = await session.scalar(
            select(DeviceHeartbeat)
            .where(DeviceHeartbeat.device_id == device.id)
            .order_by(DeviceHeartbeat.received_at.desc())
            .limit(1)
        )
        deployment = await session.scalar(
            select(FirmwareDeployment)
            .where(FirmwareDeployment.device_id == device.id)
            .order_by(FirmwareDeployment.created_at.desc())
            .limit(1)
        )
        recent_events = set(
            (
                await session.scalars(
                    select(DeviceEvent.event_code).where(
                        DeviceEvent.device_id == device.id,
                        DeviceEvent.received_at >= evaluated_at - timedelta(minutes=10),
                    )
                )
            ).all()
        )
        observations = _heartbeat_observations(
            device,
            heartbeat,
            telemetry_state,
            telemetry_sample,
            deployment,
            recent_events,
            evaluated_at,
        )
        for alert_type, observation in observations.items():
            changed += await _apply_alert_observation(
                session,
                scope_key=f"device:{device.id}:{alert_type}",
                home_id=device.home_id,
                device_id=device.id,
                alert_type=alert_type,
                observation=observation,
                now=evaluated_at,
            )
    return changed


def _read_status_evidence(status_dir: Path, filename: str) -> dict[str, Any] | None:
    path = status_dir / filename
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"state": "invalid", "error_code": type(exc).__name__, "evidence_file": filename}
    if not isinstance(value, dict):
        return {"state": "invalid", "error_code": "NON_OBJECT", "evidence_file": filename}
    allowlist = {
        "format",
        "state",
        "run_id",
        "started_at",
        "completed_at",
        "error_code",
        "verification_checks",
    }
    return {**{key: value[key] for key in allowlist if key in value}, "evidence_file": filename}


async def evaluate_operational_alerts(
    session: AsyncSession,
    *,
    status_dir: Path,
    now: datetime | None = None,
) -> int:
    evaluated_at = now or datetime.now(UTC)
    homes = (await session.scalars(select(Home.id).order_by(Home.id))).all()

    backup = _read_status_evidence(status_dir, "last-backup-attempt.json")
    restore = _read_status_evidence(status_dir, "last-restore-test-attempt.json")
    changed = 0
    for home_id in homes:
        candidate_ids = (
            await session.scalars(
                select(RateCandidate.id)
                .join(
                    RateSourceRevision,
                    RateSourceRevision.id == RateCandidate.source_revision_id,
                )
                .where(
                    RateCandidate.state == "review_required",
                    select(RateSyncRun.id)
                    .where(
                        RateSyncRun.home_id == home_id,
                        RateSyncRun.revision_id == RateSourceRevision.id,
                    )
                    .exists(),
                    ~select(RateCandidateReview.id)
                    .where(
                        RateCandidateReview.home_id == home_id,
                        RateCandidateReview.candidate_id == RateCandidate.id,
                        RateCandidateReview.state.in_(("published", "activated", "rejected")),
                    )
                    .exists(),
                )
                .limit(20)
            )
        ).all()
        sync_runs = (
            await session.scalars(
                select(RateSyncRun)
                .where(RateSyncRun.home_id == home_id)
                .order_by(RateSyncRun.started_at.desc(), RateSyncRun.id.desc())
            )
        ).all()
        latest_by_source: dict[str, RateSyncRun] = {}
        for run in sync_runs:
            latest_by_source.setdefault(run.source_id, run)
        failed_syncs = [run for run in latest_by_source.values() if run.state == "failed"]
        observations = {
            "rate_source_changed": AlertObservation(
                active=bool(candidate_ids),
                severity="warning",
                evidence={"review_required_candidate_ids": list(candidate_ids)},
                debounce_seconds=0,
                observation_key=":".join(sorted(candidate_ids)) or None,
            ),
            "rate_sync_failed": AlertObservation(
                active=bool(failed_syncs),
                severity="warning",
                evidence={
                    "failed_runs": [
                        {
                            "run_id": run.id,
                            "source_id": run.source_id,
                            "event_code": run.event_code,
                        }
                        for run in failed_syncs
                    ]
                },
                debounce_seconds=0,
                observation_key=":".join(sorted(run.id for run in failed_syncs)) or None,
            ),
            "backup_failed": AlertObservation(
                active=backup is not None and backup.get("state") != "verified",
                severity="critical",
                evidence=backup or {"state": "not_yet_attempted"},
                debounce_seconds=0,
                observation_key=str(
                    (backup or {}).get("run_id") or (backup or {}).get("state") or "none"
                ),
            ),
            "restore_test_failed": AlertObservation(
                active=restore is not None and restore.get("state") != "verified",
                severity="critical",
                evidence=restore or {"state": "not_yet_attempted"},
                debounce_seconds=0,
                observation_key=str(
                    (restore or {}).get("run_id") or (restore or {}).get("state") or "none"
                ),
            ),
        }
        if set(observations) != OPERATIONAL_ALERT_TYPES:
            raise RuntimeError(
                "operational alert implementation does not cover the required type set"
            )
        for alert_type, observation in observations.items():
            changed += await _apply_alert_observation(
                session,
                scope_key=f"home:{home_id}:{alert_type}",
                home_id=home_id,
                device_id=None,
                alert_type=alert_type,
                observation=observation,
                now=evaluated_at,
            )
    return changed


async def evaluate_firmware_deployments(session: AsyncSession) -> int:
    deployments = (
        await session.scalars(
            select(FirmwareDeployment).where(FirmwareDeployment.state == "validating")
        )
    ).all()
    completed = 0
    for deployment in deployments:
        release = await session.get(FirmwareRelease, deployment.firmware_release_id)
        if release is None:
            continue
        heartbeat = await session.scalar(
            select(DeviceHeartbeat)
            .join(Device, Device.id == DeviceHeartbeat.device_id)
            .where(
                DeviceHeartbeat.device_id == deployment.device_id,
                DeviceHeartbeat.received_at > deployment.created_at,
                Device.firmware_version == release.semantic_version,
            )
            .order_by(DeviceHeartbeat.received_at.desc())
            .limit(1)
        )
        reading = (
            await session.scalar(
                select(RawReading.id)
                .where(
                    RawReading.device_id == deployment.device_id,
                    RawReading.received_at > heartbeat.received_at,
                    RawReading.pzem_status == "ok",
                )
                .limit(1)
            )
            if heartbeat is not None
            else None
        )
        telemetry_state = await session.scalar(
            select(DeviceTelemetryState).where(
                DeviceTelemetryState.device_id == deployment.device_id,
                DeviceTelemetryState.latest_server_received_at > deployment.created_at,
            )
        )
        identity_matches_version = (
            telemetry_state is not None
            and telemetry_state.firmware_version.removeprefix("v")
            == release.semantic_version.removeprefix("v")
        )
        if (
            heartbeat is not None
            and reading is not None
            and telemetry_state is not None
            and identity_matches_version
        ):
            # Confirmation and staged-rollout advancement share the service's
            # row-locking path. A concurrent cancel/failure can therefore make
            # this a no-op instead of being overwritten as succeeded here.
            completed += len(
                await reconcile_firmware_version_heartbeat(
                    session,
                    device_id=deployment.device_id,
                    firmware_version=telemetry_state.firmware_version,
                    firmware_build_id=telemetry_state.firmware_build_id,
                    now=datetime.now(UTC),
                )
            )
    return completed


async def advance_staged_rollouts(session: AsyncSession) -> int:
    release_ids = (
        await session.scalars(
            select(FirmwareDeployment.firmware_release_id)
            .where(FirmwareDeployment.state == "staged")
            .order_by(FirmwareDeployment.created_at, FirmwareDeployment.id)
        )
    ).all()
    for release_id in dict.fromkeys(release_ids):
        # The shared service locks the release, rechecks active deployments,
        # then locks the staged row. It cannot resurrect a concurrently
        # cancelled deployment from this stale worker scan.
        if await advance_next_staged_firmware_deployment(session, release_id) is not None:
            return 1
    return 0


async def run_jobs(
    session: AsyncSession,
    *,
    backup_status_dir: Path = Path("/data/backup-status"),
    settings: Settings | None = None,
) -> dict[str, int]:
    if not await acquire_worker_lease(session):
        return {"lease_busy": 1}
    effective_settings = settings or get_settings()
    if effective_settings.env == "production" and effective_settings.log_dir is None:
        raise RuntimeError("production worker requires PM_LOG_DIR for its durable cost cursor")
    artifact_reconciliation = await reconcile_firmware_artifact_quarantines(
        session,
        firmware_dir=effective_settings.firmware_dir,
        apply=True,
    )
    restored_artifacts = artifact_reconciliation["restored_release_ids"]
    purged_artifacts = artifact_reconciliation["purged_release_ids"]
    promoted_uploads = artifact_reconciliation["promoted_upload_release_ids"]
    rate_sync = await sync_due_rate_sources(session, effective_settings)
    stateless_finalized = await finalize_stateless_history(session)
    cost_metrics: dict[str, int] = {}
    costs = await calculate_pending_costs(
        session,
        metrics=cost_metrics,
        cursor_directory=effective_settings.log_dir,
    )
    billing_metrics: dict[str, int] = {}
    billing_estimates = await calculate_billing_estimates(session, metrics=billing_metrics)
    result = {
        "rate_sources_checked": rate_sync["checked"],
        "rate_sources_failed": rate_sync["failed"],
        "rate_sources_review_required": rate_sync["review_required"],
        "rate_sources_unchanged": rate_sync["unchanged"],
        "stateless_history_finalized": stateless_finalized,
        "costs": costs,
        "costs_unpriceable": cost_metrics.get("unpriceable", 0),
        "billing_estimates": billing_estimates,
        "billing_estimates_unpriceable": billing_metrics.get("unpriceable", 0),
        "rollups": await update_rollups(session),
        "alerts": await evaluate_sensor_alerts(session),
        "operational_alerts": await evaluate_operational_alerts(
            session, status_dir=backup_status_dir
        ),
        "firmware_completed": await evaluate_firmware_deployments(session),
        "firmware_deployment_history_retained_cleanup": len(
            await apply_firmware_deployment_retention(session)
        ),
        "firmware_artifact_quarantines_restored": len(restored_artifacts)
        if isinstance(restored_artifacts, list)
        else 0,
        "firmware_artifact_quarantines_purged": len(purged_artifacts)
        if isinstance(purged_artifacts, list)
        else 0,
        "firmware_artifact_uploads_promoted": len(promoted_uploads)
        if isinstance(promoted_uploads, list)
        else 0,
        "staged_rollouts_advanced": await advance_staged_rollouts(session),
        "prepare_tokens_expired": await expire_prepare_tokens(session),
        "nonces_removed": await cleanup_nonces(session),
    }
    result["stateless_history_retained_cleanup"] = await apply_stateless_history_retention(session)
    home_ids = (await session.scalars(select(Home.id))).all()
    for home_id in home_ids:
        session.add(
            ApplicationLog(
                event_code="WORKER_CYCLE_COMPLETED",
                level="info",
                home_id=home_id,
                details={
                    "operation": "worker_cycle",
                    "state": "succeeded",
                    "count": sum(result.values()),
                },
            )
        )
    await session.commit()
    return result
