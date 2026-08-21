from __future__ import annotations

from calendar import monthrange
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
    season: str
    day_type: str
    name: str
    start_minute: int
    end_minute: int
    price_per_kwh: Decimal
    tier_start_kwh: Decimal = Decimal("0")
    tier_end_kwh: Decimal | None = None
    boundary_inclusive: bool = True
    threshold_basis: str | None = None


@dataclass(frozen=True)
class DatedPrice:
    start_utc: datetime
    end_utc: datetime
    name: str
    price_per_kwh: Decimal


@dataclass(frozen=True)
class SeasonDefinition:
    name: str
    start_month: int
    start_day: int
    end_month: int
    end_day: int


@dataclass(frozen=True)
class EventCalendar:
    local_dates: frozenset[date]
    coverage_start: date
    coverage_end: date


@dataclass(frozen=True)
class FixedCharge:
    kind: Literal[
        "daily_fixed_charge",
        "monthly_fixed_charge",
        "minimum_charge",
        "meter_charge",
        "other_fixed_charge",
    ]
    amount: Decimal
    applies: Literal[
        "per_account_per_day",
        "per_account_per_month",
        "per_account_per_cycle",
        "per_meter_per_day",
        "per_meter_per_month",
        "per_meter_per_cycle",
    ]


@dataclass(frozen=True)
class RateVersion:
    id: str
    timezone: str
    effective_start: datetime
    effective_end: datetime | None
    periods: tuple[PricePeriod, ...]
    rate_plan_id: str | None = None
    dated_prices: tuple[DatedPrice, ...] = ()
    season_definitions: tuple[SeasonDefinition, ...] = ()
    summer_months: tuple[int, ...] = (6, 7, 8, 9)
    holiday_treatment: str = "not_applicable"
    holiday_calendar: EventCalendar | None = None
    event_calendar: EventCalendar | None = None
    baseline_credit_per_kwh: Decimal = Decimal("0")
    tier_threshold_kwh_per_day: Decimal | None = None
    tier_threshold_season: str | None = None
    tier_threshold_source_kwh: Decimal | None = None
    tier1_boundary_inclusive: bool = True
    daily_fixed_charge: Decimal = Decimal("0")
    monthly_fixed_charge: Decimal = Decimal("0")
    minimum_charge: Decimal = Decimal("0")
    meter_charge: Decimal = Decimal("0")
    other_fixed_charge: Decimal = Decimal("0")
    fixed_charges: tuple[FixedCharge, ...] = ()
    cca_adjustment_per_kwh: Decimal = Decimal("0")
    surcharge_percent: Decimal = Decimal("0")
    algorithm_version: str = "cost-v1"
    holidays: frozenset[date] = frozenset()


@dataclass(frozen=True)
class CostContext:
    cumulative_cycle_kwh_before: Decimal = Decimal("0")
    baseline_remaining_kwh: Decimal = Decimal("0")
    billing_cycle_days: int | None = None
    tier_threshold_cycle_kwh: Decimal | None = None
    tier_threshold_kwh_per_day: Decimal | None = None
    tier_threshold_season: str | None = None
    tier1_boundary_inclusive: bool = True
    scope: Literal["energy_only", "allocated_account", "full_account"] = "energy_only"
    holidays: frozenset[date] = frozenset()
    holiday_calendar: EventCalendar | None = None
    event_calendar: EventCalendar | None = None


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


def season_definitions_from_storage(value: object) -> tuple[SeasonDefinition, ...]:
    if not isinstance(value, list):
        return ()
    definitions: list[SeasonDefinition] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("season_name"), str):
            raise ValueError("stored season definition is malformed")
        start_month = item.get("start_month")
        end_month = item.get("end_month")
        if not isinstance(start_month, int) or not isinstance(end_month, int):
            raise ValueError("stored season definition is malformed")
        start_day = item.get("start_day", 1)
        end_day = item.get("end_day", monthrange(2000, end_month)[1])
        if not isinstance(start_day, int) or not isinstance(end_day, int):
            raise ValueError("stored season definition is malformed")
        definitions.append(
            SeasonDefinition(
                name=item["season_name"],
                start_month=start_month,
                start_day=start_day,
                end_month=end_month,
                end_day=end_day,
            )
        )
    return tuple(definitions)


