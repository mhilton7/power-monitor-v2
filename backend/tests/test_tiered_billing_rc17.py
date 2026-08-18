from decimal import Decimal

from backend.app.services.tiered_billing import TieredCost, billing_projection, tiered_cost

TIER_1_RATE = Decimal("0.30863")
TIER_2_RATE = Decimal("0.40962")
DAILY_SERVICE = Decimal("0.76900")
DAILY_ALLOWANCE = Decimal("19.3")


def _cost(usage: str, days: int) -> TieredCost:
    return tiered_cost(
        usage_kwh=Decimal(usage),
        threshold_kwh=DAILY_ALLOWANCE * Decimal(days),
        tier_1_rate=TIER_1_RATE,
        tier_2_rate=TIER_2_RATE,
        service_days=Decimal(days),
        daily_service_charge=DAILY_SERVICE,
    )


def test_cycle_threshold_scales_by_exact_billing_days() -> None:
    assert _cost("0", 28).threshold_kwh == Decimal("540.4")
    assert _cost("0", 30).threshold_kwh == Decimal("579.0")
    assert _cost("0", 31).threshold_kwh == Decimal("598.3")


def test_tier_two_starts_only_above_the_cycle_threshold() -> None:
    threshold = _cost("579.0", 30)
    assert threshold.tier_1_usage_kwh == Decimal("579.0")
    assert threshold.tier_2_usage_kwh == Decimal("0")
    crossover = _cost("579.1", 30)
    assert crossover.tier_1_usage_kwh == Decimal("579.0")
    assert crossover.tier_2_usage_kwh == Decimal("0.1")


def test_canonical_sce_fixture_exact_tiers_costs_and_one_fixed_charge() -> None:
    result = _cost("951", 30)
    assert result.tier_1_usage_kwh == Decimal("579.0")
    assert result.tier_2_usage_kwh == Decimal("372.0")
    assert result.tier_1_cost == Decimal("178.696770")
    assert result.tier_2_cost == Decimal("152.378640")
    assert result.service_charge == Decimal("23.07000")
    assert result.total == Decimal("354.145410")
    assert result.total.quantize(Decimal("0.01")) == Decimal("354.15")


def test_projection_requires_24_reliable_hours_and_splits_projected_tiers() -> None:
    unavailable = billing_projection(
        reliable_usage_kwh=Decimal("20"),
        reliable_elapsed_hours=Decimal("23.99"),
        total_cycle_days=30,
        threshold_kwh=Decimal("579.0"),
        tier_1_rate=TIER_1_RATE,
        tier_2_rate=TIER_2_RATE,
        daily_service_charge=DAILY_SERVICE,
        reading_coverage=Decimal("1"),
        unresolved_counter_resets=0,
        unresolved_connection_gaps=0,
    )
    assert unavailable["status"] == "insufficient_data"
    assert unavailable["projected_total"] is None

    projected = billing_projection(
        reliable_usage_kwh=Decimal("40"),
        reliable_elapsed_hours=Decimal("48"),
        total_cycle_days=30,
        threshold_kwh=Decimal("579.0"),
        tier_1_rate=TIER_1_RATE,
        tier_2_rate=TIER_2_RATE,
        daily_service_charge=DAILY_SERVICE,
        reading_coverage=Decimal("0.95"),
        unresolved_counter_resets=0,
        unresolved_connection_gaps=0,
    )
    assert projected["status"] == "available"
    assert projected["confidence"] == "moderate"
    assert projected["projected_usage_kwh"] == Decimal("600")
    assert projected["projected_tier_1_usage_kwh"] == Decimal("579.0")
    assert projected["projected_tier_2_usage_kwh"] == Decimal("21.0")


def test_projection_confidence_discloses_unresolved_energy() -> None:
    projected = billing_projection(
        reliable_usage_kwh=Decimal("100"),
        reliable_elapsed_hours=Decimal("168"),
        total_cycle_days=30,
        threshold_kwh=Decimal("579.0"),
        tier_1_rate=TIER_1_RATE,
        tier_2_rate=TIER_2_RATE,
        daily_service_charge=DAILY_SERVICE,
        reading_coverage=Decimal("0.99"),
        unresolved_counter_resets=1,
        unresolved_connection_gaps=1,
    )
    assert projected["confidence"] == "limited"
    reasons = projected["confidence_reasons"]
    assert isinstance(reasons, list)
    assert "unresolved_counter_reset" in reasons
    assert "unresolved_connection_gap" in reasons
