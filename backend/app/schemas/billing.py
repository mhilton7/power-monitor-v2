from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RateDecimal = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=8)]


class SourceRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(ge=1)
    x: Decimal | None = Field(default=None, ge=0)
    y: Decimal | None = Field(default=None, ge=0)
    width: Decimal | None = Field(default=None, ge=0)
    height: Decimal | None = Field(default=None, ge=0)


class AllowedRateField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Literal[
        "rate_plan_name",
        "rate_class",
        "cca_or_direct_access_indicator",
        "season_definition",
        "day_type_definition",
        "tou_period",
        "tier_threshold",
        "baseline_allocation_rule",
        "baseline_credit_rate",
        "per_kwh_rate",
        "delivery_rate_component",
        "generation_rate_component",
        "recurring_fixed_charge",
        "recurring_tax_or_surcharge_rule",
        "recurring_credit_or_adjustment_rule",
        "tariff_effective_date",
    ]
    normalized_value: str = Field(min_length=1, max_length=500)
    supporting_label: str | None = Field(default=None, min_length=1, max_length=160)
    confidence: Decimal = Field(ge=0, le=1)
    source: SourceRegion


class TouPeriodDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    season: Literal["summer", "winter", "all"]
    day_type: Literal["weekday", "weekend", "holiday", "all"]
    name: str = Field(min_length=1, max_length=40)
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(gt=0, le=1440)
    price_per_kwh: RateDecimal
    delivery_per_kwh: RateDecimal = Decimal("0")
    generation_per_kwh: RateDecimal = Decimal("0")
    tier_start_kwh: Decimal = Field(default=Decimal("0"), ge=0)
    tier_end_kwh: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered(self) -> TouPeriodDraft:
        if self.end_minute <= self.start_minute:
            raise ValueError("TOU period must be ordered and may not cross midnight")
        if self.tier_end_kwh is not None and self.tier_end_kwh <= self.tier_start_kwh:
            raise ValueError("tier end must exceed tier start")
        return self


class ReusableChargeDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["daily_fixed", "monthly_fixed", "per_kwh", "percentage", "credit"]
    amount: Decimal
    unit: Literal["USD/day", "USD/month", "USD/kWh", "percent"]


class TierThresholdRuleDraft(BaseModel):
    """Structured, operational tier boundary derived from rate-only evidence."""

    model_config = ConfigDict(extra="forbid")
    rule_type: Literal["daily_allowance"] = "daily_allowance"
    season: Literal["summer", "winter"]
    kwh_per_day: Decimal | None = Field(
        default=None, gt=0, le=Decimal("1000"), max_digits=18, decimal_places=8
    )
    source_allowance_kwh: Decimal = Field(
        gt=0, le=Decimal("100000"), max_digits=18, decimal_places=8
    )
    source_billing_days: int | None = Field(default=None, ge=1, le=62)
    tier1_boundary_inclusive: Literal[True] = True

    @model_validator(mode="after")
    def source_values_reconcile(self) -> TierThresholdRuleDraft:
        if (self.kwh_per_day is None) != (self.source_billing_days is None):
            raise ValueError("daily allowance and billing-day evidence must be completed together")
        if (
            self.kwh_per_day is not None
            and self.source_billing_days is not None
            and self.kwh_per_day * self.source_billing_days != self.source_allowance_kwh
        ):
            raise ValueError("daily tier allowance does not reconcile with its source period")
        return self


