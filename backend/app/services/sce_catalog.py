from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urljoin, urlparse, urlunparse

from .sce_rate_parser import ParsedRateCandidate

DiscoveryState = Literal["parsed", "requires_parser", "excluded"]
CatalogLinkKind = Literal["plan", "traversal", "excluded"]

CATALOG_CRAWLER_VERSION = "sce-residential-catalog-crawl-v2"
OFFICIAL_SCE_CATALOG_HOSTS = frozenset({"sce.com", "www.sce.com"})
OFFICIAL_SCE_PUBLIC_PATH_PREFIXES = ("/regulatory/", "/save-money/")
SCE_CATALOG_DISCOVERY_URL = (
    "https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans"
)

SUPPORTED_PLAN_NAMES: dict[str, str] = {
    "DOMESTIC": "Tiered Rate Plan \u2014 Schedule D",
    "TOU-D-4-9PM": "TOU-D 4 PM to 9 PM",
    "TOU-D-5-8PM": "TOU-D 5 PM to 8 PM",
    "TOU-D-PRIME": "TOU-D-PRIME",
}


@dataclass(frozen=True)
class DiscoveredScePlan:
    source_url: str
    public_plan_name: str
    canonical_name: str
    official_schedule_code: str | None
    plan_type: str
    enrollment_status: str
    eligibility: tuple[str, ...]
    discovery_state: DiscoveryState
    exclusion_reason: str | None
    normalized_plan: dict[str, Any]
    source_level: int = 2


@dataclass(frozen=True)
class DiscoveredSceCatalogLink:
    """One bounded-crawl edge extracted from captured public SCE HTML."""

    source_url: str
    target_url: str
    label: str
    canonical_name: str
    official_schedule_code: str | None
    source_level: int
    kind: CatalogLinkKind
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class SceCatalogLinkInspection:
    """Offline HTML link extraction result with fail-closed parse evidence."""

    links: tuple[DiscoveredSceCatalogLink, ...]
    error_code: str | None


