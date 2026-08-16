from __future__ import annotations

import pytest
from backend.app.services.sce_rate_parser import (
    SourceParseError,
    parse_sce_public_page,
)


def _html(value: str) -> bytes:
    return f"<html><body>{value}</body></html>".encode()


def test_domestic_seasonal_tiered_does_not_require_holiday_rules() -> None:
    parsed = parse_sce_public_page(
        _html(
            """
            <h1>Tiered Rate Plan</h1><p>Schedule D DOMESTIC</p>
            <p>Current rates as of 6/1/26.</p>
            <h2>Tier 1</h2><p>30 cents per kWh</p><p>Up to baseline allocation</p>
            <h2>Tier 2</h2><p>40 cents per kWh</p><p>Over baseline allocation</p>
            <h2>Summer Daily Allocations (June - September)</h2>
            <p>79 cents daily Base Services Charge</p>
            """
        ),
        "text/html",
    )

    assert parsed.normalized_rates["plan_classification"] == "seasonal_tiered"
    assert parsed.normalized_rates["holiday_treatment"] == "not_applicable"
    assert parsed.normalized_rates["holiday_rule"] == "not_applicable"
    assert parsed.normalized_rates["plans"][0]["rate_plan_name"] == "DOMESTIC"
    assert parsed.normalized_rates["effective_start"] == "2026-06-01"
    assert parsed.validation_evidence["effective_date"] == "2026-06-01"


def test_tiered_primary_content_wins_over_incidental_tou_navigation() -> None:
    parsed = parse_sce_public_page(
        _html(
            """
            <nav><a>Time-Of-Use Plans</a></nav>
            <main><h1>Tiered Rate Plan</h1>
            <p>Tier 1 Allocation 30 cents / kWh</p>
            <p>Tier 2 Allocation 40 cents / kWh</p>
            <p>79 cents daily Base Services Charge</p>
            <p>Summer Daily Allocations</p>
            <p>Rates effective 06/01/2026</p></main>
            """
        ),
        "text/html",
    )

    assert parsed.normalized_rates["plan_classification"] == "seasonal_tiered"
    assert parsed.normalized_rates["holiday_treatment"] == "not_applicable"


def test_flat_plan_does_not_require_holiday_rules() -> None:
    parsed = parse_sce_public_page(
        _html("<h1>Flat Rate Plan</h1><p>Flat Rate Plan $0.25 per kWh</p>"),
        "text/html",
    )

    assert parsed.normalized_rates["plan_classification"] == "flat"
    assert parsed.normalized_rates["holiday_treatment"] == "not_applicable"


def test_tou_missing_holiday_treatment_reports_specific_diagnostic() -> None:
    with pytest.raises(SourceParseError) as captured:
        parse_sce_public_page(
            _html("<h1>Time-of-Use TOU-D-4-9PM</h1><p>weekday on peak schedule</p>"),
            "text/html",
        )

    assert captured.value.error_code == "HOLIDAY_RULE_MISSING"
    assert captured.value.evidence["classification"] == "time_of_use"


def test_unknown_plan_reports_classification_not_holiday_error() -> None:
    with pytest.raises(SourceParseError) as captured:
        parse_sce_public_page(_html("<h1>Residential pricing</h1>"), "text/html")

    assert captured.value.error_code == "RATE_PLAN_TYPE_UNRESOLVED"
