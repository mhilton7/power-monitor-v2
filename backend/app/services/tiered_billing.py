from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ProjectionStatus = Literal["available", "insufficient_data"]
ProjectionConfidence = Literal["high", "moderate", "insufficient"]
TierState = Literal["tier_1", "tier_2", "estimated_tier_1", "estimated_tier_2", "not_confirmed"]
CalculationState = Literal["exact", "estimated", "partial", "unavailable"]


@dataclass(frozen=True)
class EnergyQuality:
    """Billing-only energy provenance; estimates are never History intervals."""

    measured_kwh: Decimal
    recovered_kwh: Decimal
    estimated_kwh: Decimal
    estimate_lower_kwh: Decimal
    estimate_upper_kwh: Decimal
    unknown_gap_count: int
    unknown_gap_seconds: int
    estimation_methods: tuple[str, ...] = ()

    @property
    def saved_usage_kwh(self) -> Decimal:
        return self.measured_kwh + self.recovered_kwh

    @property
    def current_usage_kwh(self) -> Decimal:
        return self.saved_usage_kwh + self.estimated_kwh

    @property
    def lower_usage_kwh(self) -> Decimal:
        return self.measured_kwh + self.recovered_kwh + self.estimate_lower_kwh

    @property
    def upper_usage_kwh(self) -> Decimal:
        return self.measured_kwh + self.recovered_kwh + self.estimate_upper_kwh

    @property
    def fully_resolved(self) -> bool:
        return self.unknown_gap_count == 0


def billing_calculation_state(
    *,
    has_blocking_reason: bool,
    has_estimated_energy: bool,
    tou_gap_unallocated: bool,
) -> CalculationState:
    if has_blocking_reason:
        return "unavailable"
    if tou_gap_unallocated:
        return "partial"
    if has_estimated_energy:
        return "estimated"
    return "exact"


def tier_state_for_quality(*, quality: EnergyQuality, threshold_kwh: Decimal) -> TierState:
    if threshold_kwh < 0:
        raise ValueError("tier threshold must be non-negative")
    if quality.unknown_gap_count:
        return "not_confirmed"
    lower_tier: Literal["tier_1", "tier_2"] = (
        "tier_1" if quality.lower_usage_kwh <= threshold_kwh else "tier_2"
    )
    upper_tier: Literal["tier_1", "tier_2"] = (
        "tier_1" if quality.upper_usage_kwh <= threshold_kwh else "tier_2"
    )
    if lower_tier != upper_tier:
        return "not_confirmed"
    if quality.estimated_kwh:
        return "estimated_tier_1" if lower_tier == "tier_1" else "estimated_tier_2"
    return lower_tier


def estimate_confidence(
    *,
    reading_coverage: Decimal,
    quality: EnergyQuality,
    high_coverage: Decimal = Decimal("0.99"),
    minimum_coverage: Decimal = Decimal("0.95"),
    unresolved_counter_resets: int = 0,
) -> tuple[ProjectionConfidence, list[str]]:
    if not (Decimal("0") <= minimum_coverage <= high_coverage <= Decimal("1")):
        raise ValueError("estimate coverage thresholds are invalid")
    reasons: list[str] = []
    if unresolved_counter_resets:
        return "insufficient", ["unresolved_counter_reset"]
    if quality.unknown_gap_count:
        return "insufficient", ["unknown_gap_energy"]
    if quality.recovered_kwh:
        reasons.append("cumulative_meter_energy_recovered")
    if quality.estimated_kwh:
        reasons.extend(f"estimated_by_{method}" for method in quality.estimation_methods)
    if reading_coverage >= high_coverage:
        reasons.append("reading_coverage_at_or_above_high_threshold")
        return "high", reasons
    if quality.recovered_kwh and not quality.estimated_kwh:
        # The precise power curve can have gaps while the cumulative PZEM total
        # still resolves cycle energy exactly. For a tiered plan that total is
        # sufficient even when chart coverage is below the estimate threshold.
        reasons.append("cycle_energy_total_fully_recovered")
        return "high", reasons
    if not quality.estimated_kwh and reading_coverage >= minimum_coverage:
        reasons.append("reading_coverage_at_or_above_minimum_threshold")
        return "moderate", reasons
    if reading_coverage >= minimum_coverage:
        reasons.append("reading_coverage_at_or_above_minimum_threshold")
        return "moderate", reasons
    return "insufficient", ["reading_coverage_below_minimum_threshold"]


@dataclass(frozen=True)
class TieredCost:
    total_usage_kwh: Decimal
    threshold_kwh: Decimal
    tier_1_usage_kwh: Decimal
    tier_2_usage_kwh: Decimal
    tier_1_cost: Decimal
    tier_2_cost: Decimal
    service_charge: Decimal
    total: Decimal