def fixed_charges_from_storage(value: object) -> tuple[FixedCharge, ...]:
    if not isinstance(value, list):
        return ()
    charges: list[FixedCharge] = []
    kinds = {
        "daily_fixed_charge",
        "monthly_fixed_charge",
        "minimum_charge",
        "meter_charge",
        "other_fixed_charge",
    }
    applies_values = {
        "per_account_per_day",
        "per_account_per_month",
        "per_account_per_cycle",
        "per_meter_per_day",
        "per_meter_per_month",
        "per_meter_per_cycle",
    }
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("stored fixed charge is malformed")
        kind = item.get("charge", item.get("kind"))
        applies = item.get("applies")
        legacy = (
            {
                "daily_fixed": ("daily_fixed_charge", "per_account_per_day"),
                "monthly_fixed": ("monthly_fixed_charge", "per_account_per_month"),
            }.get(kind)
            if isinstance(kind, str)
            else None
        )
        if legacy is not None and applies is None:
            kind, applies = legacy
        if not isinstance(kind, str) or kind not in kinds or applies not in applies_values:
            raise ValueError("stored fixed-charge applicability is unresolved")
        assert isinstance(applies, str)
        amount = Decimal(str(item.get("amount")))
        if not amount.is_finite() or amount < 0:
            raise ValueError("stored fixed charge is malformed")
        charges.append(FixedCharge(kind=kind, amount=amount, applies=applies))  # type: ignore[arg-type]
    return tuple(charges)


def event_calendar_from_evidence(value: object) -> EventCalendar | None:
    if not isinstance(value, list):
        return None
    matching = [
        item
        for item in value
        if isinstance(item, dict) and item.get("evidence_type") == "event_calendar"
    ]
    if not matching:
        return None
    if len(matching) != 1:
        raise ValueError("stored event calendar is ambiguous")
    item = matching[0]
    if (
        item.get("status") != "resolved"
        or not isinstance(item.get("local_dates"), list)
        or not isinstance(item.get("coverage_start"), str)
        or not isinstance(item.get("coverage_end"), str)
    ):
        raise ValueError("stored event calendar is unresolved")
    try:
        local_dates = frozenset(date.fromisoformat(str(raw)) for raw in item["local_dates"])
        coverage_start = date.fromisoformat(item["coverage_start"])
        coverage_end = date.fromisoformat(item["coverage_end"])
    except ValueError as exc:
        raise ValueError("stored event calendar is malformed") from exc
    if len(local_dates) != len(item["local_dates"]):
        raise ValueError("stored event calendar is malformed or duplicated")
    if coverage_end < coverage_start or any(
        local_date < coverage_start or local_date > coverage_end for local_date in local_dates
    ):
        raise ValueError("stored event calendar is malformed")
    return EventCalendar(
        local_dates=local_dates,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )


def holiday_calendar_from_evidence(value: object) -> EventCalendar | None:
    if not isinstance(value, list):
        return None
    matching = [
        item
        for item in value
        if isinstance(item, dict) and item.get("evidence_type") == "holiday_calendar"
    ]
    if not matching:
        return None
    if len(matching) != 1:
        raise ValueError("stored holiday calendar is ambiguous")
    item = matching[0]
    if (
        item.get("status") != "resolved"
        or not isinstance(item.get("holidays"), list)
        or not isinstance(item.get("coverage_start"), str)
        or not isinstance(item.get("coverage_end"), str)
    ):
        raise ValueError("stored holiday calendar is unresolved")
    try:
        local_dates = frozenset(
            date.fromisoformat(str(holiday["local_date"]))
            for holiday in item["holidays"]
            if isinstance(holiday, dict)
            and isinstance(holiday.get("name"), str)
            and bool(holiday["name"])
        )
        coverage_start = date.fromisoformat(item["coverage_start"])
        coverage_end = date.fromisoformat(item["coverage_end"])
    except (KeyError, ValueError) as exc:
        raise ValueError("stored holiday calendar is malformed") from exc
    if len(local_dates) != len(item["holidays"]):
        raise ValueError("stored holiday calendar is malformed or duplicated")
    if coverage_end < coverage_start or any(
        local_date < coverage_start or local_date > coverage_end for local_date in local_dates
    ):
        raise ValueError("stored holiday calendar is malformed")
    return EventCalendar(
        local_dates=local_dates,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
    )


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("authoritative timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _month_day_in_range(
    month: int,
    day: int,
    definition: SeasonDefinition,
) -> bool:
    value = month * 100 + day
    start = definition.start_month * 100 + definition.start_day
    end = definition.end_month * 100 + definition.end_day
    return start <= value <= end if start <= end else value >= start or value <= end


