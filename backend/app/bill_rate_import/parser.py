from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast

from pypdf import PdfReader

from ..constants import MAX_PDF_BYTES, MAX_PDF_PAGES
from ..errors import BillRateImportError
from ..schemas.billing import (
    AllowedRateField,
    RatePlanDraft,
    ReusableChargeDraft,
    SourceRegion,
    TouPeriodDraft,
)

PARSER_VERSION = "sce-rate-only-v2"

# Categories only. Matches are never returned or logged.
PROHIBITED_PATTERNS: dict[str, re.Pattern[str]] = {
    "CUSTOMER_IDENTITY": re.compile(
        r"(?im)^\s*(customer|service address|account(?: number)?)\s*[:#]"
    ),
    "METER_IDENTIFIER": re.compile(r"(?im)^\s*meter(?: number| #)?\s*[:#]"),
    "BILL_USAGE": re.compile(r"(?im)(total\s+kwh|usage\s+(?:this|last)|meter\s+reading)"),
    "BILL_TOTAL": re.compile(r"(?im)(amount\s+due|total\s+bill|current\s+balance|past\s+due)"),
    "PAYMENT": re.compile(r"(?im)(payment\s+(?:history|method)|autopay|bank\s+account)"),
}

PLAN_PATTERN = re.compile(r"\b(DOMESTIC|TOU-D-(?:4-9PM|5-8PM|PRIME))\b", re.IGNORECASE)
PERIOD_PATTERN = re.compile(
    r"(?im)^\s*(summer|winter|all)\s+"
    r"(weekday|weekend|holiday|all)\s+"
    r"(off-peak|super-off-peak|mid-peak|on-peak)\s+"
    r"([01]?\d|2[0-3]):([0-5]\d)\s*(?:-|\u2013)\s*"
    r"(?:24:00|([01]?\d|2[0-3]):([0-5]\d))\s+"
    r"\$?(\d+(?:\.\d+)?)\s*(?:/\s*kwh|per\s+kwh)",
)
BASELINE_PATTERN = re.compile(r"(?i)baseline\s+credit[^\d$]*\$?(\d+(?:\.\d+)?)\s*/?\s*kwh")
DAILY_PATTERN = re.compile(
    r"(?i)(?:base\s+services?|daily\s+service)\s+charge[^\d$]*\$?(\d+(?:\.\d+)?)\s*(?:/|per)\s*day"
)

