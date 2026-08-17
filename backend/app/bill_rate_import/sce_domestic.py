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
    TouPeriodDraft,
)

PARSER_VERSION = "sce-domestic-rates-v3"
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
    threshold_status = (
        "No reusable tier threshold was established from this bill; retain the existing "
        "configured threshold and require administrator review."
    )
    fields = (
        _field("rate_plan_name", "DOMESTIC", source_page, "Your rate"),
        _field("rate_class", "residential_tiered", source_page, "Your rate"),
        _field("season_definition", "summer", source_page, "Energy-Summer", "0.97"),
        _field("tier_threshold", threshold_status, source_page, "Tier threshold status", "0.99"),
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
        periods=periods,
        reusable_charges=charges,
        billing_period_start=period_start,
        billing_period_end=period_end,
        billing_period_days=period_days,
        tier_threshold_basis=threshold_status,
        baseline_allocation_rule=(
            "Retain the existing configured reusable baseline threshold; the customer-specific "
            "bill-period allowance is not imported."
        ),
        fields=fields,
        parser_version=PARSER_VERSION,
        source_artifact_sha256=source_sha256,
        candidate_complete=False,
    )