def season_for_local(rate: RateVersion, local: datetime) -> str:
    if not rate.season_definitions:
        return "summer" if local.month in rate.summer_months else "winter"
    matches = [
        definition.name
        for definition in rate.season_definitions
        if _month_day_in_range(local.month, local.day, definition)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"rate season resolved to {len(matches)} definitions at {local.date().isoformat()}"
        )
    return matches[0]


def season_from_storage(value: object, local: datetime) -> str:
    definitions = season_definitions_from_storage(value)
    if not definitions:
        return "summer" if local.month in (6, 7, 8, 9) else "winter"
    matches = [
        definition.name
        for definition in definitions
        if _month_day_in_range(local.month, local.day, definition)
    ]
    if len(matches) != 1:
        raise ValueError("stored season definitions do not resolve exactly once")
    return matches[0]


def _base_day_type(rate: RateVersion, local: datetime, context: CostContext) -> str:
    ordinary = "weekend" if local.weekday() >= 5 else "weekday"
    treatment = {
        "weekend_schedule": "same_as_weekend",
        "explicit_schedule": "explicit_holiday_schedule",
        "no_special_treatment": "no_special_treatment",
    }.get(rate.holiday_treatment, rate.holiday_treatment)
    day_sensitive = any(
        period.day_type in {"weekday", "weekend", "holiday"} for period in rate.periods
    )
    if treatment == "unresolved":
        if day_sensitive:
            raise ValueError("rate holiday treatment is unresolved")
        return ordinary
    requires_detection = (
        treatment
        in {
            "same_as_weekend",
            "same_as_weekday",
            "explicit_holiday_schedule",
        }
        and day_sensitive
    )
    if not requires_detection:
        if treatment in {
            "not_applicable",
            "no_special_treatment",
            "event_calendar_required",
        }:
            return ordinary
        if treatment not in {
            "same_as_weekend",
            "same_as_weekday",
            "explicit_holiday_schedule",
        }:
            raise ValueError("rate holiday treatment is unresolved")
        return ordinary
    calendar = context.holiday_calendar or rate.holiday_calendar
    if calendar is None:
        raise ValueError("rate holiday calendar is unresolved")
    local_date = local.date()
    if local_date < calendar.coverage_start or local_date > calendar.coverage_end:
        raise ValueError("rate holiday calendar does not cover the pricing instant")
    if local_date not in calendar.local_dates:
        return ordinary
    if treatment == "same_as_weekend":
        return "weekend"
    if treatment == "same_as_weekday":
        return "weekday"
    if treatment == "explicit_holiday_schedule":
        return "holiday"
    raise ValueError("rate holiday treatment is unresolved")


def _day_type_priority(
    rate: RateVersion,
    local: datetime,
    context: CostContext,
) -> tuple[str, ...]:
    base = _base_day_type(rate, local, context)
    event_aware = any(period.day_type in {"event_day", "non_event_day"} for period in rate.periods)
    if not event_aware:
        return (base, "all")
    calendar = context.event_calendar or rate.event_calendar
    if calendar is None:
        raise ValueError("rate event calendar is unresolved")
    local_date = local.date()
    if local_date < calendar.coverage_start or local_date > calendar.coverage_end:
        raise ValueError("rate event calendar does not cover the pricing instant")
    event_type = "event_day" if local_date in calendar.local_dates else "non_event_day"
    return (event_type, base, "all")


def _effective_tier_bounds(
    rate: RateVersion, period: PricePeriod, context: CostContext
) -> tuple[Decimal, Decimal | None]:
    if period.threshold_basis == "account_daily_baseline":
        if (
            context.tier_threshold_cycle_kwh is None
            or context.tier_threshold_season is None
            or period.season not in (context.tier_threshold_season, "all")
        ):
            raise ValueError("account tier-threshold evidence is unresolved")
        threshold = context.tier_threshold_cycle_kwh
        return (
            threshold if period.tier_start_kwh > 0 else Decimal("0"),
            threshold if period.tier_end_kwh is not None else None,
        )
    source = rate.tier_threshold_source_kwh
    if (
        rate.tier_threshold_kwh_per_day is None
        or rate.tier_threshold_season is None
        or source is None
        or context.billing_cycle_days is None
        or period.season not in (rate.tier_threshold_season, "all")
    ):
        return period.tier_start_kwh, period.tier_end_kwh
    threshold = rate.tier_threshold_kwh_per_day * context.billing_cycle_days
    start = threshold if period.tier_start_kwh == source else period.tier_start_kwh
    end = threshold if period.tier_end_kwh == source else period.tier_end_kwh
    return start, end