_CHARGES_ANCHORS = (
    "details of your new charges",
    "your rate",
    "billing period",
    "delivery charges",
    "generation charges",
    "base services charge",
    "energy-summer",
)
_PRIMARY_BREAKDOWN = re.compile(r"your\s+delivery\s+charges\s+include\s*:", re.IGNORECASE)
_BILLING_PERIOD_PATTERN = re.compile(
    r"billing\s+period\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:to|[-\u2013])\s*"
    r"(\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


def detected_prohibited_categories(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in PROHIBITED_PATTERNS.items() if pattern.search(text))


def _minutes(hour: str, minute: str) -> int:
    return int(hour) * 60 + int(minute)


def _normalize_price(raw: str) -> Decimal:
    value = Decimal(raw)
    return value / Decimal(100) if value > Decimal("2") else value


def _bill_error(code: str, detail: str) -> BillRateImportError:
    return BillRateImportError(detail, code=code)


def _decimal(raw: str) -> Decimal:
    try:
        value = Decimal(raw.replace(",", ""))
    except Exception as exc:
        raise _bill_error("RATE_LINES_NOT_FOUND", "A reusable SCE rate line is invalid.") from exc
    if not value.is_finite() or value < 0 or value > Decimal("20"):
        raise _bill_error("RATE_LINES_NOT_FOUND", "A reusable SCE rate is outside safe bounds.")
    return value.quantize(Decimal("0.00000001"))


def _quantity_decimal(raw: str) -> Decimal:
    try:
        value = Decimal(raw.replace(",", ""))
    except Exception as exc:
        raise _bill_error("RATE_LINES_NOT_FOUND", "A reusable SCE threshold is invalid.") from exc
    if not value.is_finite() or value <= 0 or value > Decimal("1000000"):
        raise _bill_error(
            "RATE_LINES_NOT_FOUND", "A reusable SCE threshold is outside safe bounds."
        )
    return value.quantize(Decimal("0.00000001"))


def _page_score(text: str) -> int:
    normalized = " ".join(text.lower().split())
    return sum(anchor in normalized for anchor in _CHARGES_ANCHORS)


def _section(text: str, start: str, end: str | None) -> str:
    start_match = re.search(start, text, re.IGNORECASE)
    if start_match is None:
        return ""
    remainder = text[start_match.end() :]
    if end is None:
        return remainder
    end_match = re.search(end, remainder, re.IGNORECASE)
    return remainder[: end_match.start()] if end_match is not None else remainder


def _unit_rate(section: str, label: str) -> Decimal:
    match = re.search(
        rf"{label}.{{0,140}}?\bkwh\s*(?:x|\u00d7)\s*\$\s*(\d+(?:\.\d+)?)",
        section,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise _bill_error("RATE_LINES_NOT_FOUND", "A required reusable SCE rate line is missing.")
    return _decimal(match.group(1))


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


def _parse_bill_date(value: str) -> date:
    fmt = "%m/%d/%y" if len(value.rsplit("/", 1)[-1]) == 2 else "%m/%d/%Y"
    try:
        return datetime.strptime(value, fmt).date()
    except ValueError as exc:
        raise _bill_error(
            "RATE_LINES_NOT_FOUND", "The SCE billing-period dates are invalid."
        ) from exc


def _extract_domestic(
    text: str,
    source_sha256: str,
    *,
    source_page: int,
) -> RatePlanDraft:
    primary = _PRIMARY_BREAKDOWN.split(text, maxsplit=1)[0]
    delivery = _section(primary, r"delivery\s+charges", r"generation\s+charges")
    generation = _section(primary, r"generation\s+charges", r"subtotal\s+of\s+your\s+new\s+charges")
    if not delivery or not generation:
        raise _bill_error(
            "RATE_LINES_NOT_FOUND", "The SCE delivery or generation rate table is missing."
        )

    daily_match = re.search(
        r"base\s+services?\s+charge.{0,100}?(?:x|\u00d7)\s*\$\s*(\d+(?:\.\d+)?)",
        delivery,
        re.IGNORECASE | re.DOTALL,
    )
    if daily_match is None:
        raise _bill_error("RATE_LINES_NOT_FOUND", "The SCE daily base service rate is missing.")
    daily = _decimal(daily_match.group(1))
    delivery_one = _unit_rate(delivery, r"tier\s*1\b")
    delivery_two = _unit_rate(delivery, r"tier\s*2\b")
    generation_one = _unit_rate(generation, r"tier\s*1\b")
    generation_two = _unit_rate(generation, r"tier\s*2\b")
    wildfire = _unit_rate(delivery, r"wildfire\s+fund\s+charge")
    recovery = _unit_rate(generation, r"fixed\s+recovery\s+charge")
    state_tax = _unit_rate(primary, r"state\s+tax")
    tier_one = sum(
        (delivery_one, generation_one, wildfire, recovery, state_tax), start=Decimal("0")
    ).quantize(Decimal("0.00000001"))
    tier_two = sum(
        (delivery_two, generation_two, wildfire, recovery, state_tax), start=Decimal("0")
    ).quantize(Decimal("0.00000001"))

    period_match = _BILLING_PERIOD_PATTERN.search(primary)
    if period_match is None:
        raise _bill_error("RATE_LINES_NOT_FOUND", "The SCE billing period is missing.")
    period_start = _parse_bill_date(period_match.group(1))
    period_end = _parse_bill_date(period_match.group(2))
    period_days = (period_end - period_start).days + 1
    if not 1 <= period_days <= 62:
        raise _bill_error("RATE_LINES_NOT_FOUND", "The SCE billing period is outside safe bounds.")

    baseline_match = re.search(
        r"summer\s+baseline\s+allowance\s*:\s*(\d+(?:\.\d+)?)\s*kwh",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if baseline_match is None:
        raise _bill_error(
            "RATE_LINES_NOT_FOUND", "The seasonal tier threshold evidence is missing."
        )
    period_threshold = _quantity_decimal(baseline_match.group(1))
    daily_threshold = (period_threshold / Decimal(period_days)).quantize(Decimal("0.00000001"))

    fields = (
        _field("rate_plan_name", "DOMESTIC", source_page, "Your rate"),
        _field("rate_class", "residential_tiered", source_page, "Your rate"),
        _field(
            "season_definition",
            "summer billing period",
            source_page,
            "Energy-Summer",
            "0.97",
        ),
        _field(
            "tier_threshold",
            f"{period_threshold} kWh over {period_days} days; {daily_threshold} kWh/day derived",
            source_page,
            "Your summer baseline allowance",
            "0.95",
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
            "Normalized exact line-item sum",
        ),
        _field(
            "per_kwh_rate",
            f"tier_2={tier_two} USD/kWh",
            source_page,
            "Normalized exact line-item sum",
        ),
        _field("recurring_fixed_charge", f"{daily} USD/day", source_page, "Base services charge"),
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
    charges = (
        ReusableChargeDraft(
            name="Base Services Charge", kind="daily_fixed", amount=daily, unit="USD/day"
        ),
        ReusableChargeDraft(
            name="Wildfire fund charge", kind="per_kwh", amount=wildfire, unit="USD/kWh"
        ),
        ReusableChargeDraft(
            name="Fixed recovery charge", kind="per_kwh", amount=recovery, unit="USD/kWh"
        ),
        ReusableChargeDraft(name="State tax", kind="per_kwh", amount=state_tax, unit="USD/kWh"),
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
            tier_start_kwh=Decimal("0"),
            tier_end_kwh=period_threshold,
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
            tier_start_kwh=period_threshold,
        ),
    )
    return RatePlanDraft(
        rate_plan_name="DOMESTIC",
        rate_class="residential_tiered",
        plan_classification="seasonal_tiered",
        holiday_treatment="not_applicable",
        cca_or_direct_access_indicator="sce_generation",
        periods=periods,
        billing_period_start=period_start,
        billing_period_end=period_end,
        billing_period_days=period_days,
        tier_threshold_basis=(
            "bill-period summer baseline allowance "
            f"{period_threshold} kWh over {period_days} days; derived {daily_threshold} kWh/day; "
            "administrator must confirm a reusable tariff basis"
        ),
        baseline_allocation_rule=(
            "customer-specific bill-period baseline evidence; reusable tariff threshold incomplete"
        ),
        reusable_charges=charges,
        fields=fields,
        parser_version=PARSER_VERSION,
        source_artifact_sha256=source_sha256,
        candidate_complete=False,
    )


def extract_rate_plan_from_text(
    text: str, source_sha256: str, *, source_page: int = 1
) -> RatePlanDraft:
    """Return only allowlisted reusable pricing fields; input text is not retained."""

    plan_match = PLAN_PATTERN.search(text)
    if plan_match is None:
        raise _bill_error(
            "RATE_NAME_NOT_FOUND", "No supported reusable SCE rate-plan name was found."
        )
    plan_name = plan_match.group(1).upper()
    if plan_name == "DOMESTIC":
        return _extract_domestic(text, source_sha256, source_page=source_page)
    periods: list[TouPeriodDraft] = []
    fields: list[AllowedRateField] = [
        AllowedRateField(
            name="rate_plan_name",
            normalized_value=plan_name,
            confidence=Decimal("0.98"),
            source=SourceRegion(page=source_page),
        )
    ]
    for match in PERIOD_PATTERN.finditer(text):
        season, day_type, name, start_h, start_m, end_h, end_m, price = match.groups()
        end_minute = 1440 if end_h is None else _minutes(end_h, end_m or "0")
        normalized_price = _normalize_price(price)
        period = TouPeriodDraft(
            season=cast(Literal["summer", "winter", "all"], season.lower()),
            day_type=cast(Literal["weekday", "weekend", "holiday", "all"], day_type.lower()),
            name=name.lower(),
            start_minute=_minutes(start_h, start_m),
            end_minute=end_minute,
            price_per_kwh=normalized_price,
        )
        periods.append(period)
        fields.append(
            AllowedRateField(
                name="tou_period",
                normalized_value=(
                    f"{period.season}:{period.day_type}:{period.name}:"
                    f"{period.start_minute}-{period.end_minute}@{period.price_per_kwh} USD/kWh"
                ),
                confidence=Decimal("0.93"),
                source=SourceRegion(page=source_page),
            )
        )
    if not periods:
        raise BillRateImportError("no complete reusable per-kWh schedule was found")

    baseline_match = BASELINE_PATTERN.search(text)
    baseline = _normalize_price(baseline_match.group(1)) if baseline_match else None
    if baseline is not None:
        fields.append(
            AllowedRateField(
                name="baseline_credit_rate",
                normalized_value=f"{baseline} USD/kWh",
                confidence=Decimal("0.92"),
                source=SourceRegion(page=source_page),
            )
        )
    daily_match = DAILY_PATTERN.search(text)
    charges: tuple[ReusableChargeDraft, ...] = ()
    if daily_match:
        amount = Decimal(daily_match.group(1))
        charges = (
            ReusableChargeDraft(
                name="Base Services Charge",
                kind="daily_fixed",
                amount=amount,
                unit="USD/day",
            ),
        )
        fields.append(
            AllowedRateField(
                name="recurring_fixed_charge",
                normalized_value=f"{amount} USD/day",
                confidence=Decimal("0.95"),
                source=SourceRegion(page=source_page),
            )
        )
    generation: Literal["sce_generation", "cca", "direct_access", "unknown"] = (
        "direct_access"
        if re.search(r"(?i)direct\s+access", text)
        else "cca"
        if re.search(r"(?i)community\s+choice|\bCCA\b", text)
        else "sce_generation"
    )
    return RatePlanDraft(
        rate_plan_name=plan_name,
        rate_class="residential_time_of_use",
        plan_classification="time_of_use",
        holiday_treatment="weekend_schedule",
        cca_or_direct_access_indicator=generation,
        periods=tuple(periods),
        baseline_allocation_rule=(
            "credit capped by administrator-configured baseline allocation" if baseline else None
        ),
        baseline_credit_rate=baseline,
        reusable_charges=charges,
        fields=tuple(fields),
        parser_version=PARSER_VERSION,
        source_artifact_sha256=source_sha256,
    )


def extract_rate_plan_from_pdf(
    data: bytes,
    *,
    local_ocr: Callable[[bytes, int], str] | None = None,
) -> tuple[RatePlanDraft, tuple[str, ...]]:
    if len(data) > MAX_PDF_BYTES:
        raise _bill_error("PDF_TOO_LARGE", "The PDF exceeds the configured size limit.")
    if not data.startswith(b"%PDF-"):
        raise _bill_error("PDF_INVALID", "The upload does not have a valid PDF signature.")
    artifact_hash = hashlib.sha256(data).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
    except Exception as exc:
        raise _bill_error("PDF_INVALID", "The PDF is malformed.") from exc
    if reader.is_encrypted:
        raise _bill_error("PDF_ENCRYPTED", "Encrypted or password-protected PDFs are not accepted.")
    if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
        raise _bill_error("PDF_PAGE_LIMIT", "The PDF page count is outside the configured limit.")
    page_text: list[str] = []
    categories: set[str] = set()
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise _bill_error(
                "PDF_TEXT_UNAVAILABLE", "The PDF text layer could not be extracted."
            ) from exc
        if len(text.strip()) < 30:
            if local_ocr is None:
                raise _bill_error(
                    "PDF_TEXT_UNAVAILABLE",
                    "The PDF has no usable text layer and local OCR is unavailable.",
                )
            try:
                text = local_ocr(data, page_number)
            except BillRateImportError:
                raise
            except Exception as exc:
                raise _bill_error("EXTRACTION_TIMED_OUT", "local OCR timed out or failed") from exc
        categories.update(detected_prohibited_categories(text))
        page_text.append(text)
    domestic_pages = [
        index
        for index, text in enumerate(page_text, start=1)
        if re.search(r"\bDOMESTIC\b", text, re.IGNORECASE)
    ]
    if domestic_pages:
        scored_pages = [(index, _page_score(text)) for index, text in enumerate(page_text, start=1)]
        charges_page, score = max(scored_pages, key=lambda item: item[1])
        if score < 4:
            page_text.clear()
            raise _bill_error(
                "CHARGES_PAGE_NOT_FOUND",
                "No SCE rate-detail charges page was found in the PDF.",
            )
        combined = page_text[charges_page - 1]
        normalized = " ".join(combined.split())
        if not re.search(r"\b(?:SCE|SOUTHERN\s+CALIFORNIA\s+EDISON)\b", normalized, re.IGNORECASE):
            page_text.clear()
            raise _bill_error(
                "UTILITY_NOT_RECOGNIZED", "The rate-detail page is not recognized as SCE."
            )
    else:
        # Legacy sanitized TOU fixtures can span pages and intentionally omit
        # statement chrome. Their closed schedule still receives exhaustive
        # RatePlanDraft coverage validation.
        if any(
            re.search(r"\b(?:SCE|SOUTHERN\s+CALIFORNIA\s+EDISON)\b", text, re.IGNORECASE)
            and PLAN_PATTERN.search(text) is None
            for text in page_text
        ):
            page_text.clear()
            raise _bill_error(
                "CHARGES_PAGE_NOT_FOUND",
                "No SCE rate-detail charges page was found in the PDF.",
            )
        combined = "\n".join(page_text)
        charges_page = next(
            (
                index
                for index, text in enumerate(page_text, start=1)
                if PLAN_PATTERN.search(text) is not None
            ),
            1,
        )
    try:
        draft = extract_rate_plan_from_text(combined, artifact_hash, source_page=charges_page)
        page_by_value: dict[str, int] = {}
        for page_number, text in enumerate(page_text, start=1):
            plan_match = PLAN_PATTERN.search(text)
            if plan_match is not None:
                page_by_value[plan_match.group(1).upper()] = page_number
            for match in PERIOD_PATTERN.finditer(text):
                season, day_type, name, start_h, start_m, end_h, end_m, price = match.groups()
                start_minute = _minutes(start_h, start_m)
                end_minute = 1440 if end_h is None else _minutes(end_h, end_m or "0")
                normalized_price = _normalize_price(price)
                normalized = (
                    f"{season.lower()}:{day_type.lower()}:{name.lower()}:"
                    f"{start_minute}-{end_minute}@{normalized_price} USD/kWh"
                )
                page_by_value[normalized] = page_number
            baseline = BASELINE_PATTERN.search(text)
            if baseline is not None:
                page_by_value[f"{_normalize_price(baseline.group(1))} USD/kWh"] = page_number
            daily = DAILY_PATTERN.search(text)
            if daily is not None:
                page_by_value[f"{Decimal(daily.group(1))} USD/day"] = page_number
        draft = draft.model_copy(
            update={
                "fields": tuple(
                    field.model_copy(
                        update={
                            "source": SourceRegion(
                                page=page_by_value.get(field.normalized_value, field.source.page)
                            )
                        }
                    )
                    for field in draft.fields
                )
            }
        )
    finally:
        combined = ""  # discard temporary text containing prohibited values
        page_text.clear()
    return draft, tuple(sorted(categories))
