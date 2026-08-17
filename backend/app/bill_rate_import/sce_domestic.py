from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from ..errors import BillRateImportError
from ..schemas.billing import (
    AllowedRateField,
    RatePlanDraft,
    ReusableChargeDraft,
    SourceRegion,
    TierThresholdRuleDraft,
    TouPeriodDraft,
)

PARSER_VERSION = "sce-domestic-rates-v4"
_RATE_QUANTUM = Decimal("0.00000001")
_MONEY_QUANTUM = Decimal("0.01")
_UTILITY_PATTERN = re.compile(r"\b(?:SCE|SOUTHERN\s+CALIFORNIA\s+EDISON)\b", re.IGNORECASE)
_INFORMATIONAL_BREAKDOWN = re.compile(
    r"your\s+(?:delivery|generation|overall\s+energy)\s+charges\s+include\s*:?",
    re.IGNORECASE,
)
_BILLING_PERIOD_PATTERN = re.compile(
    r"billing\s+period\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*"
    r"(?:to|[-\u2013])\s*(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


def _error(code: str, detail: str) -> BillRateImportError:
    return BillRateImportError(detail, code=code)


def _decimal(raw: str, *, code: str) -> Decimal:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise _error(code, "A required reusable SCE rate component is invalid.") from exc
    if not value.is_finite() or value < 0 or value > Decimal("20"):
        raise _error(code, "A required reusable SCE rate component is outside safe bounds.")
    return value.quantize(_RATE_QUANTUM)


def _section(text: str, start: str, end: str | None) -> str:
    start_match = re.search(start, text, re.IGNORECASE)
    if start_match is None:
        return ""
    remainder = text[start_match.end() :]
    if end is None:
        return remainder
    end_match = re.search(end, remainder, re.IGNORECASE)
    return remainder[: end_match.start()] if end_match is not None else remainder


def _window_after_label(text: str, label: str, *, code: str) -> str:
    match = re.search(label, text, re.IGNORECASE)
    if match is None:
        raise _error(code, "A required labeled SCE rate component is missing.")
    return text[match.end() : match.end() + 220]


def _per_kwh_rate(text: str, label: str, *, code: str) -> Decimal:
    window = _window_after_label(text, label, code=code)
    patterns = (
        r"\b[\d,]+(?:\.\d+)?\s*kwh\s*(?:x|\u00d7)\s*\$?\s*(\d+(?:\.\d+)?)",
        r"\$?\s*(\d+(?:\.\d+)?)\s*(?:/\s*kwh|per\s+kwh)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, window, re.IGNORECASE)
        if match is not None:
            return _decimal(match.group(1), code=code)
    raise _error(code, "A required labeled SCE per-kWh rate is missing.")


def _daily_rate(text: str) -> Decimal:
    window = _window_after_label(
        text,
        r"base\s+services?\s+charge",
        code="BASE_CHARGE_NOT_FOUND",
    )
    patterns = (
        r"\b[\d,]+(?:\.\d+)?\s*days?\s*(?:x|\u00d7)\s*\$?\s*(\d+(?:\.\d+)?)",
        r"\$?\s*(\d+(?:\.\d+)?)\s*(?:/\s*day|per\s+day)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, window, re.IGNORECASE)
        if match is not None:
            return _decimal(match.group(1), code="BASE_CHARGE_NOT_FOUND")
    raise _error("BASE_CHARGE_NOT_FOUND", "The SCE daily base service rate is missing.")


def _parse_date(value: str) -> date | None:
    year = value.rsplit("/", 1)[-1]
    format_string = "%m/%d/%y" if len(year) == 2 else "%m/%d/%Y"
    try:
        return datetime.strptime(value, format_string).date()
    except ValueError:
        return None


def _optional_billing_period(text: str) -> tuple[date | None, date | None, int | None]:
    match = _BILLING_PERIOD_PATTERN.search(text)
    if match is None:
        return None, None, None
    start = _parse_date(match.group(1))
    end = _parse_date(match.group(2))
    if start is None or end is None:
        return None, None, None
    days = (end - start).days + 1
    if not 1 <= days <= 62:
        return None, None, None
    return start, end, days


def _explicit_billing_days(text: str) -> int | None:
    patterns = (
        r"billing\s+period[^\d]{0,120}\(\s*(\d{1,2})\s+days?\s*\)",
        r"base\s+services?\s+charge\s+(\d{1,2})\s+days?\b",
        r"\b(\d{1,2})\s+billing\s+days?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match is not None:
            days = int(match.group(1))
            return days if 1 <= days <= 62 else None
    return None


def _allowance_kwh(text: str) -> Decimal | None:
    match = re.search(
        r"(?:your\s+)?summer\s+baseline\s+allowance\s*:?\s*"
        r"([\d,]+(?:\.\d+)?)\s*kwh\b",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def _usage_quantity(text: str, label: str) -> Decimal | None:
    return _multiplier(text, label, "kwh")


def _total_usage_kwh(text: str) -> Decimal | None:
    match = re.search(
        r"(?:your\s+)?total\s+usage\s*:?\s*([\d,]+(?:\.\d+)?)\s*kwh\b",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _threshold_rule(
    *, primary: str, delivery: str, generation: str, billing_days: int | None
) -> TierThresholdRuleDraft | None:
    allowance = _allowance_kwh(primary)
    if allowance is None:
        return None
    delivery_one = _usage_quantity(delivery, r"tier\s*1\s*\(\s*within\s+baseline\s*\)")
    delivery_two = _usage_quantity(delivery, r"tier\s*2\s*\(\s*over\s+baseline\s*\)")
    generation_one = _usage_quantity(generation, r"tier\s*1\s*\(\s*within\s+baseline\s*\)")
    generation_two = _usage_quantity(generation, r"tier\s*2\s*\(\s*over\s+baseline\s*\)")
    tier_one_values = {value for value in (delivery_one, generation_one) if value is not None}
    tier_two_values = {value for value in (delivery_two, generation_two) if value is not None}
    if tier_one_values != {allowance} or len(tier_two_values) != 1:
        return None
    tier_two = next(iter(tier_two_values))
    stated_total = _total_usage_kwh(primary)
    charged_total = _usage_quantity(delivery, r"wildfire\s+fund\s+charge")
    totals = {value for value in (stated_total, charged_total) if value is not None}
    if len(totals) != 1 or allowance + tier_two != next(iter(totals)):
        return None
    per_day = allowance / Decimal(billing_days) if billing_days is not None else None
    if per_day is not None:
        exponent = per_day.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -8:
            return None
    return TierThresholdRuleDraft(
        season="summer",
        kwh_per_day=per_day,
        source_allowance_kwh=allowance,
        source_billing_days=billing_days,
        tier1_boundary_inclusive=True,
    )


def _multiplier(text: str, label: str, unit: Literal["kwh", "days"]) -> Decimal | None:
    match = re.search(label, text, re.IGNORECASE)
    if match is None:
        return None
    window = text[match.end() : match.end() + 180]
    suffix = r"kwh" if unit == "kwh" else r"days?"
    quantity = re.search(
        rf"\b([\d,]+(?:\.\d+)?)\s*{suffix}\s*(?:x|\u00d7)",
        window,
        re.IGNORECASE,
    )
    if quantity is None:
        return None
    try:
        value = Decimal(quantity.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value >= 0 else None


def _validate_optional_total(
    text: str,
    *,
    delivery: str,
    daily: Decimal,
    tier_one: Decimal,
    tier_two: Decimal,
) -> None:
    printed_match = next(
        (
            match
            for match in re.finditer(
                r"\byour\s+new\s+charges\s*:?\s*\$\s*([\d,]+(?:\.\d{2})?)\b",
                text,
                re.IGNORECASE,
            )
            if re.search(r"(?:details|subtotal)\s+of\s*$", text[: match.start()], re.IGNORECASE)
            is None
        ),
        None,
    )
    tier_one_kwh = _multiplier(delivery, r"tier\s*1\b", "kwh")
    tier_two_kwh = _multiplier(delivery, r"tier\s*2\b", "kwh")
    days = _multiplier(delivery, r"base\s+services?\s+charge", "days")
    if printed_match is None or tier_one_kwh is None or tier_two_kwh is None or days is None:
        return
    printed = Decimal(printed_match.group(1).replace(",", "")).quantize(_MONEY_QUANTUM)
    calculated = (tier_one_kwh * tier_one + tier_two_kwh * tier_two + days * daily).quantize(
        _MONEY_QUANTUM, rounding=ROUND_HALF_UP
    )
    if abs(calculated - printed) > Decimal("0.02"):
        raise _error(
            "RATE_RECONCILIATION_FAILED",
            "The exact SCE rate components do not reconcile with the printed charge total.",
        )


RateFieldName = Literal[
    "rate_plan_name",
    "rate_class",
    "season_definition",
    "tier_threshold",
    "per_kwh_rate",
    "delivery_rate_component",
    "generation_rate_component",
    "recurring_fixed_charge",
    "recurring_tax_or_surcharge_rule",
]


def _field(
    name: RateFieldName,
    value: str,
    page: int,
    label: str,
    confidence: str = "0.98",
) -> AllowedRateField:
    return AllowedRateField(
        name=name,
        normalized_value=value,
        supporting_label=label,
        confidence=Decimal(confidence),
        source=SourceRegion(page=page),
    )


def extract_sce_domestic_rate_draft(
    text: str,
    source_sha256: str,
    *,
    source_page: int,
) -> RatePlanDraft:
    """Extract exact reusable DOMESTIC rates without using customer bill dates as rules."""

    normalized = " ".join(text.split())
    if _UTILITY_PATTERN.search(normalized) is None:
        raise _error("UTILITY_NOT_RECOGNIZED", "The rate-detail page is not recognized as SCE.")
    required_signals = (
        r"details\s+of\s+your\s+new\s+charges",
        r"your\s+rate\s*:?\s*DOMESTIC\b",
        r"delivery\s+charges",
        r"generation\s+charges",
    )
    if (
        sum(re.search(signal, normalized, re.IGNORECASE) is not None for signal in required_signals)
        < 3
    ):
        raise _error("CHARGES_PAGE_NOT_FOUND", "No SCE rate-detail charges page was found.")

    primary = _INFORMATIONAL_BREAKDOWN.split(normalized, maxsplit=1)[0]
    delivery = _section(primary, r"delivery\s+charges", r"generation\s+charges")
    generation = _section(
        primary,
        r"generation\s+charges",
        r"subtotal\s+of\s+your\s+new\s+charges",
    )
    if not delivery or not generation:
        raise _error(
            "TIER_RATE_NOT_FOUND",
            "The SCE delivery or generation rate table is missing.",
        )

    daily = _daily_rate(delivery)
    delivery_one = _per_kwh_rate(delivery, r"tier\s*1\b", code="TIER_RATE_NOT_FOUND")
    delivery_two = _per_kwh_rate(delivery, r"tier\s*2\b", code="TIER_RATE_NOT_FOUND")
    generation_one = _per_kwh_rate(generation, r"tier\s*1\b", code="TIER_RATE_NOT_FOUND")
    generation_two = _per_kwh_rate(generation, r"tier\s*2\b", code="TIER_RATE_NOT_FOUND")
    wildfire = _per_kwh_rate(
        delivery,
        r"wildfire\s+fund\s+charge",
        code="RATE_COMPONENT_INVALID",
    )
    recovery = _per_kwh_rate(
        generation,
        r"fixed\s+recovery\s+charge",
        code="RATE_COMPONENT_INVALID",
    )
    state_tax = _per_kwh_rate(
        primary,
        r"state\s+tax",
        code="RATE_COMPONENT_INVALID",
    )
    tier_one = sum(
        (delivery_one, generation_one, wildfire, recovery, state_tax),
        start=Decimal("0"),
    ).quantize(_RATE_QUANTUM)
    tier_two = sum(
        (delivery_two, generation_two, wildfire, recovery, state_tax),
        start=Decimal("0"),
    ).quantize(_RATE_QUANTUM)
    _validate_optional_total(
        primary,
        delivery=delivery,
        daily=daily,
        tier_one=tier_one,
        tier_two=tier_two,
    )

    period_start, period_end, period_days = _optional_billing_period(primary)
    period_days = period_days or _explicit_billing_days(primary)
    threshold_rule = _threshold_rule(
        primary=normalized,
        delivery=delivery,
        generation=generation,
        billing_days=period_days,
    )
    threshold_status = "bill_baseline_allowance" if threshold_rule else "review_required"
    fields = (
        _field("rate_plan_name", "DOMESTIC", source_page, "Your rate"),
        _field("rate_class", "residential_tiered", source_page, "Your rate"),
        _field("season_definition", "summer", source_page, "Energy-Summer", "0.97"),
        _field(
            "tier_threshold",
            (
                f"summer_daily_allowance={threshold_rule.kwh_per_day} kWh/day; "
                f"source_allowance={threshold_rule.source_allowance_kwh} kWh; "
                f"source_days={threshold_rule.source_billing_days}; boundary=inclusive"
                if threshold_rule and threshold_rule.kwh_per_day is not None
                else (
                    f"summer_source_allowance={threshold_rule.source_allowance_kwh} kWh; "
                    "billing_days=review_required"
                    if threshold_rule
                    else threshold_status
                )
            ),
            source_page,
            "Summer baseline allowance",
            "0.99" if threshold_rule else "0.50",
        ),
        _field(
            "delivery_rate_component",
            f"tier_1={delivery_one} USD/kWh",
            source_page,
            "Delivery Tier 1",
        ),
        _field(
            "delivery_rate_component",
            f"tier_2={delivery_two} USD/kWh",
            source_page,
            "Delivery Tier 2",
        ),
        _field(
            "generation_rate_component",
            f"tier_1={generation_one} USD/kWh",
            source_page,
            "Generation Tier 1",
        ),
        _field(
            "generation_rate_component",
            f"tier_2={generation_two} USD/kWh",
            source_page,
            "Generation Tier 2",
        ),
        _field(
            "per_kwh_rate",
            f"tier_1={tier_one} USD/kWh",
            source_page,
            "Tier 1 all-in rate",
        ),
        _field(
            "per_kwh_rate",
            f"tier_2={tier_two} USD/kWh",
            source_page,
            "Tier 2 all-in rate",
        ),
        _field(
            "recurring_fixed_charge",
            f"{daily} USD/day",
            source_page,
            "Base services charge",
        ),
        _field(
            "recurring_tax_or_surcharge_rule",
            f"wildfire_fund={wildfire} USD/kWh",
            source_page,
            "Wildfire fund charge",
        ),
        _field(
            "recurring_tax_or_surcharge_rule",
            f"fixed_recovery={recovery} USD/kWh",
            source_page,
            "Fixed recovery charge",
        ),
        _field(
            "recurring_tax_or_surcharge_rule",
            f"state_tax={state_tax} USD/kWh",
            source_page,
            "State tax",
        ),
    )
    periods = (
        TouPeriodDraft(
            season="summer",
            day_type="all",
            name="tier_1",
            start_minute=0,
            end_minute=1440,
            price_per_kwh=tier_one,
            delivery_per_kwh=delivery_one,
            generation_per_kwh=generation_one,
            tier_end_kwh=(
                threshold_rule.source_allowance_kwh
                if threshold_rule and threshold_rule.kwh_per_day is not None
                else None
            ),
        ),
        TouPeriodDraft(
            season="summer",
            day_type="all",
            name="tier_2",
            start_minute=0,
            end_minute=1440,
            price_per_kwh=tier_two,
            delivery_per_kwh=delivery_two,
            generation_per_kwh=generation_two,
            tier_start_kwh=(
                threshold_rule.source_allowance_kwh
                if threshold_rule and threshold_rule.kwh_per_day is not None
                else Decimal("0")
            ),
        ),
    )
    charges = (
        ReusableChargeDraft(
            name="Base Services Charge",
            kind="daily_fixed",
            amount=daily,
            unit="USD/day",
        ),
        ReusableChargeDraft(
            name="Wildfire fund charge",
            kind="per_kwh",
            amount=wildfire,
            unit="USD/kWh",
        ),
        ReusableChargeDraft(
            name="Fixed recovery charge",
            kind="per_kwh",
            amount=recovery,
            unit="USD/kWh",
        ),
        ReusableChargeDraft(
            name="State tax",
            kind="per_kwh",
            amount=state_tax,
            unit="USD/kWh",
        ),
    )
    return RatePlanDraft(
        rate_plan_name="DOMESTIC",
        rate_class="residential_tiered",
        plan_classification="seasonal_tiered",
        holiday_treatment="not_applicable",
        cca_or_direct_access_indicator="sce_generation",
        verified_seasons=("summer",),
        periods=periods,
        tier_threshold_rule=threshold_rule,
        reusable_charges=charges,
        billing_period_start=period_start,
        billing_period_end=period_end,
        billing_period_days=period_days,
        tier_threshold_basis=threshold_status,
        baseline_allocation_rule="daily_allowance" if threshold_rule else None,
        fields=fields,
        parser_version=PARSER_VERSION,
        source_artifact_sha256=source_sha256,
        candidate_complete=threshold_rule is not None and threshold_rule.kwh_per_day is not None,
    )