def _tier_boundary_is_inclusive(
    rate: RateVersion, period: PricePeriod, context: CostContext
) -> bool:
    account_boundary = (
        context.tier1_boundary_inclusive
        if period.threshold_basis == "account_daily_baseline"
        else True
    )
    return period.boundary_inclusive and rate.tier1_boundary_inclusive and account_boundary


def _period_for(
    rate: RateVersion,
    instant_utc: datetime,
    cumulative_kwh: Decimal,
    context: CostContext,
    *,
    incremental_energy: bool = False,
) -> PricePeriod:
    if rate.dated_prices:
        instant = _ensure_utc(instant_utc)
        matches = [
            item
            for item in rate.dated_prices
            if _ensure_utc(item.start_utc) <= instant < _ensure_utc(item.end_utc)
        ]
        if len(matches) != 1:
            raise ValueError(
                f"dated rate schedule resolved to {len(matches)} prices at {instant.isoformat()}"
            )
        selected = matches[0]
        return PricePeriod(
            season="all",
            day_type="all",
            name=selected.name,
            start_minute=0,
            end_minute=1440,
            price_per_kwh=selected.price_per_kwh,
        )
    local = instant_utc.astimezone(ZoneInfo(rate.timezone))
    minute = local.hour * 60 + local.minute
    season = season_for_local(rate, local)
    day_types = _day_type_priority(rate, local, context)
    candidates: list[tuple[PricePeriod, Decimal, Decimal | None]] = []
    for period in rate.periods:
        if not (
            period.season in (season, "all")
            and period.day_type in day_types
            and period.start_minute <= minute < period.end_minute
        ):
            continue
        tier_start, tier_end = _effective_tier_bounds(rate, period, context)
        upper_matches = tier_end is None or cumulative_kwh < tier_end
        if (
            tier_end is not None
            and cumulative_kwh == tier_end
            and _tier_boundary_is_inclusive(rate, period, context)
        ):
            upper_matches = True
        if cumulative_kwh >= tier_start and upper_matches:
            candidates.append((period, tier_start, tier_end))
    if candidates:
        specificity = max(
            (
                int(period.season == season),
                len(day_types) - day_types.index(period.day_type),
            )
            for period, _tier_start, _tier_end in candidates
        )
        candidates = [
            candidate
            for candidate in candidates
            for period in (candidate[0],)
            if (
                int(period.season == season),
                len(day_types) - day_types.index(period.day_type),
            )
            == specificity
        ]
    if len(candidates) == 2:
        lower, upper = sorted(candidates, key=lambda item: item[1])
        lower_period, _lower_start, lower_end = lower
        _upper_period, upper_start, _upper_end = upper
        if lower_end == cumulative_kwh == upper_start and _tier_boundary_is_inclusive(
            rate, lower_period, context
        ):
            candidates = [upper if incremental_energy else lower]
    if len(candidates) != 1:
        raise ValueError(
            f"rate schedule resolved to {len(candidates)} periods at {local.isoformat()}"
        )
    return candidates[0][0]


def resolve_price_period(
    rate: RateVersion,
    instant_utc: datetime,
    cumulative_kwh: Decimal,
    context: CostContext | None = None,
) -> PricePeriod:
    """Resolve one exact local schedule period or fail closed on ambiguity."""

    return _period_for(rate, _ensure_utc(instant_utc), cumulative_kwh, context or CostContext())


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