class RatePlanDraft(BaseModel):
    """Closed rate-only parser output. No arbitrary metadata or consumption fields."""

    model_config = ConfigDict(extra="forbid")
    utility_name: Literal["Southern California Edison"] = "Southern California Edison"
    rate_plan_name: str = Field(min_length=1, max_length=120)
    rate_class: str = Field(min_length=1, max_length=80)
    plan_classification: Literal["flat", "tiered", "seasonal_tiered", "time_of_use", "unknown"] = (
        "time_of_use"
    )
    holiday_treatment: Literal[
        "not_applicable",
        "no_special_treatment",
        "weekend_schedule",
        "explicit_schedule",
        "unresolved",
    ] = "unresolved"
    cca_or_direct_access_indicator: Literal["sce_generation", "cca", "direct_access", "unknown"]
    summer_months: tuple[int, ...] = (6, 7, 8, 9)
    winter_months: tuple[int, ...] = (1, 2, 3, 4, 5, 10, 11, 12)
    periods: tuple[TouPeriodDraft, ...]
    verified_seasons: tuple[Literal["summer", "winter"], ...] = ("summer", "winter")
    tier_threshold_rule: TierThresholdRuleDraft | None = None
    baseline_allocation_rule: str | None = Field(default=None, max_length=500)
    baseline_credit_rate: RateDecimal | None = None
    reusable_charges: tuple[ReusableChargeDraft, ...] = ()
    billing_period_start: date | None = None
    billing_period_end: date | None = None
    billing_period_days: int | None = Field(default=None, ge=1, le=62)
    tier_threshold_basis: str | None = Field(default=None, max_length=500)
    effective_start_candidate: date | None = None
    effective_end_candidate: date | None = None
    fields: tuple[AllowedRateField, ...]
    parser_version: str = Field(min_length=1, max_length=40)
    source_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_required: Literal[True] = True
    candidate_complete: bool = True

    @model_validator(mode="after")
    def validate_period_coverage(self) -> RatePlanDraft:
        if not self.periods:
            raise ValueError("at least one reusable rate period is required")
        months = (*self.summer_months, *self.winter_months)
        if sorted(months) != list(range(1, 13)):
            raise ValueError("summer and winter months must partition all twelve months")

        if not self.verified_seasons or len(set(self.verified_seasons)) != len(
            self.verified_seasons
        ):
            raise ValueError("verified rate seasons must be non-empty and unique")
        if (
            self.tier_threshold_rule is not None
            and self.tier_threshold_rule.season not in self.verified_seasons
        ):
            raise ValueError("tier threshold season must be supported by verified rate evidence")

        # A genuinely incomplete draft may retain rate evidence for correction.
        if not self.candidate_complete:
            return self

        if self.plan_classification == "seasonal_tiered" and (
            self.tier_threshold_rule is None
            or self.tier_threshold_rule.kwh_per_day is None
            or self.tier_threshold_rule.source_billing_days is None
        ):
            raise ValueError("a complete seasonal tiered plan requires a structured threshold")

        # Validate the schedule that the pricing engine will actually resolve,
        # including `all` fallbacks, holiday treatment, and tier boundaries.
        # A group merely being absent must never make an incomplete schedule
        # appear valid.
        thresholds = {Decimal("0")}
        for period in self.periods:
            thresholds.add(period.tier_start_kwh)
            if period.tier_end_kwh is not None:
                thresholds.add(period.tier_end_kwh)
        ordered_thresholds = sorted(thresholds)
        tier_samples = set(ordered_thresholds)
        for left, right in pairwise(ordered_thresholds):
            tier_samples.add((left + right) / Decimal(2))
        tier_samples.add(ordered_thresholds[-1] + Decimal("1"))

        for season in self.verified_seasons:
            for day_type in ("weekday", "weekend", "holiday"):
                for minute in range(1440):
                    for cumulative in tier_samples:
                        candidates = [
                            period
                            for period in self.periods
                            if period.season in (season, "all")
                            and period.day_type in (day_type, "all")
                            and period.start_minute <= minute < period.end_minute
                            and cumulative >= period.tier_start_kwh
                            and (period.tier_end_kwh is None or cumulative < period.tier_end_kwh)
                        ]
                        if candidates:
                            specificity = max(
                                int(period.season == season) + int(period.day_type == day_type)
                                for period in candidates
                            )
                            candidates = [
                                period
                                for period in candidates
                                if int(period.season == season) + int(period.day_type == day_type)
                                == specificity
                            ]
                        if len(candidates) != 1:
                            raise ValueError(
                                "rate schedule must resolve exactly once for every "
                                f"season/day/minute/tier; got {len(candidates)} at "
                                f"{season}/{day_type}/{minute}/{cumulative}"
                            )
        return self
