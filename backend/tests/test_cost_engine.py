from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from backend.app.services.cost_engine import (
    CostContext,
    PricePeriod,
    RateVersion,
    fixed_charge_microdollars,
    price_sensor_interval,
)


def rate(periods: tuple[PricePeriod, ...], **kwargs) -> RateVersion:  # type: ignore[no-untyped-def]
    return RateVersion(
        id="rate-v1",
        timezone="America/Los_Angeles",
        effective_start=datetime(2026, 1, 1, tzinfo=UTC),
        effective_end=datetime(2027, 1, 1, tzinfo=UTC),
        periods=periods,
        **kwargs,
    )


def test_tou_boundary_splits_sensor_energy_exactly() -> None:
    periods = (
        PricePeriod("all", "all", "off", 0, 16 * 60, Decimal("0.25")),
        PricePeriod("all", "all", "on", 16 * 60, 21 * 60, Decimal("0.50")),
        PricePeriod("all", "all", "off", 21 * 60, 1440, Decimal("0.25")),
    )
    result = price_sensor_interval(
        start_utc=datetime(2026, 8, 13, 22, 59, 30, tzinfo=UTC),
        end_utc=datetime(2026, 8, 13, 23, 0, 30, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=rate(periods),
    )
    assert result.energy_mwh == 1_000_000
    assert result.total_microdollars == 375_000
    assert [item.period_name for item in result.slices] == ["off", "on"]


def test_fall_back_repeated_times_are_distinct_utc_energy() -> None:
    periods = (PricePeriod("all", "all", "flat", 0, 1440, Decimal("0.30")),)
    result = price_sensor_interval(
        start_utc=datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
        end_utc=datetime(2026, 11, 1, 10, 30, tzinfo=UTC),
        energy_mwh=2_000_000,
        rate=rate(periods),
    )
    assert result.energy_mwh == 2_000_000
    assert result.total_microdollars == 600_000


def test_baseline_credit_and_fixed_charge_only_for_full_account() -> None:
    periods = (PricePeriod("all", "all", "flat", 0, 1440, Decimal("0.40")),)
    version = rate(
        periods,
        baseline_credit_per_kwh=Decimal("0.10"),
        daily_fixed_charge=Decimal("0.79"),
    )
    energy_only = price_sensor_interval(
        start_utc=datetime(2026, 2, 1, tzinfo=UTC),
        end_utc=datetime(2026, 2, 1, 0, 1, tzinfo=UTC),
        energy_mwh=2_000_000,
        rate=version,
        context=CostContext(baseline_remaining_kwh=Decimal("5"), scope="energy_only"),
    )
    full = price_sensor_interval(
        start_utc=datetime(2026, 2, 1, tzinfo=UTC),
        end_utc=datetime(2026, 2, 1, 0, 1, tzinfo=UTC),
        energy_mwh=2_000_000,
        rate=version,
        context=CostContext(baseline_remaining_kwh=Decimal("1"), scope="full_account"),
    )
    assert energy_only.credit_microdollars == 0
    assert full.credit_microdollars == 100_000
    assert (
        fixed_charge_microdollars(version, date(2026, 2, 1), date(2026, 2, 2), scope="energy_only")
        == 0
    )
    assert (
        fixed_charge_microdollars(version, date(2026, 2, 1), date(2026, 2, 2), scope="full_account")
        == 790_000
    )


def test_energy_is_split_exactly_at_tier_threshold() -> None:
    periods = (
        PricePeriod(
            "all",
            "all",
            "tier-one",
            0,
            1440,
            Decimal("0.10"),
            tier_start_kwh=Decimal("0"),
            tier_end_kwh=Decimal("1"),
        ),
        PricePeriod(
            "all",
            "all",
            "tier-two",
            0,
            1440,
            Decimal("0.30"),
            tier_start_kwh=Decimal("1"),
        ),
    )
    result = price_sensor_interval(
        start_utc=datetime(2026, 3, 1, tzinfo=UTC),
        end_utc=datetime(2026, 3, 1, 0, 1, tzinfo=UTC),
        energy_mwh=500_000,
        rate=rate(periods),
        context=CostContext(cumulative_cycle_kwh_before=Decimal("0.8")),
    )
    assert result.total_microdollars == 110_000
    assert [item.energy_mwh for item in result.slices] == [200_000, 300_000]
    assert [item.period_name for item in result.slices] == ["tier-one", "tier-two"]


def _sce_domestic_rate() -> RateVersion:
    source_threshold = Decimal("579.0")
    return rate(
        (
            PricePeriod(
                "summer",
                "all",
                "tier-one",
                0,
                1440,
                Decimal("0.30863"),
                tier_end_kwh=source_threshold,
            ),
            PricePeriod(
                "summer",
                "all",
                "tier-two",
                0,
                1440,
                Decimal("0.40962"),
                tier_start_kwh=source_threshold,
            ),
        ),
        tier_threshold_kwh_per_day=Decimal("19.3"),
        tier_threshold_season="summer",
        tier_threshold_source_kwh=source_threshold,
        daily_fixed_charge=Decimal("0.769"),
    )


def test_sce_daily_allowance_prorates_and_keeps_the_boundary_in_tier_one() -> None:
    version = _sce_domestic_rate()
    for days, threshold in ((28, Decimal("540.4")), (30, Decimal("579.0")), (31, Decimal("598.3"))):
        exact = price_sensor_interval(
            start_utc=datetime(2026, 7, 1, tzinfo=UTC),
            end_utc=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
            energy_mwh=int(threshold * Decimal(1_000_000)),
            rate=version,
            context=CostContext(billing_cycle_days=days),
        )
        assert {item.period_name for item in exact.slices} == {"tier-one"}
        crossing = price_sensor_interval(
            start_utc=datetime(2026, 7, 1, tzinfo=UTC),
            end_utc=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
            energy_mwh=100_000,
            rate=version,
            context=CostContext(
                cumulative_cycle_kwh_before=threshold,
                billing_cycle_days=days,
            ),
        )
        assert {item.period_name for item in crossing.slices} == {"tier-two"}


def test_sce_source_bill_reconciles_through_the_existing_cost_engine() -> None:
    version = _sce_domestic_rate()
    result = price_sensor_interval(
        start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
        energy_mwh=951_000_000,
        rate=version,
        context=CostContext(billing_cycle_days=30),
    )
    fixed = fixed_charge_microdollars(
        version, date(2026, 6, 22), date(2026, 7, 22), scope="full_account"
    )
    assert [item.energy_mwh for item in result.slices] == [579_000_000, 372_000_000]
    assert result.energy_cost_microdollars == 331_075_410
    assert fixed == 23_070_000
    assert result.total_microdollars + fixed == 354_145_410
    rounded_dollars = (Decimal(result.total_microdollars + fixed) / Decimal(1_000_000)).quantize(
        Decimal("0.01")
    )
    assert rounded_dollars == Decimal("354.15")


def test_sce_579_point_1_kwh_places_only_point_1_in_tier_two() -> None:
    result = price_sensor_interval(
        start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
        energy_mwh=579_100_000,
        rate=_sce_domestic_rate(),
        context=CostContext(billing_cycle_days=30),
    )
    assert [item.energy_mwh for item in result.slices] == [579_000_000, 100_000]
    assert [item.period_name for item in result.slices] == ["tier-one", "tier-two"]
    assert [item.price_per_kwh for item in result.slices] == [
        Decimal("0.30863"),
        Decimal("0.40962"),
    ]


def test_cca_adjustment_and_surcharge_use_decimal_arithmetic() -> None:
    periods = (PricePeriod("all", "all", "flat", 0, 1440, Decimal("0.20")),)
    version = rate(
        periods,
        cca_adjustment_per_kwh=Decimal("0.05"),
        surcharge_percent=Decimal("10"),
    )
    result = price_sensor_interval(
        start_utc=datetime(2026, 4, 1, tzinfo=UTC),
        end_utc=datetime(2026, 4, 1, 0, 1, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    assert result.total_microdollars == 275_000


def test_specific_schedule_overrides_an_all_day_fallback_deterministically() -> None:
    version = rate(
        (
            PricePeriod("all", "all", "fallback", 0, 1440, Decimal("0.20")),
            PricePeriod("summer", "weekday", "summer-weekday", 0, 1440, Decimal("0.40")),
        )
    )
    result = price_sensor_interval(
        start_utc=datetime(2026, 8, 13, 20, tzinfo=UTC),
        end_utc=datetime(2026, 8, 13, 20, 1, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    assert result.total_microdollars == 400_000
    assert {item.period_name for item in result.slices} == {"summer-weekday"}
