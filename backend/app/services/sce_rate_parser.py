from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from itertools import pairwise
from typing import Any, Literal

PARSER_VERSION = "sce-power-monitor-compatible-v3"
CANDIDATE_SCHEMA = "sce-rate-candidate/1.0.0"

PlanClassification = Literal[
    "flat",
    "tiered",
    "seasonal_tiered",
    "time_of_use",
    "unknown",
]
HolidayTreatment = Literal[
    "not_applicable",
    "no_special_treatment",
    "weekend_schedule",
    "explicit_schedule",
    "unresolved",
]


class SourceParseError(ValueError):
    """A parser failure containing only allowlisted operational evidence."""

    def __init__(
        self,
        error_code: str,
        detail: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        self.evidence = evidence or {}


@dataclass(frozen=True)
class ParsedRateCandidate:
    normalized_rates: dict[str, Any]
    validation_evidence: dict[str, Any]


class _VisibleText(HTMLParser):
    """Collect visible text from the predecessor's bounded plan container.

    The live SCE document includes site navigation, educational articles, and
    FAQs containing rate-like words.  The predecessor parser succeeded because
    it read the ``accordion-container-bg-layout`` plan region rather than the
    entire document.  Prefer that exact region, then ``main``, and use the full
    body only for isolated plan documents and deterministic parser fixtures.
    """

    _VOID_TAGS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._skip_depth = 0
        self._main_depth: int | None = None
        self._primary_depths: list[int] = []
        self._blocked_depths: list[int] = []
        self._all_values: list[str] = []
        self._main_values: list[str] = []
        self._primary_values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower not in self._VOID_TAGS:
            self._depth += 1
        attributes = {key.lower(): value or "" for key, value in attrs}
        if lower in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
        if self._main_depth is None and (
            lower == "main" or attributes.get("role", "").lower() == "main"
        ):
            self._main_depth = self._depth
        classes = set(attributes.get("class", "").lower().split())
        if "accordion-container-bg-layout" in classes:
            self._primary_depths.append(self._depth)
        marker = " ".join(
            (
                attributes.get("id", ""),
                attributes.get("class", ""),
                attributes.get("aria-label", ""),
            )
        ).lower()
        if lower in {"nav", "header", "footer", "aside"} or any(
            value in marker
            for value in (
                "faq",
                "frequently-asked",
                "glossary",
                "related-link",
                "site-navigation",
                "marketing",
            )
        ):
            self._blocked_depths.append(self._depth)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self._VOID_TAGS:
            return
        if lower in {"script", "style", "noscript", "template"} and self._skip_depth:
            self._skip_depth -= 1
        if self._blocked_depths and self._blocked_depths[-1] == self._depth:
            self._blocked_depths.pop()
        if self._primary_depths and self._primary_depths[-1] == self._depth:
            self._primary_depths.pop()
        if self._main_depth == self._depth:
            self._main_depth = None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._blocked_depths:
            return
        value = " ".join(data.replace("\xa0", " ").split())
        if value:
            self._all_values.append(value)
            if self._main_depth is not None:
                self._main_values.append(value)
            if self._primary_depths:
                self._primary_values.append(value)

    @property
    def values(self) -> list[str]:
        return self._primary_values or self._main_values or self._all_values


@dataclass(frozen=True)
class _GroupDefinition:
    season: str
    day_type: str
    start_marker: str
    end_marker: str | None
    names: tuple[str, ...]
    time_labels: tuple[str, ...]
    boundaries: tuple[int, ...]


@dataclass(frozen=True)
class _PlanDefinition:
    name: str
    heading: str
    next_heading: str | None
    has_baseline_credit: bool
    groups: tuple[_GroupDefinition, ...]


def _groups(peak_start: int, peak_end: int) -> tuple[_GroupDefinition, ...]:
    def time_label(minute: int) -> str:
        hour = minute // 60
        suffix = "pm" if hour >= 12 else "am"
        display_hour = hour - 12 if hour > 12 else hour
        return f"{display_hour}{suffix}"

    peak_start_label = time_label(peak_start)
    peak_end_label = time_label(peak_end)
    return (
        _GroupDefinition(
            season="summer",
            day_type="weekday",
            start_marker="Weekdays",
            end_marker="Weekend",
            names=("off_peak", "on_peak", "off_peak"),
            time_labels=("12am", peak_start_label, peak_end_label, "12am"),
            boundaries=(0, peak_start, peak_end, 1440),
        ),
        _GroupDefinition(
            season="summer",
            day_type="weekend_holiday",
            start_marker="Weekend",
            end_marker=None,
            names=("off_peak", "mid_peak", "off_peak"),
            time_labels=("12am", peak_start_label, peak_end_label, "12am"),
            boundaries=(0, peak_start, peak_end, 1440),
        ),
        _GroupDefinition(
            season="winter",
            day_type="all_days",
            start_marker="Weekdays & Weekend",
            end_marker=None,
            names=("off_peak", "super_off_peak", "mid_peak", "off_peak"),
            time_labels=("12am", "8am", peak_start_label, peak_end_label, "12am"),
            boundaries=(0, 480, peak_start, peak_end, 1440),
        ),
    )


PLAN_DEFINITIONS = (
    _PlanDefinition(
        name="TOU-D-4-9PM",
        heading=r"TOU-D\s*4\s*PM\s*(?:TO|-)\s*9\s*PM",
        next_heading=r"TOU-D\s*5\s*PM\s*(?:TO|-)\s*8\s*PM",
        has_baseline_credit=True,
        groups=_groups(960, 1260),
    ),
    _PlanDefinition(
        name="TOU-D-5-8PM",
        heading=r"TOU-D\s*5\s*PM\s*(?:TO|-)\s*8\s*PM",
        next_heading=r"TOU-D\s*-?\s*PRIME",
        has_baseline_credit=True,
        groups=_groups(1020, 1200),
    ),
    _PlanDefinition(
        name="TOU-D-PRIME",
        heading=r"TOU-D\s*-?\s*PRIME",
        next_heading=None,
        has_baseline_credit=False,
        groups=_groups(960, 1260),
    ),
)

PRICE_PATTERN = re.compile(
    r"(?P<name>SUPER\s*[- ]?\s*OFF\s*[- ]?\s*PEAK|OFF\s*[- ]?\s*PEAK|"
    r"MID\s*[- ]?\s*PEAK|ON\s*[- ]?\s*PEAK)\s+"
    r"(?:(?P<dollar>\$)\s*)?(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<cents>\u00a2|CENTS?)?\s*(?:(?:PER|/)\s*KWH)?",
    re.IGNORECASE,
)


def _section(text: str, start_pattern: str, end_pattern: str | None) -> str:
    start = re.search(start_pattern, text, flags=re.IGNORECASE)
    if start is None:
        raise SourceParseError(
            "LAYOUT_MISSING_SECTION",
            "required SCE rate section is missing",
            evidence={"missing_section": start_pattern[:80]},
        )
    if end_pattern is None:
        return text[start.end() :]
    end = re.search(end_pattern, text[start.end() :], flags=re.IGNORECASE)
    if end is None:
        raise SourceParseError(
            "LAYOUT_MISSING_SECTION",
            "required SCE rate section boundary is missing",
            evidence={"missing_section_boundary": end_pattern[:80]},
        )
    return text[start.end() : start.end() + end.start()]


def _normalized_period_name(value: str) -> str:
    return re.sub(r"[^A-Z]+", "_", value.upper()).strip("_").lower()


def _decimal_price(match: re.Match[str]) -> Decimal:
    try:
        value = Decimal(match.group("amount"))
    except InvalidOperation as exc:
        raise SourceParseError("PRICE_INVALID", "SCE rate price is invalid") from exc
    is_cents = match.group("cents") is not None and match.group("dollar") is None
    price = value / Decimal(100) if is_cents else value
    if price <= 0 or price > Decimal("5"):
        raise SourceParseError("PRICE_OUT_OF_RANGE", "SCE rate price is outside safe bounds")
    return price.quantize(Decimal("0.00000001"))


def _require_time_sequence(text: str, expected: tuple[str, ...]) -> None:
    labels = tuple(
        match.group(0).lower().replace(" ", "").replace(".", "")
        for match in re.finditer(
            r"\b(?:12|[1-9]|1[01])\s*(?:a\.?m\.?|p\.?m\.?)\b",
            text,
            re.IGNORECASE,
        )
    )
    observed = labels[: len(expected)]
    legacy_terminal_midnight_omitted = expected[-1:] == ("12am",) and labels == expected[:-1]
    if observed != expected and not legacy_terminal_midnight_omitted:
        raise SourceParseError(
            "TIME_BOUNDARY_MISMATCH",
            "SCE period time boundaries did not match the approved structure",
            evidence={"expected_time_labels": list(expected), "observed_count": len(labels)},
        )


def _parse_group(text: str, definition: _GroupDefinition) -> list[dict[str, Any]]:
    group = _section(text, re.escape(definition.start_marker), definition.end_marker)
    visible_rates = re.split(
        r"AFTER\s+BASELINE\s+CREDIT",
        group,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    _require_time_sequence(visible_rates, definition.time_labels)
    matches = list(PRICE_PATTERN.finditer(visible_rates))
    names = tuple(_normalized_period_name(match.group("name")) for match in matches)
    if names != definition.names:
        raise SourceParseError(
            "PERIOD_STRUCTURE_MISMATCH",
            "SCE period names or count did not match the approved structure",
            evidence={
                "season": definition.season,
                "day_type": definition.day_type,
                "expected_period_count": len(definition.names),
                "observed_period_count": len(names),
            },
        )
    periods: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        periods.append(
            {
                "season": definition.season,
                "day_type": definition.day_type,
                "name": definition.names[index],
                "start_minute": definition.boundaries[index],
                "end_minute": definition.boundaries[index + 1],
                "price_per_kwh": format(_decimal_price(match), "f"),
                "currency": "USD",
                "unit": "kWh",
                "tier_min_kwh": None,
                "tier_max_kwh": None,
            }
        )
    if periods[0]["start_minute"] != 0 or periods[-1]["end_minute"] != 1440:
        raise SourceParseError("COVERAGE_INVALID", "SCE periods do not cover a complete day")
    for first, second in pairwise(periods):
        if first["end_minute"] != second["start_minute"]:
            raise SourceParseError("COVERAGE_INVALID", "SCE periods contain a gap or overlap")
    return periods


def _charge(block: str) -> Decimal:
    match = re.search(
        r"BASE\s+SERVICES?\s+CHARGE\s*:?\s*(?:\|\s*)?\$\s*"
        r"(?P<amount>\d+(?:\.\d+)?)\s*(?:PER|/)\s*DAY",
        block,
        re.IGNORECASE,
    )
    if match is None:
        raise SourceParseError("FIXED_CHARGE_MISSING", "SCE daily base service charge is missing")
    value = Decimal(match.group("amount")).quantize(Decimal("0.00000001"))
    if value <= 0 or value > Decimal("20"):
        raise SourceParseError("FIXED_CHARGE_INVALID", "SCE daily charge is outside safe bounds")
    return value


def _baseline_credit(block: str, required: bool) -> tuple[Decimal, bool]:
    if not required:
        if re.search(r"BASELINE\s+CREDIT\s*:?\s*(?:\|\s*)?NONE", block, re.IGNORECASE):
            return Decimal("0.00000000"), True
        raise SourceParseError(
            "BASELINE_RULE_MISSING", "SCE baseline-credit absence is not explicit"
        )
    match = re.search(
        r"BASELINE\s+CREDIT\s*:?\s*(?:\|\s*)?\$\s*(?P<amount>\d+(?:\.\d+)?)\s*"
        r"(?:PER|/)\s*KWH\s+UP\s+TO\s+(?:YOUR\s+)?(?:MONTHLY\s+)?BASELINE\s+ALLOCATION",
        block,
        re.IGNORECASE,
    )
    scope_verified = match is not None
    if match is None:
        match = re.search(
            r"BASELINE\s+CREDIT\s*:?\s*(?:\|\s*)?\$\s*(?P<amount>\d+(?:\.\d+)?)\s*"
            r"(?:PER|/)\s*KWH",
            block,
            re.IGNORECASE,
        )
    if match is None:
        raise SourceParseError("BASELINE_RULE_MISSING", "SCE baseline credit is missing")
    value = Decimal(match.group("amount")).quantize(Decimal("0.00000001"))
    if value <= 0 or value > Decimal("1"):
        raise SourceParseError(
            "BASELINE_RULE_INVALID", "SCE baseline credit is outside safe bounds"
        )
    return value, scope_verified


def _component_scope_verified(block: str) -> bool:
    return (
        re.search(
            r"RATES\s+SHOWN\s+REFLECT\s+PRICING.*DELIVERY\s+AND\s+GENERATION.*SCE",
            block,
            re.IGNORECASE | re.DOTALL,
        )
        is not None
    )


def _classify_plan(text: str) -> PlanClassification:
    """Classify before applying schedule-specific validation.

    This ordering is security relevant: an unknown or tiered tariff must never
    be forced through the TOU holiday validator and mislabeled as a missing
    holiday rule.
    """

    # SCE pages share global navigation and FAQ content. The official tiered
    # page therefore mentions TOU plans even though its primary tariff is not
    # time-dependent. Prefer stronger page-local tier evidence before an
    # incidental TOU link or comparison paragraph.
    if re.search(r"\b(?:DOMESTIC|SCHEDULE\s+D)\b", text, re.IGNORECASE) and re.search(
        r"\bTIER\s*1\b.*\bTIER\s*2\b", text, re.IGNORECASE | re.DOTALL
    ):
        return (
            "seasonal_tiered"
            if re.search(
                r"JUNE\s*[-\u2013\u2014]\s*SEPTEMBER|SUMMER\s+(?:DAILY\s+)?ALLOCATION",
                text,
                re.IGNORECASE,
            )
            else "tiered"
        )
    if re.search(r"TIERED\s+RATE\s+PLAN", text, re.IGNORECASE) and re.search(
        r"\bTIER\s*1\b.*\bTIER\s*2\b", text, re.IGNORECASE | re.DOTALL
    ):
        return (
            "seasonal_tiered"
            if re.search(
                r"SUMMER\s+(?:DAILY\s+)?ALLOCATIONS?|JUNE\s*[-\u2013\u2014]\s*SEPTEMBER",
                text,
                re.IGNORECASE,
            )
            else "tiered"
        )
    if re.search(r"\bTOU(?:-D)?\b|TIME\s*[- ]?OF\s*[- ]?USE", text, re.IGNORECASE):
        return "time_of_use"
    if re.search(r"\bFLAT\s+RATE\b", text, re.IGNORECASE):
        return "flat"
    return "unknown"


def _holiday_treatment(text: str, classification: PlanClassification) -> HolidayTreatment:
    if classification in {"flat", "tiered", "seasonal_tiered"}:
        return "not_applicable"
    if classification == "unknown":
        raise SourceParseError(
            "RATE_PLAN_TYPE_UNRESOLVED",
            "the SCE source tariff could not be classified safely",
            evidence={"classification": classification},
        )
    if re.search(
        r"HOLIDAYS?\s+(?:FOLLOW|USE|ARE\s+CHARGED\s+AT)\s+(?:THE\s+)?WEEKEND\s+RATES?",
        text,
        re.IGNORECASE,
    ):
        return "weekend_schedule"
    if re.search(r"HOLIDAYS?\s+(?:HAVE|USE)\s+NO\s+SPECIAL\s+TREATMENT", text, re.IGNORECASE):
        return "no_special_treatment"
    if re.search(r"HOLIDAY\s+(?:RATE|SCHEDULE|PERIOD)", text, re.IGNORECASE):
        return "explicit_schedule"
    return "unresolved"


def _visible_text(body: bytes, media_type: str) -> str:
    if media_type not in {"text/html", "application/xhtml+xml"}:
        raise SourceParseError(
            "PARSER_MEDIA_TYPE_UNSUPPORTED",
            "the configured SCE page parser requires HTML",
        )
    try:
        source = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceParseError("SOURCE_ENCODING_INVALID", "SCE source is not valid UTF-8") from exc
    parser = _VisibleText()
    try:
        parser.feed(source)
        parser.close()
    except Exception as exc:
        raise SourceParseError("HTML_INVALID", "SCE source HTML could not be parsed") from exc
    return "\n".join(parser.values)


def _public_decimal(raw: str, *, cents: bool) -> Decimal:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation as exc:
        raise SourceParseError("PRICE_INVALID", "SCE public price is invalid") from exc
    if cents:
        value /= Decimal(100)
    if value <= 0 or value > Decimal("20"):
        raise SourceParseError("PRICE_OUT_OF_RANGE", "SCE public price is outside safe bounds")
    return value.quantize(Decimal("0.00000001"))


def _effective_date(text: str) -> str | None:
    match = re.search(
        r"(?:CURRENT\s+RATES?\s+AS\s+OF|RATES?\s+EFFECTIVE)\s+"
        r"(?P<month>\d{1,2})/(?P<day>\d{1,2})/(?P<year>\d{2,4})",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    year = int(match.group("year"))
    if year < 100:
        year += 2000
    return f"{year:04d}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"


def _tier_price(text: str, tier: int) -> Decimal:
    match = re.search(
        rf"\bTIER\s*{tier}\b(?:(?!\bTIER\s*[12]\b).){{0,240}}?"
        r"(?:(?P<dollar>\$)\s*)?(?P<amount>\d+(?:\.\d+)?)\s*(?P<cents>\u00a2|CENTS?)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        raise SourceParseError(
            "RATE_LINES_NOT_FOUND",
            "the SCE tiered source does not contain both tier prices",
            evidence={"missing_tier": tier},
        )
    return _public_decimal(match.group("amount"), cents=match.group("dollar") is None)


def _tiered_candidate(
    text: str,
    classification: Literal["tiered", "seasonal_tiered"],
) -> ParsedRateCandidate:
    tier_one = _tier_price(text, 1)
    tier_two = _tier_price(text, 2)
    base_match = re.search(
        r"(?:(?P<dollar>\$)\s*)?(?P<amount>\d+(?:\.\d+)?)\s*"
        r"(?P<cents>\u00a2|CENTS?)?\s*(?:DAILY|PER\s+DAY)\s+BASE\s+SERVICES?\s+CHARGE",
        text,
        re.IGNORECASE,
    ) or re.search(
        r"BASE\s+SERVICES?\s+CHARGE\s*:?.{0,40}?"
        r"(?:(?P<dollar>\$)\s*)?(?P<amount>\d+(?:\.\d+)?)\s*"
        r"(?P<cents>\u00a2|CENTS?)?\s*(?:PER\s+DAY|/\s*DAY)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if base_match is None:
        raise SourceParseError(
            "FIXED_CHARGE_MISSING",
            "the SCE tiered source daily base service charge is missing",
        )
    daily = _public_decimal(
        base_match.group("amount"),
        cents=base_match.group("dollar") is None and base_match.group("cents") is not None,
    )
    effective_date = _effective_date(text)
    periods = [
        {
            "season": "all",
            "day_type": "all",
            "name": "tier_1",
            "start_minute": 0,
            "end_minute": 1440,
            "price_per_kwh": format(tier_one, "f"),
            "currency": "USD",
            "unit": "kWh",
            "tier_min_kwh": "0.000000",
            "tier_max_kwh": None,
        },
        {
            "season": "all",
            "day_type": "all",
            "name": "tier_2",
            "start_minute": 0,
            "end_minute": 1440,
            "price_per_kwh": format(tier_two, "f"),
            "currency": "USD",
            "unit": "kWh",
            "tier_min_kwh": None,
            "tier_max_kwh": None,
        },
    ]
    normalized = {
        "schema": CANDIDATE_SCHEMA,
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "plan_classification": classification,
        "holiday_treatment": "not_applicable",
        "season_definitions": {
            "summer": {"start_month": 6, "end_month": 9},
            "winter": {"start_month": 10, "end_month": 5},
        },
        "holiday_rule": "not_applicable",
        "effective_start": effective_date,
        "effective_end": None,
        "effective_date_confirmation_required": effective_date is None,
        "plans": [
            {
                "rate_plan_name": "DOMESTIC",
                "rate_class": "residential",
                "pricing_model": classification,
                "daily_fixed_charge": format(daily, "f"),
                "monthly_fixed_charge": "0.00000000",
                "baseline_credit_per_kwh": "0.00000000",
                "rate_components": "sce_delivery_and_generation_combined",
                "rate_precision": "consumer_display_rounded",
                "tier_threshold_basis": "home_baseline_allocation_review_required",
                "periods": periods,
            }
        ],
    }
    return ParsedRateCandidate(
        normalized_rates=normalized,
        validation_evidence={
            "parser_version": PARSER_VERSION,
            "schema": CANDIDATE_SCHEMA,
            "plan_classification": classification,
            "holiday_treatment": "not_applicable",
            "plan_count": 1,
            "period_count": len(periods),
            "seasons": ["summer", "winter"],
            "day_types": ["all"],
            "coverage": "semantic_tier_coverage",
            "price_unit": "USD/kWh",
            "effective_date": effective_date or "administrator_confirmation_required",
            "warnings": [
                "PUBLIC_SOURCE_PRICES_ARE_DISPLAY_ROUNDED",
                "HOME_BASELINE_ALLOCATION_REVIEW_REQUIRED",
            ],
        },
    )


def _flat_candidate(text: str) -> ParsedRateCandidate:
    price_match = re.search(
        r"FLAT\s+RATE(?:\s+PLAN)?.{0,240}?"
        r"(?:(?P<dollar>\$)\s*)?(?P<amount>\d+(?:\.\d+)?)\s*"
        r"(?P<cents>\u00a2|CENTS?)?\s*(?:PER|/)\s*KWH",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if price_match is None:
        raise SourceParseError(
            "RATE_LINES_NOT_FOUND",
            "the SCE flat source does not contain a reusable per-kWh rate",
        )
    price = _public_decimal(
        price_match.group("amount"),
        cents=price_match.group("dollar") is None and price_match.group("cents") is not None,
    )
    normalized = {
        "schema": CANDIDATE_SCHEMA,
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "plan_classification": "flat",
        "holiday_treatment": "not_applicable",
        "season_definitions": {
            "summer": {"start_month": 6, "end_month": 9},
            "winter": {"start_month": 10, "end_month": 5},
        },
        "holiday_rule": "not_applicable",
        "effective_start": None,
        "effective_end": None,
        "effective_date_confirmation_required": True,
        "plans": [
            {
                "rate_plan_name": "FLAT",
                "rate_class": "residential",
                "pricing_model": "flat",
                "daily_fixed_charge": "0.00000000",
                "monthly_fixed_charge": "0.00000000",
                "baseline_credit_per_kwh": "0.00000000",
                "rate_components": "sce_delivery_and_generation_combined",
                "rate_precision": "consumer_display_rounded",
                "periods": [
                    {
                        "season": "all",
                        "day_type": "all",
                        "name": "flat",
                        "start_minute": 0,
                        "end_minute": 1440,
                        "price_per_kwh": format(price, "f"),
                        "currency": "USD",
                        "unit": "kWh",
                        "tier_min_kwh": None,
                        "tier_max_kwh": None,
                    }
                ],
            }
        ],
    }
    return ParsedRateCandidate(
        normalized_rates=normalized,
        validation_evidence={
            "parser_version": PARSER_VERSION,
            "schema": CANDIDATE_SCHEMA,
            "plan_classification": "flat",
            "holiday_treatment": "not_applicable",
            "plan_count": 1,
            "period_count": 1,
            "seasons": ["summer", "winter"],
            "day_types": ["all"],
            "coverage": "complete",
            "price_unit": "USD/kWh",
            "effective_date": "administrator_confirmation_required",
        },
    )


def parse_sce_public_page(body: bytes, media_type: str) -> ParsedRateCandidate:
    text = _visible_text(body, media_type)
    classification = _classify_plan(text)
    treatment = _holiday_treatment(text, classification)
    if classification == "tiered":
        return _tiered_candidate(text, "tiered")
    if classification == "seasonal_tiered":
        return _tiered_candidate(text, "seasonal_tiered")
    if classification == "flat":
        return _flat_candidate(text)

    matched_definitions = tuple(
        definition
        for definition in PLAN_DEFINITIONS
        if re.search(definition.heading, text, flags=re.IGNORECASE) is not None
    )
    if not matched_definitions:
        raise SourceParseError(
            "LAYOUT_MISSING_SECTION",
            "the SCE time-of-use source contains no supported plan section",
        )
    plans: list[dict[str, Any]] = []
    component_scope_verified = True
    baseline_scope_verified = True
    for index, definition in enumerate(matched_definitions):
        next_heading = (
            matched_definitions[index + 1].heading if index + 1 < len(matched_definitions) else None
        )
        block = _section(text, definition.heading, next_heading)
        plan_component_scope_verified = _component_scope_verified(block)
        component_scope_verified = component_scope_verified and plan_component_scope_verified
        summer = _section(
            block,
            r"JUNE\s*[-\u2013\u2014]\s*SEPTEMBER",
            r"OCTOBER\s*[-\u2013\u2014]\s*MAY",
        )
        winter = _section(block, r"OCTOBER\s*[-\u2013\u2014]\s*MAY", None)
        periods: list[dict[str, Any]] = []
        periods.extend(_parse_group(summer, definition.groups[0]))
        periods.extend(_parse_group(summer, definition.groups[1]))
        periods.extend(_parse_group(winter, definition.groups[2]))
        if treatment == "unresolved":
            periods = [
                {**period, "day_type": "weekend"}
                if period["day_type"] == "weekend_holiday"
                else period
                for period in periods
            ]
        if len(periods) != 10:
            raise SourceParseError(
                "PERIOD_COUNT_INVALID",
                "SCE plan does not contain the required complete period set",
                evidence={"plan": definition.name, "observed_period_count": len(periods)},
            )
        baseline_credit, plan_baseline_scope_verified = _baseline_credit(
            block, definition.has_baseline_credit
        )
        baseline_scope_verified = baseline_scope_verified and plan_baseline_scope_verified
        plans.append(
            {
                "rate_plan_name": definition.name,
                "rate_class": "residential",
                "pricing_model": (
                    "time_of_use_plus_baseline_credit"
                    if definition.has_baseline_credit
                    else "time_of_use"
                ),
                "daily_fixed_charge": format(_charge(block), "f"),
                "monthly_fixed_charge": "0.00000000",
                "baseline_credit_per_kwh": format(baseline_credit, "f"),
                "baseline_credit_scope": (
                    "through_home_baseline_allocation"
                    if plan_baseline_scope_verified
                    else "unresolved"
                ),
                "rate_components": (
                    "sce_delivery_and_generation_combined"
                    if plan_component_scope_verified
                    else "unresolved"
                ),
                "rate_precision": "consumer_display_rounded",
                "eligibility": (
                    [
                        "electric_vehicle",
                        "plug_in_hybrid",
                        "residential_battery",
                        "heat_pump_or_electrification",
                    ]
                    if definition.name == "TOU-D-PRIME"
                    else []
                ),
                "enrollment_status": "open_or_eligibility_required",
                "periods": periods,
            }
        )

    effective_date = _effective_date(text)
    normalized = {
        "schema": CANDIDATE_SCHEMA,
        "utility_name": "Southern California Edison",
        "timezone": "America/Los_Angeles",
        "currency": "USD",
        "season_definitions": {
            "summer": {"start_month": 6, "end_month": 9},
            "winter": {"start_month": 10, "end_month": 5},
        },
        "plan_classification": classification,
        "holiday_treatment": treatment,
        "holiday_rule": {
            "weekend_schedule": "weekend_rates",
            "no_special_treatment": "no_special_treatment",
            "explicit_schedule": "explicit_schedule",
            "unresolved": "unresolved",
        }[treatment],
        "effective_start": effective_date,
        "effective_end": None,
        "effective_date_confirmation_required": effective_date is None,
        "rate_component_scope_verified": component_scope_verified,
        "baseline_credit_scope_verified": baseline_scope_verified,
        "plans": plans,
    }
    return ParsedRateCandidate(
        normalized_rates=normalized,
        validation_evidence={
            "parser_version": PARSER_VERSION,
            "schema": CANDIDATE_SCHEMA,
            "plan_classification": classification,
            "holiday_treatment": treatment,
            "plan_count": len(plans),
            "period_count": sum(len(plan["periods"]) for plan in plans),
            "seasons": ["summer", "winter"],
            "day_types": (
                ["weekday", "weekend", "holiday"]
                if treatment != "unresolved"
                else ["weekday", "weekend"]
            ),
            "coverage": "complete",
            "price_unit": "USD/kWh",
            "effective_date": effective_date or "administrator_confirmation_required",
            "warnings": [
                "PUBLIC_SOURCE_PRICES_ARE_DISPLAY_ROUNDED",
                *(
                    ["HOLIDAY_TREATMENT_REQUIRES_AUTHORITATIVE_EVIDENCE"]
                    if treatment == "unresolved"
                    else []
                ),
                *(
                    ["RATE_COMPONENT_SCOPE_REQUIRES_AUTHORITATIVE_EVIDENCE"]
                    if not component_scope_verified
                    else []
                ),
                *(
                    ["BASELINE_CREDIT_SCOPE_REQUIRES_AUTHORITATIVE_EVIDENCE"]
                    if not baseline_scope_verified
                    else []
                ),
            ],
        },
    )


def parse_sce_tou_public_page(body: bytes, media_type: str) -> ParsedRateCandidate:
    """Backward-compatible entry point retained for existing callers."""

    return parse_sce_public_page(body, media_type)


def side_by_side_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    *,
    previous_candidate_id: str | None,
) -> dict[str, Any]:
    """Return complete review values plus a bounded list of changed paths."""

    changes: list[dict[str, Any]] = []

    def walk(path: str, old: Any, new: Any) -> None:
        if len(changes) >= 500:
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                walk(f"{path}.{key}" if path else key, old.get(key), new.get(key))
            return
        if isinstance(old, list) and isinstance(new, list):
            for index in range(max(len(old), len(new))):
                walk(
                    f"{path}[{index}]",
                    old[index] if index < len(old) else None,
                    new[index] if index < len(new) else None,
                )
            return
        if old != new:
            changes.append({"path": path, "before": old, "after": new})

    walk("", before, after)
    return {
        "schema": "sce-rate-diff/1.0.0",
        "previous_candidate_id": previous_candidate_id,
        "before": before,
        "after": after,
        "changes": changes,
        "change_count": len(changes),
        "truncated": len(changes) >= 500,
    }


__all__ = [
    "CANDIDATE_SCHEMA",
    "PARSER_VERSION",
    "ParsedRateCandidate",
    "SourceParseError",
    "parse_sce_public_page",
    "parse_sce_tou_public_page",
    "side_by_side_diff",
]