def tiered_cost(
    *,
    usage_kwh: Decimal,
    threshold_kwh: Decimal,
    tier_1_rate: Decimal,
    tier_2_rate: Decimal,
    service_days: Decimal,
    daily_service_charge: Decimal,
) -> TieredCost:
    """Calculate an exact tier split without binary floating-point arithmetic."""

    if (
        min(
            usage_kwh,
            threshold_kwh,
            tier_1_rate,
            tier_2_rate,
            service_days,
            daily_service_charge,
        )
        < 0
    ):
        raise ValueError("tiered billing inputs must be non-negative")
    tier_1_usage = min(usage_kwh, threshold_kwh)
    tier_2_usage = max(Decimal("0"), usage_kwh - threshold_kwh)
    tier_1_cost = tier_1_usage * tier_1_rate
    tier_2_cost = tier_2_usage * tier_2_rate
    service_charge = service_days * daily_service_charge
    return TieredCost(
        total_usage_kwh=usage_kwh,
        threshold_kwh=threshold_kwh,
        tier_1_usage_kwh=tier_1_usage,
        tier_2_usage_kwh=tier_2_usage,
        tier_1_cost=tier_1_cost,
        tier_2_cost=tier_2_cost,
        service_charge=service_charge,
        total=tier_1_cost + tier_2_cost + service_charge,
    )


def billing_projection(
    *,
    reliable_usage_kwh: Decimal,
    reliable_elapsed_hours: Decimal,
    total_cycle_days: int,
    threshold_kwh: Decimal,
    tier_1_rate: Decimal,
    tier_2_rate: Decimal,
    daily_service_charge: Decimal,
    reading_coverage: Decimal,
    unresolved_counter_resets: int,
    unresolved_connection_gaps: int,
    estimated_energy_kwh: Decimal = Decimal("0"),
    unknown_gap_count: int | None = None,
    minimum_reliable_hours: Decimal = Decimal("24"),
    high_coverage: Decimal = Decimal("0.99"),
    minimum_coverage: Decimal = Decimal("0.95"),
) -> dict[str, object]:
    reasons: list[str] = []
    if total_cycle_days <= 0:
        reasons.append("billing_cycle_schedule_invalid")
    if reliable_elapsed_hours < minimum_reliable_hours:
        reasons.append("fewer_than_minimum_reliable_hours")
    effective_unknown_count = (
        unresolved_connection_gaps if unknown_gap_count is None else unknown_gap_count
    )
    quality = EnergyQuality(
        measured_kwh=max(Decimal("0"), reliable_usage_kwh - estimated_energy_kwh),
        recovered_kwh=Decimal("0"),
        estimated_kwh=estimated_energy_kwh,
        estimate_lower_kwh=estimated_energy_kwh,
        estimate_upper_kwh=estimated_energy_kwh,
        unknown_gap_count=effective_unknown_count,
        unknown_gap_seconds=0,
        estimation_methods=("short_gap_neighbor_interpolation",) if estimated_energy_kwh else (),
    )
    confidence, confidence_reasons = estimate_confidence(
        reading_coverage=reading_coverage,
        quality=quality,
        high_coverage=high_coverage,
        minimum_coverage=minimum_coverage,
        unresolved_counter_resets=unresolved_counter_resets,
    )
    if confidence == "insufficient":
        reasons.extend(confidence_reasons)
    if reasons:
        return {
            "status": "insufficient_data",
            "confidence": None,
            "confidence_reasons": reasons,
            "projected_usage_kwh": None,
            "projected_tier_1_usage_kwh": None,
            "projected_tier_2_usage_kwh": None,
            "projected_tier_1_cost": None,
            "projected_tier_2_cost": None,
            "projected_service_charge": None,
            "projected_total": None,
        }

    reliable_days = reliable_elapsed_hours / Decimal("24")
    projected_usage = reliable_usage_kwh / reliable_days * Decimal(total_cycle_days)
    projected = tiered_cost(
        usage_kwh=projected_usage,
        threshold_kwh=threshold_kwh,
        tier_1_rate=tier_1_rate,
        tier_2_rate=tier_2_rate,
        service_days=Decimal(total_cycle_days),
        daily_service_charge=daily_service_charge,
    )
    if reliable_elapsed_hours < Decimal("72"):
        reasons.append("fewer_than_3_reliable_days")
    reasons.extend(reason for reason in confidence_reasons if reason not in reasons)
    if not reasons:
        reasons.append("reliable_whole_home_coverage")
    return {
        "status": "available",
        "confidence": confidence,
        "confidence_reasons": reasons,
        "projected_usage_kwh": projected.total_usage_kwh,
        "projected_tier_1_usage_kwh": projected.tier_1_usage_kwh,
        "projected_tier_2_usage_kwh": projected.tier_2_usage_kwh,
        "projected_tier_1_cost": projected.tier_1_cost,
        "projected_tier_2_cost": projected.tier_2_cost,
        "projected_service_charge": projected.service_charge,
        "projected_total": projected.total,
    }
