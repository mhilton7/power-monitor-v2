from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from typing import Literal
from zoneinfo import ZoneInfo

MICRODOLLARS_PER_DOLLAR = Decimal(1_000_000)
MWH_PER_KWH = Decimal(1_000_000)


@dataclass(frozen=True)
class PricePeriod:
    season: Literal["summer", "winter", "all"]
    day_type: Literal["weekday", "weekend", "holiday", "all"]
    name: str
    start_minute: int
    end_minute: int
    price_per_kwh: Decimal
    tier_start_kwh: Decimal = Decimal("0")
    tier_end_kwh: Decimal | None = None


@dataclass(frozen=True)
class RateVersion:
    id: str
    timezone: str
    effective_start: datetime
    effective_end: datetime | None
    periods: tuple[PricePeriod, ...]
    summer_months: tuple[int, ...] = (6, 7, 8, 9)
    baseline_credit_per_kwh: Decimal = Decimal("0")
    daily_fixed_charge: Decimal = Decimal("0")
    monthly_fixed_charge: Decimal = Decimal("0")
    cca_adjustment_per_kwh: Decimal = Decimal("0")
    surcharge_percent: Decimal = Decimal("0")
    algorithm_version: str = "cost-v1"
    holidays: frozenset[date] = frozenset()


@dataclass(frozen=True)
class CostContext:
    cumulative_cycle_kwh_before: Decimal = Decimal("0")
    baseline_remaining_kwh: Decimal = Decimal("0")
    scope: Literal["energy_only", "allocated_account", "full_account"] = "energy_only"
    holidays: frozenset[date] = frozenset()


@dataclass(frozen=True)
class PricedSlice:
    start_utc: datetime
    end_utc: datetime
    energy_mwh: int
    period_name: str
    price_per_kwh: Decimal
    cost_microdollars: int
    credit_microdollars: int