def _pricing_boundaries(rate: RateVersion, start: datetime, end: datetime) -> list[datetime]:
    # Immutable dated prices are UTC intervals; local minute/day/season
    # boundaries cannot affect them. Avoid artificial one-minute slices so the
    # sensor's indivisible integer mWh are allocated only at actual price changes.
    points = set() if rate.dated_prices else set(_minute_boundaries(start, end))
    for item in rate.dated_prices:
        item_start = _ensure_utc(item.start_utc)
        item_end = _ensure_utc(item.end_utc)
        if start < item_start < end:
            points.add(item_start)
        if start < item_end < end:
            points.add(item_end)
    return sorted(points)


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

    boundaries = [start, *_pricing_boundaries(rate, start, end), end]
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
                context,
                incremental_energy=remaining_mwh > 0,
            )
            segment_mwh = remaining_mwh
            _tier_start, tier_end = _effective_tier_bounds(rate, period, context)
            if tier_end is not None:
                tier_capacity = int((tier_end - cumulative) * MWH_PER_KWH)
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


def _canonical_fixed_charges(rate: RateVersion) -> tuple[FixedCharge, ...]:
    if rate.fixed_charges:
        return rate.fixed_charges
    if rate.minimum_charge or rate.meter_charge or rate.other_fixed_charge:
        raise ValueError("fixed-charge applicability is unresolved")
    charges: list[FixedCharge] = []
    if rate.daily_fixed_charge:
        charges.append(
            FixedCharge(
                "daily_fixed_charge",
                rate.daily_fixed_charge,
                "per_account_per_day",
            )
        )
    if rate.monthly_fixed_charge:
        charges.append(
            FixedCharge(
                "monthly_fixed_charge",
                rate.monthly_fixed_charge,
                "per_account_per_month",
            )
        )
    return tuple(charges)


def _touched_calendar_months(start: date, end_exclusive: date) -> int:
    """Count each calendar month containing at least one day in the charge range."""

    end_inclusive = end_exclusive - timedelta(days=1)
    return (end_inclusive.year - start.year) * 12 + end_inclusive.month - start.month + 1


def _fixed_charge_multiplier(
    charge: FixedCharge,
    *,
    days: int,
    months: int,
    meter_count: int | None,
    include_cycle_charge: bool,
) -> int:
    if charge.applies.startswith("per_meter_"):
        if meter_count is None or meter_count < 1:
            raise ValueError("per-meter fixed charge requires an exact utility meter count")
        entity_count = meter_count
    else:
        entity_count = 1
    if charge.applies.endswith("_per_day"):
        recurrence_count = days
    elif charge.applies.endswith("_per_month"):
        recurrence_count = months
    else:
        recurrence_count = int(include_cycle_charge)
    return entity_count * recurrence_count


def fixed_charge_microdollars(
    rate: RateVersion,
    start_local_date: date,
    end_local_date_exclusive: date,
    *,
    scope: str,
    meter_count: int | None = None,
    include_cycle_charge: bool = True,
    variable_charge_microdollars: int | None = None,
) -> int:
    if scope != "full_account":
        return 0
    if end_local_date_exclusive <= start_local_date:
        raise ValueError("fixed-charge range must be ordered")
    days = (end_local_date_exclusive - start_local_date).days
    months = _touched_calendar_months(start_local_date, end_local_date_exclusive)
    additive = Decimal("0")
    minimums: list[Decimal] = []
    for charge in _canonical_fixed_charges(rate):
        if not charge.amount.is_finite() or charge.amount < 0:
            raise ValueError("fixed charge must be a nonnegative exact decimal")
        if not charge.amount:
            continue
        multiplier = _fixed_charge_multiplier(
            charge,
            days=days,
            months=months,
            meter_count=meter_count,
            include_cycle_charge=include_cycle_charge,
        )
        total = charge.amount * multiplier
        if charge.kind == "minimum_charge":
            minimums.append(total)
        else:
            additive += total
    additive_microdollars = int(
        (additive * MICRODOLLARS_PER_DOLLAR).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    )
    if not minimums:
        return additive_microdollars
    if variable_charge_microdollars is None:
        raise ValueError("minimum charge requires the exact variable-charge subtotal")
    minimum_microdollars = max(
        int((minimum * MICRODOLLARS_PER_DOLLAR).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
        for minimum in minimums
    )
    subtotal = variable_charge_microdollars + additive_microdollars
    return additive_microdollars + max(0, minimum_microdollars - subtotal)


def current_cost_per_hour_microdollars(power_w: Decimal, price_per_kwh: Decimal) -> int:
    if power_w < 0 or price_per_kwh < 0:
        raise ValueError("power and price must be nonnegative")
    dollars = power_w / Decimal(1000) * price_per_kwh
    return int((dollars * MICRODOLLARS_PER_DOLLAR).quantize(Decimal("1")))
