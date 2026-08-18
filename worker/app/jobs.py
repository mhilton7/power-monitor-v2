from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from backend.app.config import Settings, get_settings
from backend.app.models import (
    Alert,
    AlertConditionState,
    AlertEvent,
    AlertMaintenanceWindow,
    ApplicationLog,
    BillingEstimate,
    BillingEstimateSelection,
    CalculationRun,
    Circuit,
    CostRun,
    Device,
    DeviceEvent,
    DeviceHeartbeat,
    DeviceNonce,
    FirmwareDeployment,
    FirmwareRelease,
    Home,
    IntervalCost,
    IntervalCostSelection,
    NormalizedInterval,
    RateAssignment,
    RateCandidate,
    RateCandidateReview,
    RateHoliday,
    RatePeriod,
    RatePlanVersion,
    RateSourceRevision,
    RateSyncRun,
    RawReading,
    Rollup,
    UtilityAccount,
    aware_utc,
)
from backend.app.services.commands import create_command, expire_prepare_tokens
from backend.app.services.cost_engine import (
    CostContext,
    PricePeriod,
    RateVersion,
    fixed_charge_microdollars,
    price_sensor_interval,
)
from backend.app.services.rate_sync import sync_due_rate_sources
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

WORKER_LOCK_ID = 0x504D5632

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
    row = (
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
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    version, account, device = row
    periods = (
        await session.scalars(
            select(RatePeriod).where(RatePeriod.rate_plan_version_id == version.id)
        )
    ).all()
    holidays = frozenset(
        (
            await session.scalars(
                select(RateHoliday.local_date).where(RateHoliday.rate_plan_version_id == version.id)
            )
        ).all()
    )
    domain = RateVersion(
        id=version.id,
        timezone=version.timezone,
        effective_start=aware_utc(version.effective_start),
        effective_end=aware_utc(version.effective_end) if version.effective_end else None,
        periods=tuple(
            PricePeriod(
                season=period.season,  # type: ignore[arg-type]
                day_type=period.day_type,  # type: ignore[arg-type]
                name=period.period_name,
                start_minute=period.start_minute,
                end_minute=period.end_minute,
                price_per_kwh=period.price_per_kwh,
                tier_start_kwh=period.tier_start_kwh,
                tier_end_kwh=period.tier_end_kwh,
            )
            for period in periods
        ),
        baseline_credit_per_kwh=version.baseline_credit_per_kwh,
        tier_threshold_kwh_per_day=version.tier_threshold_kwh_per_day,
        tier_threshold_season=cast(
            Literal["summer", "winter"] | None, version.tier_threshold_season
        ),
        tier_threshold_source_kwh=version.tier_threshold_source_kwh,
        daily_fixed_charge=version.daily_fixed_charge,
        monthly_fixed_charge=version.monthly_fixed_charge,
        cca_adjustment_per_kwh=version.cca_adjustment_per_kwh,
        surcharge_percent=version.surcharge_percent,
        algorithm_version=version.algorithm_version,
        holidays=holidays,
    )
    effective_scope = "energy_only"
    member_ids: tuple[str, ...] = (device.id,)
    if account.cost_scope == "full_account":
        home_total = await session.scalar(
            select(Circuit).where(
                Circuit.home_id == account.home_id,
                Circuit.is_home_total.is_(True),
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


async def _cost_context(
    session: AsyncSession,
    interval: NormalizedInterval,
    account: UtilityAccount,
    rate: RateVersion,
    member_ids: tuple[str, ...],
    effective_scope: str,
) -> CostContext:
    cycle_start = _billing_cycle_start(interval.start_utc, account)
    prior_mwh = int(
        await session.scalar(
            select(func.sum(NormalizedInterval.energy_mwh))
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.device_id.in_(member_ids),
                RawReading.reset_generation == Device.reset_generation,
                NormalizedInterval.start_utc >= cycle_start,
                NormalizedInterval.end_utc <= interval.start_utc,
                NormalizedInterval.source_authenticated.is_(True),
            )
        )
        or 0
    )
    cumulative = Decimal(prior_mwh) / Decimal(1_000_000)
    allocation = account.baseline_allocation_kwh or Decimal("0")
    return CostContext(
        cumulative_cycle_kwh_before=cumulative,
        baseline_remaining_kwh=max(Decimal("0"), allocation - cumulative),
        billing_cycle_days=_billing_cycle_days(cycle_start, account),
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


async def calculate_pending_costs(session: AsyncSession, limit: int = 1000) -> int:
    intervals = (
        await session.scalars(
            select(NormalizedInterval)
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                RawReading.reset_generation == Device.reset_generation,
                ~select(IntervalCostSelection.normalized_interval_id)
                .where(IntervalCostSelection.normalized_interval_id == NormalizedInterval.id)
                .exists(),
            )
            .order_by(NormalizedInterval.start_utc)
            .limit(limit)
        )
    ).all()
    created = 0
    processed: set[str] = set()
    for interval in intervals:
        if interval.id in processed:
            continue
        resolved = await _rate_for_interval(session, interval)
        if resolved is None:
            continue
        rate, account, effective_scope, member_ids = resolved
        group = [interval]
        if len(member_ids) > 1:
            group = list(
                (
                    await session.scalars(
                        select(NormalizedInterval)
                        .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
                        .join(Device, Device.id == NormalizedInterval.device_id)
                        .where(
                            NormalizedInterval.device_id.in_(member_ids),
                            RawReading.reset_generation == Device.reset_generation,
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
        context = await _cost_context(session, interval, account, rate, member_ids, effective_scope)
        combined_energy = sum(item.energy_mwh for item in group)
        result = price_sensor_interval(
            start_utc=aware_utc(interval.start_utc),
            end_utc=aware_utc(interval.end_utc),
            energy_mwh=combined_energy,
            rate=rate,
            context=context,
        )
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
        weights = [item.energy_mwh for item in group]
        costs = _allocate_integer_total(result.energy_cost_microdollars, weights)
        credits = _allocate_integer_total(result.credit_microdollars, weights)
        period_name = ",".join(dict.fromkeys(item.period_name for item in result.slices))
        for index, member in enumerate(group):
            cost = IntervalCost(
                normalized_interval_id=member.id,
                cost_run_id=run.id,
                rate_plan_version_id=rate.id,
                energy_mwh=member.energy_mwh,
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
    return created


async def calculate_billing_estimates(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Select immutable cycle-to-date estimates from authenticated sensor costs."""

    instant = now or datetime.now(UTC)
    scope_end = instant.replace(second=0, microsecond=0)
    accounts = (await session.scalars(select(UtilityAccount))).all()
    created = 0
    for account in accounts:
        assignment = await session.scalar(
            select(RateAssignment)
            .join(
                RatePlanVersion,
                RatePlanVersion.id == RateAssignment.rate_plan_version_id,
            )
            .where(
                RateAssignment.utility_account_id == account.id,
                RateAssignment.effective_start <= instant,
                (RateAssignment.effective_end.is_(None) | (RateAssignment.effective_end > instant)),
                RatePlanVersion.state == "published",
                RatePlanVersion.effective_start <= instant,
                (
                    RatePlanVersion.effective_end.is_(None)
                    | (RatePlanVersion.effective_end > instant)
                ),
            )
            .order_by(RateAssignment.effective_start.desc())
            .limit(1)
        )
        if assignment is None:
            continue
        version = await session.get(RatePlanVersion, assignment.rate_plan_version_id)
        if version is None:
            continue
        periods = (
            await session.scalars(
                select(RatePeriod).where(RatePeriod.rate_plan_version_id == version.id)
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
        rate = RateVersion(
            id=version.id,
            timezone=version.timezone,
            effective_start=aware_utc(version.effective_start),
            effective_end=aware_utc(version.effective_end) if version.effective_end else None,
            periods=tuple(
                PricePeriod(
                    season=period.season,  # type: ignore[arg-type]
                    day_type=period.day_type,  # type: ignore[arg-type]
                    name=period.period_name,
                    start_minute=period.start_minute,
                    end_minute=period.end_minute,
                    price_per_kwh=period.price_per_kwh,
                    tier_start_kwh=period.tier_start_kwh,
                    tier_end_kwh=period.tier_end_kwh,
                )
                for period in periods
            ),
            baseline_credit_per_kwh=version.baseline_credit_per_kwh,
            tier_threshold_kwh_per_day=version.tier_threshold_kwh_per_day,
            tier_threshold_season=cast(
                Literal["summer", "winter"] | None, version.tier_threshold_season
            ),
            tier_threshold_source_kwh=version.tier_threshold_source_kwh,
            daily_fixed_charge=version.daily_fixed_charge,
            monthly_fixed_charge=version.monthly_fixed_charge,
            cca_adjustment_per_kwh=version.cca_adjustment_per_kwh,
            surcharge_percent=version.surcharge_percent,
            algorithm_version=version.algorithm_version,
            holidays=holidays,
        )
        cycle_start = _billing_cycle_start(instant, account)
        for scope_kind, scope_id, member_ids in await _billing_scope_groups(session, account):
            cost_rows = (
                await session.execute(
                    select(NormalizedInterval, IntervalCost)
                    .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
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
                        RawReading.reset_generation == Device.reset_generation,
                        NormalizedInterval.start_utc >= cycle_start,
                        NormalizedInterval.end_utc <= scope_end,
                    )
                    .order_by(NormalizedInterval.start_utc, NormalizedInterval.device_id)
                )
            ).all()
            energy_mwh = sum(interval.energy_mwh for interval, _cost in cost_rows)
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
            fixed = fixed_charge_microdollars(
                rate,
                local_start,
                local_end_exclusive,
                scope=scope_kind,
            )
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
    return created


async def update_rollups(session: AsyncSession) -> int:
    # Recomputable hourly rollups. Immutable raw/normalized evidence remains untouched.
    since = datetime.now(UTC) - timedelta(days=40)
    rows = (
        await session.scalars(
            select(NormalizedInterval)
            .join(RawReading, RawReading.id == NormalizedInterval.raw_reading_id)
            .join(Device, Device.id == NormalizedInterval.device_id)
            .where(
                NormalizedInterval.start_utc >= since,
                RawReading.reset_generation == Device.reset_generation,
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
        energy = sum(value.energy_mwh for value in values)
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
    deployment: FirmwareDeployment | None,
    recent_event_codes: set[str],
    now: datetime,
) -> dict[str, AlertObservation]:
    age_seconds = (
        (now - aware_utc(heartbeat.received_at)).total_seconds()
        if heartbeat is not None
        else (now - aware_utc(device.created_at)).total_seconds()
    )
    fresh = heartbeat is not None and age_seconds <= 30
    heartbeat_key = (
        heartbeat.id if heartbeat is not None else f"missing:{int(now.timestamp()) // 15}"
    )
    flags = {str(flag).strip().lower() for flag in (heartbeat.health_flags if heartbeat else [])}
    heartbeat_evidence: dict[str, Any] = {
        "heartbeat_id": heartbeat.id if heartbeat else None,
        "heartbeat_at": aware_utc(heartbeat.received_at).isoformat() if heartbeat else None,
        "heartbeat_age_seconds": round(age_seconds, 3),
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
    storage = heartbeat.storage_status if heartbeat is not None else None

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
            active=bool(fresh and heartbeat and heartbeat.backlog >= 10),
            evidence={**heartbeat_evidence, "backlog": heartbeat.backlog if heartbeat else None},
        ),
        "pzem_unavailable": fresh_observation(
            active=bool(fresh and heartbeat and heartbeat.pzem_status != "ok"),
            evidence={
                **heartbeat_evidence,
                "pzem_status": heartbeat.pzem_status if heartbeat else None,
            },
        ),
        "microsd_missing": fresh_observation(
            active=bool(fresh and storage == "missing"),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "microsd_read_only": fresh_observation(
            active=bool(fresh and storage == "read_only"),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "microsd_nearly_full": fresh_observation(
            active=bool(
                fresh
                and (storage == "full" or flags & {"microsd_nearly_full", "storage_nearly_full"})
            ),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "microsd_corrupt_segment": fresh_observation(
            active=bool(
                fresh
                and (
                    storage == "corrupt"
                    or flags & {"microsd_corrupt_segment", "storage_segment_corrupt"}
                    or "STORAGE_SEGMENT_CORRUPT" in recent_event_codes
                )
            ),
            evidence={**heartbeat_evidence, "storage_status": storage},
        ),
        "time_untrusted": fresh_observation(
            active=bool(fresh and heartbeat and heartbeat.time_status != "trusted"),
            evidence={
                **heartbeat_evidence,
                "time_status": heartbeat.time_status if heartbeat else None,
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
            device, heartbeat, deployment, recent_events, evaluated_at
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
        if heartbeat is not None and reading is not None:
            deployment.state = "succeeded"
            deployment.progress_percent = 100
            deployment.completed_at = datetime.now(UTC)
            deployment.evidence = {
                **deployment.evidence,
                "healthy_heartbeat_at": heartbeat.received_at.isoformat(),
                "post_boot_reading_id": reading,
            }
            completed += 1
    return completed


async def advance_staged_rollouts(session: AsyncSession) -> int:
    staged = (
        await session.scalars(
            select(FirmwareDeployment)
            .where(FirmwareDeployment.state == "staged")
            .order_by(FirmwareDeployment.created_at)
        )
    ).all()
    advanced = 0
    for deployment in staged:
        active = await session.scalar(
            select(FirmwareDeployment.id).where(
                FirmwareDeployment.firmware_release_id == deployment.firmware_release_id,
                FirmwareDeployment.state.in_(("queued", "downloading", "validating")),
            )
        )
        if active is not None:
            continue
        prior_failure = await session.scalar(
            select(FirmwareDeployment.id).where(
                FirmwareDeployment.firmware_release_id == deployment.firmware_release_id,
                FirmwareDeployment.state.in_(("failed", "rolled_back")),
            )
        )
        if prior_failure is not None:
            continue
        issued_by = deployment.evidence.get("issued_by_user_id")
        manifest = deployment.evidence.get("manifest")
        if not isinstance(issued_by, str) or not isinstance(manifest, dict):
            continue
        await create_command(
            session,
            device_id=deployment.device_id,
            command_type="ota_install",
            issued_by_user_id=issued_by,
            idempotency_key=f"ota:{deployment.id}",
            payload=manifest,
        )
        deployment.state = "queued"
        advanced += 1
        break
    return advanced


async def run_jobs(
    session: AsyncSession,
    *,
    backup_status_dir: Path = Path("/data/backup-status"),
    settings: Settings | None = None,
) -> dict[str, int]:
    if not await acquire_worker_lease(session):
        return {"lease_busy": 1}
    rate_sync = await sync_due_rate_sources(session, settings or get_settings())
    result = {
        "rate_sources_checked": rate_sync["checked"],
        "rate_sources_failed": rate_sync["failed"],
        "rate_sources_review_required": rate_sync["review_required"],
        "rate_sources_unchanged": rate_sync["unchanged"],
        "costs": await calculate_pending_costs(session),
        "billing_estimates": await calculate_billing_estimates(session),
        "rollups": await update_rollups(session),
        "alerts": await evaluate_sensor_alerts(session),
        "operational_alerts": await evaluate_operational_alerts(
            session, status_dir=backup_status_dir
        ),
        "firmware_completed": await evaluate_firmware_deployments(session),
        "staged_rollouts_advanced": await advance_staged_rollouts(session),
        "prepare_tokens_expired": await expire_prepare_tokens(session),
        "nonces_removed": await cleanup_nonces(session),
    }
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
