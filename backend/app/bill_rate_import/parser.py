from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable
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

PARSER_VERSION = "sce-rate-only-v1"

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

PLAN_PATTERN = re.compile(r"\b(TOU-D-(?:4-9PM|5-8PM|PRIME))\b", re.IGNORECASE)
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


def detected_prohibited_categories(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in PROHIBITED_PATTERNS.items() if pattern.search(text))


def _minutes(hour: str, minute: str) -> int:
    return int(hour) * 60 + int(minute)


def _normalize_price(raw: str) -> Decimal:
    value = Decimal(raw)
    return value / Decimal(100) if value > Decimal("2") else value


def extract_rate_plan_from_text(
    text: str, source_sha256: str, *, source_page: int = 1
) -> RatePlanDraft:
    """Return only allowlisted reusable pricing fields; input text is not retained."""

    plan_match = PLAN_PATTERN.search(text)
    if plan_match is None:
        raise BillRateImportError("no supported reusable SCE rate-plan name was found")
    plan_name = plan_match.group(1).upper()
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
        raise BillRateImportError("PDF exceeds the configured size limit")
    if not data.startswith(b"%PDF-"):
        raise BillRateImportError("upload does not have a valid PDF signature")
    artifact_hash = hashlib.sha256(data).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
    except Exception as exc:
        raise BillRateImportError("malformed PDF") from exc
    if reader.is_encrypted:
        raise BillRateImportError("encrypted or password-protected PDFs are not accepted")
    if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
        raise BillRateImportError("PDF page count is outside the configured limit")
    page_text: list[str] = []
    categories: set[str] = set()
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise BillRateImportError("PDF text extraction failed") from exc
        if len(text.strip()) < 30:
            if local_ocr is None:
                raise BillRateImportError(
                    "document requires local OCR, but no OCR worker is configured"
                )
            try:
                text = local_ocr(data, page_number)
            except BillRateImportError:
                raise
            except Exception as exc:
                raise BillRateImportError("local OCR timed out or failed") from exc
        categories.update(detected_prohibited_categories(text))
        page_text.append(text)
    combined = "\n".join(page_text)
    try:
        draft = extract_rate_plan_from_text(combined, artifact_hash)
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