class _OfficialLinks(HTMLParser):
    """Extract links only from authorized primary plan regions in ``main``.

    SCE pages contain plan-like phrases in global navigation, FAQs, related
    content, and marketing cards.  Keeping a current primary-plan heading and
    resetting it at every unrelated heading prevents those phrases from
    inheriting the preceding plan's identity.
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
        self.links: list[tuple[str, str, str | None]] = []
        self._depth = 0
        self._main_depth: int | None = None
        self._blocked_depths: list[int] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._anchor_family: str | None = None
        self._heading_tag: str | None = None
        self._heading_text: list[str] = []
        self._current_family: str | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        is_void = lower in self._VOID_TAGS
        if not is_void:
            self._depth += 1
        attributes = {key.lower(): value or "" for key, value in attrs}
        if lower in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
        if self._main_depth is None and (
            lower == "main" or attributes.get("role", "").lower() == "main"
        ):
            self._main_depth = self._depth
        marker = " ".join(
            (
                attributes.get("id", ""),
                attributes.get("class", ""),
                attributes.get("aria-label", ""),
            )
        ).lower()
        if self._in_main() and (
            lower in {"nav", "header", "footer", "aside"}
            or any(
                value in marker
                for value in (
                    "faq",
                    "frequently-asked",
                    "glossary",
                    "related-link",
                    "site-navigation",
                    "marketing",
                )
            )
        ):
            self._blocked_depths.append(self._depth)
        if self._active() and re.fullmatch(r"h[1-6]", lower):
            self._heading_tag = lower
            self._heading_text = []
        if lower == "a" and self._active():
            self._href = next((value for key, value in attrs if key.lower() == "href"), None)
            self._text = []
            self._anchor_family = self._current_family

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in self._VOID_TAGS:
            return
        if lower == "a" and self._href is not None and self._active():
            label = " ".join(" ".join(self._text).split())
            family = self._anchor_family
            if label.casefold() == "tiered rate plan":
                family = "DOMESTIC"
            elif family is None and self._heading_tag is not None:
                family = _supported_canonical(label)
            if label:
                self.links.append((self._href, label, family))
            self._href = None
            self._text = []
            self._anchor_family = None
        if self._heading_tag == lower:
            heading = " ".join(" ".join(self._heading_text).split())
            self._current_family = _supported_canonical(heading)
            self._heading_tag = None
            self._heading_text = []
        if lower in {"script", "style", "noscript", "template"} and self._skip_depth:
            self._skip_depth -= 1
        if self._blocked_depths and self._blocked_depths[-1] == self._depth:
            self._blocked_depths.pop()
        if self._main_depth == self._depth:
            self._main_depth = None
            self._current_family = None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._heading_tag is not None and self._active():
            self._heading_text.append(data)
        if self._href is not None and self._active():
            self._text.append(data)

    def _in_main(self) -> bool:
        return self._main_depth is not None and self._depth >= self._main_depth

    def _active(self) -> bool:
        return self._in_main() and not self._blocked_depths and not self._skip_depth


_KNOWN_NAMES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\bTOU\s*[- ]?D\s*[- ]?4\s*(?:PM\s*)?(?:TO|-)?\s*9\s*PM\b", re.I),
        "TOU-D-4-9PM",
        "TOU-D-4-9PM",
    ),
    (
        re.compile(r"\bTOU\s*[- ]?D\s*[- ]?5\s*(?:PM\s*)?(?:TO|-)?\s*8\s*PM\b", re.I),
        "TOU-D-5-8PM",
        "TOU-D-5-8PM",
    ),
    (re.compile(r"\bTOU\s*[- ]?D\s*[- ]?PRIME\b", re.I), "TOU-D-PRIME", "TOU-D-PRIME"),
    (re.compile(r"\b(?:DOMESTIC|SCHEDULE\s+D|TIERED\s+RATE\s+PLAN)\b", re.I), "DOMESTIC", "D"),
)


def _canonical(label: str) -> tuple[str, str | None]:
    for pattern, canonical, schedule in _KNOWN_NAMES:
        if pattern.search(label):
            return canonical, schedule
    schedule_match = re.search(
        r"\b(?:SCHEDULE\s+)?(?P<code>(?:TOU|EV|D|CPP|CARE|FERA|NEM)[A-Z0-9-]*(?:\s+[A-Z0-9-]+)*)\b",
        label,
        re.IGNORECASE,
    )
    if schedule_match is not None:
        code = re.sub(r"\s+", "-", schedule_match.group("code").upper()).strip("-")
        return code, code
    canonical = re.sub(r"[^A-Z0-9]+", "-", label.upper()).strip("-")
    return canonical[:160], None


def _supported_canonical(value: str) -> str | None:
    for pattern, canonical, _schedule in _KNOWN_NAMES[:3]:
        if pattern.search(value):
            return canonical
    if re.search(r"\b(?:TIERED\s+RATE\s+PLAN|SCHEDULE\s+D|DOMESTIC)\b", value, re.I):
        return "DOMESTIC"
    return None


def _link_matches_family(label: str, href: str, family: str) -> bool:
    matched = _supported_canonical(f"{label} {urlparse(href).path}")
    if matched == family:
        return True
    return family == "DOMESTIC" and label.casefold() == "tiered rate plan"


def _plan_type(label: str) -> str:
    upper = label.upper()
    if "CRITICAL PEAK" in upper or "CPP" in upper:
        return "critical_peak_pricing"
    if "DYNAMIC" in upper or "HOURLY" in upper:
        return "dynamic_hourly"
    if "TOU" in upper or "TIME OF USE" in upper or "TIME-OF-USE" in upper:
        if "BASELINE" in upper or "CREDIT" in upper:
            return "time_of_use_with_baseline_credit"
        return "time_of_use"
    if "TIER" in upper or "DOMESTIC" in upper or "SCHEDULE D" in upper:
        return "seasonal_tiered"
    if "FLAT" in upper:
        return "flat"
    return "unknown"


def _stored_plan_type(value: object, label: str) -> str:
    candidate = str(value or _plan_type(label))
    aliases = {
        "time_of_use_plus_baseline_credit": "time_of_use_with_baseline_credit",
    }
    candidate = aliases.get(candidate, candidate)
    allowed = {
        "flat",
        "tiered",
        "seasonal_tiered",
        "time_of_use",
        "seasonal_time_of_use",
        "time_of_use_with_baseline_credit",
        "critical_peak_pricing",
        "dynamic_hourly",
        "unknown",
    }
    return candidate if candidate in allowed else "unknown"


def _eligibility(label: str) -> tuple[str, ...]:
    upper = label.upper()
    terms = {
        "electric_vehicle": ("EV", "ELECTRIC VEHICLE", "PLUG-IN"),
        "solar_or_nem": ("SOLAR", "NEM"),
        "care": ("CARE",),
        "fera": ("FERA",),
        "medical_baseline": ("MEDICAL BASELINE",),
        "heat_pump_or_electrification": ("HEAT PUMP", "ELECTRIFICATION"),
        "battery": ("BATTERY",),
    }
    return tuple(key for key, needles in terms.items() if any(item in upper for item in needles))


def _is_official_tariff_document(label: str, href: str) -> bool:
    value = f"{label} {href}".upper()
    return urlparse(href).path.lower().endswith(".pdf") or any(
        phrase in value
        for phrase in (
            "RESIDENTIAL RATES",
            "RESIDENTIAL TARIFF",
            "RATE SCHEDULE",
            "TARIFF BOOK",
            "SCHEDULE D",
        )
    )


def _normalized_discovered_url(source_url: str, href: str) -> str | None:
    try:
        parsed = urlparse(urljoin(source_url, href))
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    normalized = urlunparse(
        (
            "https",
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )
    return normalized if len(normalized) <= 500 else None


def discover_sce_catalog_links(
    body: bytes,
    media_type: str,
    *,
    source_url: str,
) -> tuple[DiscoveredSceCatalogLink, ...]:
    return inspect_sce_catalog_links(
        body,
        media_type,
        source_url=source_url,
    ).links


def inspect_sce_catalog_links(
    body: bytes,
    media_type: str,
    *,
    source_url: str,
    enrichment_for: str | None = None,
) -> SceCatalogLinkInspection:
    """Extract only bounded-crawl-relevant links from captured official HTML.

    The function performs no network access. Non-SCE links and ordinary SCE
    navigation are deliberately ignored. Official tariff links on the legacy
    SharePoint host are retained as explicit exclusions because that host is
    outside the production fetch allowlist.
    """

    if media_type not in {"text/html", "application/xhtml+xml"}:
        return SceCatalogLinkInspection(links=(), error_code="CATALOG_INDEX_MEDIA_TYPE")
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return SceCatalogLinkInspection(links=(), error_code="CATALOG_INDEX_INVALID_UTF8")
    parser = _OfficialLinks()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return SceCatalogLinkInspection(links=(), error_code="CATALOG_INDEX_HTML_PARSE_FAILED")

    discovered: dict[str, DiscoveredSceCatalogLink] = {}
    for href, raw_label, scoped_family in parser.links:
        label = " ".join(raw_label.split())[:160]
        target_url = _normalized_discovered_url(source_url, href)
        if target_url is None:
            continue
        host = (urlparse(target_url).hostname or "").lower().rstrip(".")
        family = enrichment_for or scoped_family
        if family not in SUPPORTED_PLAN_NAMES:
            continue
        tariff_document = _is_official_tariff_document(label, target_url)
        if host == "edisonintl.sharepoint.com" and tariff_document:
            canonical = family
            schedule = "D" if family == "DOMESTIC" else family
            discovered.setdefault(
                target_url,
                DiscoveredSceCatalogLink(
                    source_url=source_url,
                    target_url=target_url,
                    label=label,
                    canonical_name=canonical,
                    official_schedule_code=schedule,
                    source_level=1,
                    kind="excluded",
                    exclusion_reason="OFFICIAL_HOST_OUTSIDE_FETCH_ALLOWLIST",
                ),
            )
            continue
        if host not in OFFICIAL_SCE_CATALOG_HOSTS:
            continue
        if not urlparse(target_url).path.lower().startswith(OFFICIAL_SCE_PUBLIC_PATH_PREFIXES):
            continue
        if not _link_matches_family(label, target_url, family):
            continue
        candidate = DiscoveredSceCatalogLink(
            source_url=source_url,
            target_url=target_url,
            label=label,
            canonical_name=family,
            official_schedule_code="D" if family == "DOMESTIC" else family,
            source_level=1 if tariff_document else 2,
            kind="plan",
        )
        prior = discovered.get(target_url)
        if prior is None:
            discovered[target_url] = candidate
    return SceCatalogLinkInspection(
        links=tuple(discovered[url] for url in sorted(discovered)),
        error_code=None,
    )


def discovered_plan_from_link(
    link: DiscoveredSceCatalogLink,
    *,
    discovery_state: DiscoveryState,
    exclusion_reason: str | None = None,
) -> DiscoveredScePlan:
    if link.kind == "traversal":
        raise ValueError("catalog traversal links are not rate plans")
    return DiscoveredScePlan(
        source_url=link.target_url,
        public_plan_name=SUPPORTED_PLAN_NAMES[link.canonical_name],
        canonical_name=link.canonical_name,
        official_schedule_code=link.official_schedule_code,
        plan_type=_plan_type(link.label),
        enrollment_status=_enrollment_status(link.label),
        eligibility=_eligibility(link.label),
        discovery_state=discovery_state,
        exclusion_reason=exclusion_reason,
        normalized_plan={},
        source_level=link.source_level,
    )


def _catalog_metadata(parsed: ParsedRateCandidate) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in (
        "effective_start",
        "effective_end",
        "season_definitions",
        "holiday_treatment",
        "holiday_rule",
        "timezone",
        "currency",
    ):
        value = parsed.normalized_rates.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _normalized_catalog_plan(
    plan: dict[str, Any],
    parsed: ParsedRateCandidate,
) -> dict[str, Any]:
    return {**plan, "catalog_metadata": _catalog_metadata(parsed)}


def _candidate_signature_complete(
    plan: dict[str, Any],
    parsed: ParsedRateCandidate,
) -> bool:
    periods = plan.get("periods")
    if not isinstance(periods, list) or not periods:
        return False
    if any(
        not isinstance(period, dict)
        or not isinstance(period.get("price_per_kwh"), str)
        or not period["price_per_kwh"]
        for period in periods
    ):
        return False
    if plan.get("daily_fixed_charge") is None and plan.get("monthly_fixed_charge") is None:
        return False
    canonical, _schedule = _canonical(str(plan.get("rate_plan_name", "")))
    if canonical == "DOMESTIC":
        names = {str(period.get("name")) for period in periods if isinstance(period, dict)}
        if not {"tier_1", "tier_2"}.issubset(names) or not plan.get("tier_threshold_basis"):
            return False
    else:
        if any(
            period.get("season") is None
            or period.get("day_type") is None
            or period.get("start_minute") is None
            or period.get("end_minute") is None
            for period in periods
            if isinstance(period, dict)
        ):
            return False
    return bool(
        parsed.normalized_rates.get("effective_start")
        or parsed.normalized_rates.get("effective_date_confirmation_required") is True
    )


def _enrollment_status(label: str) -> str:
    upper = label.upper()
    if "EXISTING CUSTOMER" in upper or "CLOSED TO NEW" in upper:
        return "existing_customers_only"
    if "PILOT" in upper:
        return "pilot"
    if "CLOSED" in upper or "NOT AVAILABLE" in upper:
        return "closed"
    return "open_or_eligibility_required"


def discover_sce_catalog(
    body: bytes,
    media_type: str,
    *,
    source_url: str,
    parsed: ParsedRateCandidate | None,
) -> tuple[DiscoveredScePlan, ...]:
    """Inventory every plan-like official link in one captured SCE revision.

    This function performs no network access.  Callers pass bytes already
    obtained through the bounded official-source fetcher.  Unknown plans are
    retained as ``requires_parser`` instead of being silently omitted.
    """

    if media_type not in {"text/html", "application/xhtml+xml"}:
        return ()
    try:
        body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return ()
    parsed_plans: dict[str, dict[str, Any]] = {}
    if parsed is not None:
        plans = parsed.normalized_rates.get("plans")
        if isinstance(plans, list):
            for plan in plans:
                if not isinstance(plan, dict):
                    continue
                raw_name = plan.get("rate_plan_name")
                if isinstance(raw_name, str) and raw_name:
                    canonical, _schedule = _canonical(raw_name)
                    if canonical in SUPPORTED_PLAN_NAMES and _candidate_signature_complete(
                        plan, parsed
                    ):
                        parsed_plans[canonical] = _normalized_catalog_plan(plan, parsed)

    discovered: dict[str, DiscoveredScePlan] = {}
    for link in discover_sce_catalog_links(body, media_type, source_url=source_url):
        if link.kind == "traversal":
            continue
        if link.kind == "excluded":
            discovered.setdefault(
                link.canonical_name,
                discovered_plan_from_link(
                    link,
                    discovery_state="excluded",
                    exclusion_reason=link.exclusion_reason,
                ),
            )
            continue
        normalized = parsed_plans.get(link.canonical_name, {})
        state: DiscoveryState = "parsed" if normalized else "requires_parser"
        discovered[link.canonical_name] = DiscoveredScePlan(
            source_url=link.target_url,
            public_plan_name=SUPPORTED_PLAN_NAMES[link.canonical_name],
            canonical_name=link.canonical_name,
            official_schedule_code=link.official_schedule_code,
            plan_type=_stored_plan_type(normalized.get("pricing_model"), link.label),
            enrollment_status=_enrollment_status(link.label),
            eligibility=_eligibility(link.label),
            discovery_state=state,
            exclusion_reason=None,
            normalized_plan=normalized,
            source_level=link.source_level,
        )

    # A schedule page may contain no self-link.  Parsed plans are still explicit
    # catalog results from the captured official revision.
    for canonical, plan in parsed_plans.items():
        if canonical in discovered:
            continue
        _canonical_name, schedule = _canonical(str(plan.get("rate_plan_name", canonical)))
        discovered[canonical] = DiscoveredScePlan(
            source_url=source_url,
            public_plan_name=SUPPORTED_PLAN_NAMES[canonical],
            canonical_name=canonical,
            official_schedule_code=schedule,
            plan_type=_stored_plan_type(plan.get("pricing_model"), str(plan.get("rate_plan_name"))),
            enrollment_status="open_or_eligibility_required",
            eligibility=(),
            discovery_state="parsed",
            exclusion_reason=None,
            normalized_plan=plan,
            source_level=2,
        )
    return tuple(discovered[key] for key in sorted(discovered))


__all__ = [
    "CATALOG_CRAWLER_VERSION",
    "OFFICIAL_SCE_PUBLIC_PATH_PREFIXES",
    "SCE_CATALOG_DISCOVERY_URL",
    "SUPPORTED_PLAN_NAMES",
    "DiscoveredSceCatalogLink",
    "DiscoveredScePlan",
    "SceCatalogLinkInspection",
    "discover_sce_catalog",
    "discover_sce_catalog_links",
    "discovered_plan_from_link",
    "inspect_sce_catalog_links",
]
