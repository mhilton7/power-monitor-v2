from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ProjectionStatus = Literal["available", "insufficient_data"]
ProjectionConfidence = Literal["high", "moderate", "limited"]


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
) -> dict[str, object]:
    reasons: list[str] = []
    if total_cycle_days <= 0:
        reasons.append("billing_cycle_schedule_invalid")
    if reliable_elapsed_hours < Decimal("24"):
        reasons.append("fewer_than_24_reliable_hours")
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
    if unresolved_counter_resets:
        reasons.append("unresolved_counter_reset")
    if unresolved_connection_gaps:
        reasons.append("unresolved_connection_gap")
    if reading_coverage < Decimal("0.90"):
        reasons.append("reading_coverage_below_90_percent")
    if reliable_elapsed_hours < Decimal("72"):
        reasons.append("fewer_than_3_reliable_days")
    if (
        unresolved_counter_resets
        or unresolved_connection_gaps
        or reading_coverage < Decimal("0.75")
    ):
        confidence: ProjectionConfidence = "limited"
    elif reliable_elapsed_hours >= Decimal("168") and reading_coverage >= Decimal("0.98"):
        confidence = "high"
    else:
        confidence = "moderate"
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