@dataclass(frozen=True)
class CostResult:
    rate_version_id: str
    algorithm_version: str
    energy_mwh: int
    energy_cost_microdollars: int
    credit_microdollars: int
    total_microdollars: int
    slices: tuple[PricedSlice, ...]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("authoritative timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _season(rate: RateVersion, local: datetime) -> str:
    return "summer" if local.month in rate.summer_months else "winter"


def _day_type(local: datetime, holidays: frozenset[date]) -> str:
    if local.date() in holidays:
        return "holiday"
    return "weekend" if local.weekday() >= 5 else "weekday"


def _period_for(
    rate: RateVersion, instant_utc: datetime, cumulative_kwh: Decimal, holidays: frozenset[date]
) -> PricePeriod:
    local = instant_utc.astimezone(ZoneInfo(rate.timezone))
    minute = local.hour * 60 + local.minute
    season = _season(rate, local)
    day_type = _day_type(local, holidays)
    candidates = [
        period
        for period in rate.periods
        if period.season in (season, "all")
        and period.day_type in (day_type, "all")
        and period.start_minute <= minute < period.end_minute
        and cumulative_kwh >= period.tier_start_kwh
        and (period.tier_end_kwh is None or cumulative_kwh < period.tier_end_kwh)
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
    if len(candidates) != 1:
        raise ValueError(
            f"rate schedule resolved to {len(candidates)} periods at {local.isoformat()}"
        )
    return candidates[0]


def _minute_boundaries(start: datetime, end: datetime) -> list[datetime]:
    first = start.replace(second=0, microsecond=0)
    if first <= start:
        first += timedelta(minutes=1)
    points: list[datetime] = []
    cursor = first
    while cursor < end:
        points.append(cursor)
        cursor += timedelta(minutes=1)
    return points


def _timedelta_microseconds(value: timedelta) -> int:
    """Convert a duration with integer arithmetic; authoritative allocation never uses float."""
    return ((value.days * 86_400) + value.seconds) * 1_000_000 + value.microseconds


def _allocate_integer_energy(total_mwh: int, durations_us: list[int]) -> list[int]:
    duration_total = sum(durations_us)
    if duration_total <= 0:
        raise ValueError("interval duration must be positive")
    allocations: list[int] = []
    remainder = total_mwh
    for index, duration in enumerate(durations_us):
        if index == len(durations_us) - 1:
            value = remainder
        else:
            value = total_mwh * duration // duration_total
        allocations.append(value)
        remainder -= value
    return allocations


def _allocate_microdollars(values: list[Decimal]) -> list[int]:
    """Round once at the interval boundary, then apportion exact integer microdollars."""

    if any(value < 0 for value in values):
        raise ValueError("microdollar allocation values must be non-negative")
    bases = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in values]
    target = int(sum(values, Decimal("0")).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    remaining = target - sum(bases)
    order = sorted(
        range(len(values)),
        key=lambda index: (values[index] - Decimal(bases[index]), -index),
        reverse=True,
    )
    for index in order[:remaining]:
        bases[index] += 1
    return bases


def price_sensor_interval(
    *,
    start_utc: datetime,
    end_utc: datetime,
    energy_mwh: int,
    rate: RateVersion,
    context: CostContext | None = None,
) -> CostResult:
    """Price authenticated sensor energy. This API cannot accept bill extraction objects."""

    start = _ensure_utc(start_utc)
    end = _ensure_utc(end_utc)
    context = context or CostContext()
    if end <= start or energy_mwh < 0:
        raise ValueError("invalid sensor interval")
    effective_start = _ensure_utc(rate.effective_start)
    effective_end = _ensure_utc(rate.effective_end) if rate.effective_end else None
    if start < effective_start or (effective_end is not None and end > effective_end):
        raise ValueError("sensor interval is outside the immutable rate version")

    boundaries = [start, *_minute_boundaries(start, end), end]
    durations_us = [_timedelta_microseconds(right - left) for left, right in pairwise(boundaries)]
    energies = _allocate_integer_energy(energy_mwh, durations_us)
    cumulative = context.cumulative_cycle_kwh_before
    baseline_remaining = context.baseline_remaining_kwh
    slice_values: list[tuple[datetime, datetime, int, str, Decimal]] = []
    raw_costs: list[Decimal] = []
    raw_credits: list[Decimal] = []

    for left, right, bucket_mwh in zip(boundaries, boundaries[1:], energies, strict=False):
        remaining_mwh = bucket_mwh
        segment_start = left
        while remaining_mwh > 0 or (bucket_mwh == 0 and not slice_values):
            period = _period_for(
                rate,
                segment_start
                + timedelta(microseconds=_timedelta_microseconds(right - segment_start) // 2),
                cumulative,
                context.holidays,
            )
            segment_mwh = remaining_mwh
            if period.tier_end_kwh is not None:
                tier_capacity = int((period.tier_end_kwh - cumulative) * MWH_PER_KWH)
                if tier_capacity <= 0:
                    raise ValueError("tier schedule does not advance at its threshold")
                segment_mwh = min(segment_mwh, tier_capacity)
            if bucket_mwh and segment_mwh < remaining_mwh:
                consumed_after = bucket_mwh - remaining_mwh + segment_mwh
                duration_us = _timedelta_microseconds(right - left)
                offset_us = duration_us * consumed_after // bucket_mwh
                offset_us = min(duration_us - 1, max(1, offset_us))
                segment_end = left + timedelta(microseconds=offset_us)
            else:
                segment_end = right
            segment_kwh = Decimal(segment_mwh) / MWH_PER_KWH
            price = period.price_per_kwh + rate.cca_adjustment_per_kwh
            energy_cost = segment_kwh * price
            if rate.surcharge_percent:
                energy_cost += energy_cost * rate.surcharge_percent / Decimal(100)
            credited_kwh = Decimal("0")
            if context.scope == "full_account" and baseline_remaining > 0:
                credited_kwh = min(segment_kwh, baseline_remaining)
                baseline_remaining -= credited_kwh
            credit = credited_kwh * rate.baseline_credit_per_kwh
            raw_costs.append(energy_cost * MICRODOLLARS_PER_DOLLAR)
            raw_credits.append(credit * MICRODOLLARS_PER_DOLLAR)
            slice_values.append((segment_start, segment_end, segment_mwh, period.name, price))
            cumulative += segment_kwh
            remaining_mwh -= segment_mwh
            segment_start = segment_end
            if bucket_mwh == 0:
                break

    allocated_costs = _allocate_microdollars(raw_costs)
    allocated_credits = _allocate_microdollars(raw_credits)
    slices = tuple(
        PricedSlice(*values, allocated_costs[index], allocated_credits[index])
        for index, values in enumerate(slice_values)
    )
    energy_cost_total = sum(allocated_costs)
    credit_total = sum(allocated_credits)
    return CostResult(
        rate_version_id=rate.id,
        algorithm_version=rate.algorithm_version,
        energy_mwh=energy_mwh,
        energy_cost_microdollars=energy_cost_total,
        credit_microdollars=credit_total,
        total_microdollars=energy_cost_total - credit_total,
        slices=slices,
    )


def fixed_charge_microdollars(
    rate: RateVersion, start_local_date: date, end_local_date_exclusive: date, *, scope: str
) -> int:
    if scope != "full_account":
        return 0
    if end_local_date_exclusive <= start_local_date:
        raise ValueError("fixed-charge range must be ordered")
    days = (end_local_date_exclusive - start_local_date).days
    months = {(start_local_date + timedelta(days=offset)).replace(day=1) for offset in range(days)}
    dollars = rate.daily_fixed_charge * days + rate.monthly_fixed_charge * len(months)
    return int((dollars * MICRODOLLARS_PER_DOLLAR).quantize(Decimal("1")))


def current_cost_per_hour_microdollars(power_w: Decimal, price_per_kwh: Decimal) -> int:
    if power_w < 0 or price_per_kwh < 0:
        raise ValueError("power and price must be nonnegative")
    dollars = power_w / Decimal(1000) * price_per_kwh
    return int((dollars * MICRODOLLARS_PER_DOLLAR).quantize(Decimal("1")))
