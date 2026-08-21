from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from backend.app.services.cost_engine import (
    CostContext,
    DatedPrice,
    EventCalendar,
    FixedCharge,
    PricePeriod,
    RateEvaluationError,
    RateVersion,
    SeasonDefinition,
    fixed_charge_microdollars,
    fixed_charges_from_storage,
    price_sensor_interval,
    resolve_price_period,
    season_definitions_from_storage,
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


@pytest.mark.parametrize(
    "stored",
    [
        [{"season_name": "bad", "start_month": 1, "end_month": 13}],
        [
            {
                "season_name": "bad",
                "start_month": 2,
                "start_day": 30,
                "end_month": 3,
                "end_day": 1,
            }
        ],
    ],
)
def test_malformed_stored_season_values_use_rate_evaluation_error(
    stored: list[dict[str, object]],
) -> None:
    with pytest.raises(RateEvaluationError, match="stored season definition is malformed"):
        season_definitions_from_storage(stored)


def test_malformed_stored_fixed_charge_decimal_uses_rate_evaluation_error() -> None:
    with pytest.raises(RateEvaluationError, match="stored fixed charge is malformed"):
        fixed_charges_from_storage(
            [
                {
                    "charge": "daily_fixed_charge",
                    "amount": "not-a-decimal",
                    "applies": "per_account_per_day",
                }
            ]
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


def test_dynamic_prices_distinguish_both_fall_back_hours_in_utc() -> None:
    version = rate(
        (),
        dated_prices=(
            DatedPrice(
                datetime(2026, 11, 1, 8, tzinfo=UTC),
                datetime(2026, 11, 1, 9, tzinfo=UTC),
                "first-1am-pdt",
                Decimal("0.10"),
            ),
            DatedPrice(
                datetime(2026, 11, 1, 9, tzinfo=UTC),
                datetime(2026, 11, 1, 10, tzinfo=UTC),
                "second-1am-pst",
                Decimal("0.30"),
            ),
        ),
    )
    result = price_sensor_interval(
        start_utc=datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
        end_utc=datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    first_hour = [item for item in result.slices if item.period_name == "first-1am-pdt"]
    second_hour = [item for item in result.slices if item.period_name == "second-1am-pst"]
    assert sum(item.energy_mwh for item in first_hour) == 500_000
    assert sum(item.energy_mwh for item in second_hour) == 500_000
    assert len(first_hour) == len(second_hour) == 1
    assert result.total_microdollars == 200_000


def test_dynamic_price_gap_fails_closed() -> None:
    version = rate(
        (),
        dated_prices=(
            DatedPrice(
                datetime(2026, 8, 1, 0, tzinfo=UTC),
                datetime(2026, 8, 1, 0, 30, tzinfo=UTC),
                "known",
                Decimal("0.10"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="resolved to 0 prices"):
        price_sensor_interval(
            start_utc=datetime(2026, 8, 1, 0, 30, tzinfo=UTC),
            end_utc=datetime(2026, 8, 1, 1, tzinfo=UTC),
            energy_mwh=500_000,
            rate=version,
        )


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
    return rate(
        (
            PricePeriod(
                "summer",
                "all",
                "tier-one",
                0,
                1440,
                Decimal("0.30863"),
                tier_end_kwh=Decimal("1"),
                boundary_inclusive=True,
                threshold_basis="account_daily_baseline",
            ),
            PricePeriod(
                "summer",
                "all",
                "tier-two",
                0,
                1440,
                Decimal("0.40962"),
                tier_start_kwh=Decimal("1"),
                boundary_inclusive=True,
                threshold_basis="account_daily_baseline",
            ),
        ),
        daily_fixed_charge=Decimal("0.769"),
    )


def _sce_context(days: int, *, cumulative: Decimal = Decimal("0")) -> CostContext:
    return CostContext(
        cumulative_cycle_kwh_before=cumulative,
        billing_cycle_days=days,
        tier_threshold_cycle_kwh=Decimal("19.3") * days,
        tier_threshold_kwh_per_day=Decimal("19.3"),
        tier_threshold_season="summer",
        tier1_boundary_inclusive=True,
    )


def test_sce_daily_allowance_prorates_and_keeps_the_boundary_in_tier_one() -> None:
    version = _sce_domestic_rate()
    for days, threshold in ((28, Decimal("540.4")), (30, Decimal("579.0")), (31, Decimal("598.3"))):
        exact = price_sensor_interval(
            start_utc=datetime(2026, 7, 1, tzinfo=UTC),
            end_utc=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
            energy_mwh=int(threshold * Decimal(1_000_000)),
            rate=version,
            context=_sce_context(days),
        )
        assert {item.period_name for item in exact.slices} == {"tier-one"}
        crossing = price_sensor_interval(
            start_utc=datetime(2026, 7, 1, tzinfo=UTC),
            end_utc=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
            energy_mwh=100_000,
            rate=version,
            context=_sce_context(days, cumulative=threshold),
        )
        assert {item.period_name for item in crossing.slices} == {"tier-two"}


def test_sce_source_bill_reconciles_through_the_existing_cost_engine() -> None:
    version = _sce_domestic_rate()
    result = price_sensor_interval(
        start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        end_utc=datetime(2026, 7, 1, 0, 1, tzinfo=UTC),
        energy_mwh=951_000_000,
        rate=version,
        context=_sce_context(30),
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
        context=_sce_context(30),
    )
    assert [item.energy_mwh for item in result.slices] == [579_000_000, 100_000]
    assert [item.period_name for item in result.slices] == ["tier-one", "tier-two"]
    assert [item.price_per_kwh for item in result.slices] == [
        Decimal("0.30863"),
        Decimal("0.40962"),
    ]


def test_account_threshold_boundary_resolves_tier_one_then_tier_two() -> None:
    version = _sce_domestic_rate()
    context = _sce_context(30)
    at_boundary = resolve_price_period(
        version,
        datetime(2026, 7, 1, tzinfo=UTC),
        Decimal("579.0"),
        context,
    )
    above_boundary = resolve_price_period(
        version,
        datetime(2026, 7, 1, tzinfo=UTC),
        Decimal("579.000001"),
        context,
    )
    assert at_boundary.name == "tier-one"
    assert above_boundary.name == "tier-two"


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


def test_version_owned_season_boundary_splits_exactly_at_local_midnight() -> None:
    version = rate(
        (
            PricePeriod("warm", "all", "warm", 0, 1440, Decimal("0.50")),
            PricePeriod("cool", "all", "cool", 0, 1440, Decimal("0.10")),
        ),
        season_definitions=(
            SeasonDefinition("warm", 5, 15, 10, 14),
            SeasonDefinition("cool", 10, 15, 5, 14),
        ),
    )
    result = price_sensor_interval(
        # 07:00 UTC is local midnight at the May 15 season boundary.
        start_utc=datetime(2026, 5, 15, 6, 59, 30, tzinfo=UTC),
        end_utc=datetime(2026, 5, 15, 7, 0, 30, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    assert [item.period_name for item in result.slices] == ["cool", "warm"]
    assert [item.energy_mwh for item in result.slices] == [500_000, 500_000]
    assert result.total_microdollars == 300_000


def test_event_override_and_holiday_mapping_are_local_date_exact() -> None:
    event_date = date(2026, 8, 13)
    periods = (
        PricePeriod("all", "weekday", "weekday", 0, 1440, Decimal("0.20")),
        PricePeriod("all", "weekend", "weekend", 0, 1440, Decimal("0.10")),
        PricePeriod("all", "event_day", "event", 16 * 60, 21 * 60, Decimal("0.80")),
    )
    version = rate(
        periods,
        holiday_treatment="same_as_weekend",
        holiday_calendar=EventCalendar(
            local_dates=frozenset({date(2026, 8, 14)}),
            coverage_start=event_date,
            coverage_end=date(2026, 8, 14),
        ),
        event_calendar=EventCalendar(
            local_dates=frozenset({event_date}),
            coverage_start=event_date,
            coverage_end=date(2026, 8, 14),
        ),
    )
    event = price_sensor_interval(
        start_utc=datetime(2026, 8, 14, 0, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, 14, 0, 1, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    ordinary = price_sensor_interval(
        start_utc=datetime(2026, 8, 13, 19, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, 13, 19, 1, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    holiday = price_sensor_interval(
        start_utc=datetime(2026, 8, 14, 22, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, 14, 22, 1, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    assert event.total_microdollars == 800_000
    assert ordinary.total_microdollars == 200_000
    assert holiday.total_microdollars == 100_000


def test_holiday_sensitive_schedule_requires_bounded_calendar_coverage() -> None:
    periods = (
        PricePeriod("all", "weekday", "weekday", 0, 1440, Decimal("0.20")),
        PricePeriod("all", "weekend", "weekend", 0, 1440, Decimal("0.10")),
    )
    unresolved = rate(periods, holiday_treatment="same_as_weekend")
    with pytest.raises(ValueError, match="holiday calendar is unresolved"):
        price_sensor_interval(
            start_utc=datetime(2026, 8, 13, 19, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 13, 19, 1, tzinfo=UTC),
            energy_mwh=1_000_000,
            rate=unresolved,
        )
    bounded = rate(
        periods,
        holiday_treatment="same_as_weekend",
        holiday_calendar=EventCalendar(
            local_dates=frozenset({date(2026, 8, 13)}),
            coverage_start=date(2026, 8, 13),
            coverage_end=date(2026, 8, 14),
        ),
    )
    holiday = price_sensor_interval(
        start_utc=datetime(2026, 8, 13, 19, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, 13, 19, 1, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=bounded,
    )
    assert holiday.total_microdollars == 100_000
    with pytest.raises(ValueError, match="does not cover"):
        price_sensor_interval(
            start_utc=datetime(2026, 8, 15, 19, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 15, 19, 1, tzinfo=UTC),
            energy_mwh=1_000_000,
            rate=bounded,
        )


def test_event_schedule_without_exact_calendar_fails_closed() -> None:
    version = rate(
        (
            PricePeriod("all", "all", "ordinary", 0, 1440, Decimal("0.20")),
            PricePeriod("all", "event_day", "event", 960, 1260, Decimal("0.80")),
        ),
        event_calendar=None,
    )
    with pytest.raises(ValueError, match="event calendar is unresolved"):
        price_sensor_interval(
            start_utc=datetime(2026, 8, 14, 0, 0, tzinfo=UTC),
            end_utc=datetime(2026, 8, 14, 0, 1, tzinfo=UTC),
            energy_mwh=1_000_000,
            rate=version,
        )


def test_event_calendar_fails_closed_before_and_after_its_bounded_coverage() -> None:
    version = rate(
        (
            PricePeriod("all", "all", "ordinary", 0, 1440, Decimal("0.20")),
            PricePeriod("all", "event_day", "event", 0, 1440, Decimal("0.80")),
        ),
        event_calendar=EventCalendar(
            local_dates=frozenset({date(2026, 8, 13)}),
            coverage_start=date(2026, 8, 13),
            coverage_end=date(2026, 8, 14),
        ),
    )
    inside = price_sensor_interval(
        start_utc=datetime(2026, 8, 13, 19, 0, tzinfo=UTC),
        end_utc=datetime(2026, 8, 13, 19, 1, tzinfo=UTC),
        energy_mwh=1_000_000,
        rate=version,
    )
    assert inside.total_microdollars == 800_000
    for instant in (
        datetime(2026, 8, 12, 19, 0, tzinfo=UTC),
        datetime(2026, 8, 15, 19, 0, tzinfo=UTC),
    ):
        with pytest.raises(ValueError, match="does not cover"):
            price_sensor_interval(
                start_utc=instant,
                end_utc=instant + timedelta(minutes=1),
                energy_mwh=1_000_000,
                rate=version,
            )


def test_fixed_charge_recurrences_minimum_floor_and_meter_count_are_exact() -> None:
    version = rate(
        (PricePeriod("all", "all", "flat", 0, 1440, Decimal("0.20")),),
        fixed_charges=(
            FixedCharge("daily_fixed_charge", Decimal("0.50"), "per_account_per_day"),
            FixedCharge("meter_charge", Decimal("2"), "per_meter_per_cycle"),
            FixedCharge("minimum_charge", Decimal("20"), "per_account_per_cycle"),
        ),
    )
    fixed = fixed_charge_microdollars(
        version,
        date(2026, 6, 22),
        date(2026, 6, 24),
        scope="full_account",
        meter_count=1,
        variable_charge_microdollars=10_000_000,
    )
    # $1 daily + $2 meter + $7 minimum top-up; variable plus fixed is exactly $20.
    assert fixed == 10_000_000
    with pytest.raises(ValueError, match="utility meter count"):
        fixed_charge_microdollars(
            version,
            date(2026, 6, 22),
            date(2026, 6, 24),
            scope="full_account",
            variable_charge_microdollars=10_000_000,
        )


def test_monthly_charge_counts_each_touched_calendar_month_at_boundaries() -> None:
    version = rate(
        (PricePeriod("all", "all", "flat", 0, 1440, Decimal("0.20")),),
        fixed_charges=(
            FixedCharge(
                "monthly_fixed_charge",
                Decimal("3.25"),
                "per_account_per_month",
            ),
        ),
    )
    assert (
        fixed_charge_microdollars(
            version,
            date(2026, 6, 1),
            date(2026, 7, 1),
            scope="full_account",
        )
        == 3_250_000
    )
    assert (
        fixed_charge_microdollars(
            version,
            date(2026, 6, 22),
            date(2026, 6, 24),
            scope="full_account",
        )
        == 3_250_000
    )
    assert (
        fixed_charge_microdollars(
            version,
            date(2026, 6, 22),
            date(2026, 7, 22),
            scope="full_account",
        )
        == 6_500_000
    )
